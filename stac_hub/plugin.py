# -*- coding: utf-8 -*-
"""Плагин STAC Hub: тулбар, меню, главный диалог, стриминг слоёв через /vsicurl/.

Работа с мультиспектральными снимками: RGB-синтезы (истинный/ложный цвет,
SWIR, сельское хозяйство, геология, застройка) и спектральные индексы
(NDVI/NDWI/NDBI/NBR) — см. render_presets.py.
"""

import os
import re
import tempfile
import time

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from qgis.core import (
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMultiBandColorRenderer,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)

try:
    from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
except ImportError:  # pragma: no cover - qgis.analysis входит в поставку QGIS
    QgsRasterCalculator = None
    QgsRasterCalculatorEntry = None

try:
    from osgeo import gdal
except ImportError:  # pragma: no cover
    gdal = None

from . import render_presets, stac_client

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# ограничение грида расчёта индекса (пикселей по каждой стороне)
INDEX_MAX_SIDE = 5000


class StacHubPlugin(object):
    """Основной класс плагина."""

    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.menu = "&STAC Hub"
        self.toolbar = None
        self.dialog = None

    # ------------------------------------------------------------------ UI
    def initGui(self):
        icon = QIcon(os.path.join(PLUGIN_DIR, "icons", "stac_hub.png"))
        self.action_open = QAction(icon, "STAC Hub — поиск открытых геоданных", self.iface.mainWindow())
        self.action_open.setObjectName("stac_hub_open")
        self.action_open.triggered.connect(self.show_dialog)

        self.iface.addPluginToWebMenu(self.menu, self.action_open)
        self.iface.addWebToolBarIcon(self.action_open)

        self.toolbar = self.iface.addToolBar("STAC Hub")
        self.toolbar.setObjectName("STACHubToolbar")
        self.toolbar.addAction(self.action_open)

    def unload(self):
        self.iface.removePluginWebMenu(self.menu, self.action_open)
        self.iface.removeWebToolBarIcon(self.action_open)
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    def show_dialog(self):
        if self.dialog is None:
            from .dialog import StacHubDialog
            self.dialog = StacHubDialog(self, parent=self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    # ------------------------------------------------------------- данные
    def canvas_bbox(self):
        """[w, s, e, n] текущего экстента карты в WGS84 (EPSG:4326)."""
        canvas = self.iface.mapCanvas()
        if canvas is None:
            return None
        extent = canvas.extent()
        crs = canvas.mapSettings().destinationCrs()
        if crs.authid() != "EPSG:4326":
            try:
                xform = QgsCoordinateTransform(
                    crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                    QgsProject.instance())
                extent = xform.transformBoundingBox(extent)
            except Exception:  # noqa: BLE001 - не смогли трансформировать
                return None
        # умеренное расширение мелких экстентов, чтобы поиск что-то находил
        if extent.width() < 0.02 and extent.height() < 0.02:
            extent.grow(0.05)
        return [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]

    def add_cog_layer(self, http_url, layer_name, creds=None):
        """Стриминг растра через /vsicurl/ + опциональные GDAL-опции авторизации.

        Возвращает (успех, сообщение).
        """
        self._apply_gdal_auth(creds)
        uri = stac_client.vsicurl_url(http_url)
        layer = QgsRasterLayer(uri, layer_name, "gdal")
        if not layer.isValid():
            msg = ("Слой недоступен: {} (проверьте сеть/доступ; для защищённых "
                   "источников задайте учётные данные)").format(http_url[:120])
            return False, msg
        QgsProject.instance().addMapLayer(layer)
        return True, ""

    def add_rendered_layer(self, meta, preset, creds=None):
        """Добавляет слой айтема с учётом выбранного способа отображения.

        meta  — словарь из таблицы результатов (normalize);
        preset — ключ пресета render_presets ('true_color', 'false_color',
                 'swir', 'agri', 'geo', 'urban', 'ndvi', 'ndwi', 'ndbi',
                 'nbr') или 'plain'/'None' — добавить ассет как есть.
        Возвращает (успех, сообщение).
        """
        try:
            return self._add_rendered(meta, preset, creds)
        except Exception as exc:  # noqa: BLE001 - не роняем интерфейс
            return False, "Ошибка построения слоя: {}".format(exc)

    def _add_rendered(self, meta, preset, creds):
        if not preset or preset == "plain" or (preset == "true_color" and not meta.get("assets")):
            return self.add_cog_layer(meta.get("asset_url", ""), self._layer_name(meta), creds)
        rp = render_presets.plan_for(meta.get("assets") or {}, preset)
        if rp is None:
            if preset == "true_color":
                # синтез недоступен — добавляем выбранный ассет как есть
                return self.add_cog_layer(meta.get("asset_url", ""), self._layer_name(meta), creds)
            return False, ("У айтема нет каналов для «{}» "
                           "(это норм для SAR/DEM/тематических продуктов).").format(preset)
        self._apply_gdal_auth(creds)
        coll = meta.get("collection") or ""
        if preset in {i["key"]: i for i in render_presets.INDICES}:
            return self._add_index_layer(meta, rp, coll)
        return self._add_composite_layer(meta, rp, coll)

    # --------------------------------------------------------- композиты
    def _add_composite_layer(self, meta, rp, coll):
        plan = rp["plan"]
        name = self._layer_name(meta)
        if plan["mode"] == "single":
            url = stac_client.sign_url(plan["href"], coll)
            layer = QgsRasterLayer(stac_client.vsicurl_url(url), name, "gdal")
            if not layer.isValid():
                return False, "Слой недоступен: {}".format((plan["href"] or "")[:100])
            self._apply_rgb_renderer(layer, plan["bands"])
            QgsProject.instance().addMapLayer(layer)
            return True, ""
        # стек: по одному одноцветному ассету на канал -> VRT
        sources, master = [], None
        if gdal is None:
            return False, "Модуль osgeo.gdal недоступен."
        for role in rp["roles"]:
            href = stac_client.sign_url(plan["bands"][role]["href"], coll)
            ds = gdal.Open(stac_client.vsicurl_url(href))
            if ds is None:
                return False, "Канал не читается ({}): {}".format(role, href[:90])
            if master is None or (ds.RasterXSize * ds.RasterYSize >=
                                  master[0] * master[1]):
                master = (ds.RasterXSize, ds.RasterYSize,
                          ds.GetGeoTransform(), ds.GetProjection())
            sources.append({
                "href": stac_client.vsicurl_url(href),
                "dtype": gdal.GetDataTypeName(ds.GetRasterBand(1).DataType) or "Float32",
                "src_w": ds.RasterXSize, "src_h": ds.RasterYSize,
            })
            ds = None
        w, h, gt, proj = master
        vrt_path = _vrt_path(meta, rp)
        with open(vrt_path, "w", encoding="utf-8") as f:
            f.write(render_presets.build_vrt_xml(w, h, gt, proj, sources))
        layer = QgsRasterLayer(vrt_path, name, "gdal")
        if not layer.isValid():
            return False, "Виртуальный слой (VRT) не открылся: {}".format(vrt_path)
        self._apply_rgb_renderer(layer, {role: i + 1 for i, role in enumerate(rp["roles"])})
        QgsProject.instance().addMapLayer(layer)
        return True, ""

    @staticmethod
    def _apply_rgb_renderer(layer, band_map):
        """Мультиканальный рендерер + растяжка 2–98% по каждому каналу."""
        dp = layer.dataProvider()
        renderer = QgsMultiBandColorRenderer(
            dp, band_map.get("red", 1), band_map.get("green", 2), band_map.get("blue", 3))
        for role, attr in (("red", "Red"), ("green", "Green"), ("blue", "Blue")):
            band = band_map.get(role)
            if not band:
                continue
            try:
                mn, mx = _cumcut(dp, band)
                enh = QgsContrastEnhancement(dp.dataType(band))
                enh.setContrastEnhancementAlgorithm(
                    QgsContrastEnhancement.StretchToMinimumMaximum)
                enh.setMinimumValue(mn)
                enh.setMaximumValue(mx)
                getattr(renderer, "set{}ContrastEnhancement".format(attr))(enh)
            except Exception:  # noqa: BLE001 - без растяжки тоже покажет
                pass
        layer.setRenderer(renderer)
        try:
            layer.setDefaultContrastEnhancement()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ индексы
    def _add_index_layer(self, meta, rp, coll):
        if QgsRasterCalculator is None or QgsRasterCalculatorEntry is None:
            return False, "Модуль qgis.analysis недоступен — индексы нельзя рассчитать."
        plan = rp["plan"]
        # базовый слой с каналами индекса (один ассет или VRT)
        if plan["mode"] == "single":
            url = stac_client.sign_url(plan["href"], coll)
            base = QgsRasterLayer(stac_client.vsicurl_url(url), "A", "gdal")
            if not base.isValid():
                return False, "Каналы индекса не читаются: {}".format((plan["href"] or "")[:90])
        else:
            if gdal is None:
                return False, "Модуль osgeo.gdal недоступен."
            sources, master = [], None
            for role in rp["roles"]:
                href = stac_client.sign_url(plan["bands"][role]["href"], coll)
                ds = gdal.Open(stac_client.vsicurl_url(href))
                if ds is None:
                    return False, "Канал не читается ({}): {}".format(role, href[:90])
                if master is None or (ds.RasterXSize * ds.RasterYSize >=
                                      master[0] * master[1]):
                    master = (ds.RasterXSize, ds.RasterYSize,
                              ds.GetGeoTransform(), ds.GetProjection())
                sources.append({
                    "href": stac_client.vsicurl_url(href),
                    "dtype": gdal.GetDataTypeName(ds.GetRasterBand(1).DataType) or "Float32",
                    "src_w": ds.RasterXSize, "src_h": ds.RasterYSize,
                })
                ds = None
            w, h, gt, proj = master
            vrt_path = _vrt_path(meta, rp)
            with open(vrt_path, "w", encoding="utf-8") as f:
                f.write(render_presets.build_vrt_xml(w, h, gt, proj, sources))
            base = QgsRasterLayer(vrt_path, "A", "gdal")
            if not base.isValid():
                return False, "Виртуальный слой (VRT) не открылся: {}".format(vrt_path)
        # ссылки на каналы в выражении
        band_no = {role: (plan["bands"][role] if plan["mode"] == "single" else i + 1)
                   for i, role in enumerate(rp["roles"])}
        ref_a = "A@{}".format(band_no[rp["roles"][0]])
        ref_b = "A@{}".format(band_no[rp["roles"][1]])
        expr = render_presets.index_expression(rp["def"], ref_a, ref_b)
        # экстент расчёта: вид карты, пересечённый с растром (в CRS растра)
        extent = base.extent()
        downsampled = False
        try:
            canvas = self.iface.mapCanvas()
            if canvas is not None:
                cext = canvas.extent()
                ccrs = canvas.mapSettings().destinationCrs()
                if ccrs.authid() != base.crs().authid():
                    cext = QgsCoordinateTransform(
                        ccrs, base.crs(), QgsProject.instance()).transformBoundingBox(cext)
                inter = cext.intersect(base.extent())
                if not inter.isEmpty() and inter.width() > 0 and inter.height() > 0:
                    extent = inter
        except Exception:  # noqa: BLE001 - нет карты/трансформации -> весь растр
            pass
        pw = max(base.rasterUnitsPerPixelX(), 1e-12)
        ph = max(base.rasterUnitsPerPixelY(), 1e-12)
        ncols = int(extent.width() / pw) + 1
        nrows = int(extent.height() / ph) + 1
        if max(ncols, nrows) > INDEX_MAX_SIDE:
            k = float(INDEX_MAX_SIDE) / max(ncols, nrows)
            ncols = max(int(ncols * k), 2)
            nrows = max(int(nrows * k), 2)
            downsampled = True
        out_dir = os.path.join(tempfile.gettempdir(), "stac_hub_indices")
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        out_path = os.path.join(out_dir, "{}_{}.tif".format(
            rp["def"]["key"], _safe_name(meta.get("item_id", "")) or str(int(time.time()))))
        entries = []
        for ref, band in ((ref_a, band_no[rp["roles"][0]]), (ref_b, band_no[rp["roles"][1]])):
            e = QgsRasterCalculatorEntry()
            e.ref = ref
            e.raster = base
            e.bandNumber = band
            entries.append(e)
        calc = QgsRasterCalculator(expr, out_path, "GTiff", extent, base.crs(),
                                   ncols, nrows, entries)
        # QGIS 3.x: processCalculation(); страховка для нестандартных сборок
        runner = getattr(calc, "processCalculation", None) or getattr(calc, "run", None)
        if runner is None:
            return False, "QGIS не предоставляет API расчёта растровых выражений."
        res = runner()
        if res != getattr(QgsRasterCalculator, "Success", 0):
            return False, "Расчёт индекса не удался (код {}).".format(res)
        name = "{} · {}".format(rp["def"]["label"], self._layer_name(meta))
        layer = QgsRasterLayer(out_path, name, "gdal")
        if not layer.isValid():
            return False, "Результат индекса не открылся: {}".format(out_path)
        self._apply_index_style(layer, rp["def"]["key"])
        QgsProject.instance().addMapLayer(layer)
        msg = "Рассчитан {}: {}".format(rp["def"]["label"], out_path)
        if downsampled:
            msg += " (грид уменьшен до {} px по большой стороне)".format(INDEX_MAX_SIDE)
        return True, msg

    @staticmethod
    def _apply_index_style(layer, index_key):
        """Псевдоцвет для индекса по встроенной палитре."""
        dp = layer.dataProvider()
        try:
            mn, mx = _cumcut(dp, 1)
        except Exception:  # noqa: BLE001
            mn, mx = render_presets.DEFAULT_RANGE
        if not (mx > mn):
            mn, mx = render_presets.DEFAULT_RANGE
        stops = render_presets.RAMP_COLORS.get(index_key)
        if not stops:
            return
        items = []
        for frac, rgb in stops:
            items.append(QgsColorRampShader.ColorRampItem(
                mn + frac * (mx - mn), QColor(*rgb), ""))
        fcn = QgsColorRampShader()
        fcn.setColorRampType(QgsColorRampShader.Interpolated)
        fcn.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(fcn)
        layer.setRenderer(QgsSingleBandPseudoColorRenderer(dp, 1, shader))

    # ------------------------------------------------------------- прочее
    def _apply_gdal_auth(self, creds):
        if gdal is not None and creds:
            for key, value in stac_client.gdal_auth_options(creds).items():
                gdal.SetConfigOption(key, value)

    @staticmethod
    def _layer_name(meta):
        return "{} · {}".format(meta.get("collection") or meta.get("source_name", ""),
                                meta.get("date", ""))

    @staticmethod
    def show_info(parent, text):
        QMessageBox.information(parent, "STAC Hub", text)


def _cumcut(dp, band):
    """(min, max) кумулятивного среза 2–98% (учитывает варианты возврата PyQGIS)."""
    res = dp.cumulativeCut(band, 0.02, 0.98)
    nums = [float(v) for v in (res if isinstance(res, (tuple, list)) else (res,))
            if isinstance(v, (int, float))]
    if len(nums) >= 2:
        return nums[-2], nums[-1]
    raise ValueError("cumulativeCut вернул {}".format(res))


def _safe_name(s):
    return re.sub(r"[^\w\-]+", "_", s or "")[:60]


def _vrt_path(meta, rp):
    d = os.path.join(tempfile.gettempdir(), "stac_hub_vrt")
    if not os.path.isdir(d):
        os.makedirs(d)
    base = _safe_name(meta.get("item_id", "")) or "item"
    return os.path.join(d, "{}_{}_{}.vrt".format(
        base, rp["def"]["key"], str(int(time.time()))))
