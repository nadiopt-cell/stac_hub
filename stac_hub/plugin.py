# -*- coding: utf-8 -*-
"""Плагин STAC Hub: тулбар, меню, главный диалог, стриминг слоёв через /vsicurl/."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from qgis.core import QgsRasterLayer, QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem

try:
    from osgeo import gdal
except ImportError:  # pragma: no cover
    gdal = None

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


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
        if gdal is not None and creds:
            for key, value in self._gdal_auth_options(creds).items():
                gdal.SetConfigOption(key, value)
        uri = "/vsicurl/" + http_url
        layer = QgsRasterLayer(uri, layer_name, "gdal")
        if not layer.isValid():
            msg = ("Слой недоступен: {} (проверьте сеть/доступ; для защищённых "
                   "источников задайте учётные данные)").format(http_url[:120])
            return False, msg
        QgsProject.instance().addMapLayer(layer)
        return True, ""

    @staticmethod
    def _gdal_auth_options(creds):
        from . import stac_client
        return stac_client.gdal_auth_options(creds)

    # ------------------------------------------------------------- прочее
    @staticmethod
    def show_info(parent, text):
        QMessageBox.information(parent, "STAC Hub", text)
