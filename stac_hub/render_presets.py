# -*- coding: utf-8 -*-
"""Пресеты отображения мультиспектральных снимков плагина STAC Hub.

Модуль не зависит от QGIS: только стандартная библиотека.

- COMPOSITES — RGB-синтезы: какие роли каналов и в каком порядке;
- INDICES — спектральные индексы (формула, палитра);
- resolve_roles() — сопоставление ролей (red/nir/swir16/...) с ассетами
  айтема по eo:bands/common_name либо по имени ключа ассета;
- build_vrt_xml() — сборка GDAL VRT, склеивающей одноцветные ассеты
  (например B04/B03/B02 Sentinel-2) в один многослойный виртуальный растр.

Роль — каноническое имя канала: red, green, blue, nir, swir16, swir22,
coastal. Конкретные имена (common_name из eo:bands и ключи ассетов)
сводятся к ролям через CN_ALIASES и KEY_PATTERNS.
"""

import xml.sax.saxutils as _sax

# ----------------------------------------------------------------- пресеты
COMPOSITES = [
    {"key": "true_color", "label": "Истинный цвет (RGB)",
     "roles": ("red", "green", "blue")},
    {"key": "false_color", "label": "Ложный цвет (NIR–R–G)",
     "roles": ("nir", "red", "green")},
    {"key": "swir", "label": "SWIR (SWIR1–NIR–R)",
     "roles": ("swir16", "nir", "red")},
    {"key": "agri", "label": "Сельское хозяйство (SWIR1–NIR–B)",
     "roles": ("swir16", "nir", "blue")},
    {"key": "geo", "label": "Геология (SWIR1–SWIR2–B)",
     "roles": ("swir16", "swir22", "blue")},
    {"key": "urban", "label": "Городская застройка (SWIR2–NIR–R)",
     "roles": ("swir22", "nir", "red")},
]

# --------------------------------------------------------------- индексы
INDICES = [
    {"key": "ndvi", "label": "NDVI — вегетация", "roles": ("nir", "red"),
     "expr": "({a} - {b}) / ({a} + {b})"},
    {"key": "ndwi", "label": "NDWI — открытая вода", "roles": ("green", "nir"),
     "expr": "({a} - {b}) / ({a} + {b})"},
    {"key": "ndbi", "label": "NDBI — застройка", "roles": ("swir16", "nir"),
     "expr": "({a} - {b}) / ({a} + {b})"},
    {"key": "nbr", "label": "NBR — выгорание/пожары", "roles": ("nir", "swir22"),
     "expr": "({a} - {b}) / ({a} + {b})"},
]

# Палитры индексов: позиция 0..1 -> RGB (интерполированная легенда).
RAMP_COLORS = {
    "ndvi": [(0.00, (165, 0, 38)), (0.20, (215, 48, 39)), (0.40, (254, 224, 139)),
             (0.50, (255, 255, 191)), (0.60, (145, 207, 96)), (0.80, (26, 152, 80)),
             (1.00, (0, 104, 55))],
    "ndwi": [(0.00, (166, 97, 26)), (0.40, (224, 194, 125)), (0.50, (247, 247, 247)),
             (0.60, (146, 197, 222)), (1.00, (5, 69, 158))],
    "ndbi": [(0.00, (43, 131, 186)), (0.45, (171, 221, 164)), (0.55, (255, 255, 191)),
             (0.70, (253, 174, 97)), (1.00, (215, 25, 28))],
    "nbr":  [(0.00, (255, 255, 255)), (0.45, (255, 255, 191)), (0.60, (254, 196, 79)),
             (0.80, (212, 80, 39)), (1.00, (115, 15, 4))],
}

DEFAULT_RANGE = (-1.0, 1.0)

# ------------------------------------------------------- имена -> роли
# common_name из eo:bands -> роль
CN_ALIASES = {
    "red": "red", "green": "green", "blue": "blue",
    "nir": "nir", "nir08": "nir", "nir09": "nir",
    "swir16": "swir16", "swir1": "swir16",
    "swir22": "swir22", "swir2": "swir22",
    "coastal": "coastal", "aerosol": "coastal",
}

# Ключ ассета (когда eo:bands нет) -> роль. Проверка суффикса с границей:
# "sr_b4" ~ "b4", но "b11" НЕ ~ "b1" (граница по "_"/"-"/началу имени).
KEY_PATTERNS = {
    "red": ("b04", "b4", "sr_b4", "sr_b04", "sur_refl_b01", "red"),
    "green": ("b03", "b3", "sr_b3", "sr_b03", "sur_refl_b04", "green"),
    "blue": ("b02", "b2", "sr_b2", "sr_b02", "sur_refl_b03", "blue"),
    "nir": ("b08", "b8", "sr_b5", "sr_b05", "sur_refl_b02", "nir", "nir08", "nir09"),
    "swir16": ("b11", "sr_b6", "sr_b06", "sur_refl_b06", "swir16", "swir1"),
    "swir22": ("b12", "sr_b7", "sr_b07", "sur_refl_b07", "swir22", "swir2"),
    "coastal": ("b01", "sr_b1", "sr_b01", "coastal"),
}

VRT_COLOR_INTERP = {0: "Red", 1: "Green", 2: "Blue"}


def _asset_key_name(key):
    """Базовое имя ключа ассета: 'B04.tif' -> 'b04', 'SR_B4.TIF' -> 'sr_b4'."""
    k = (key or "").lower()
    for ext in (".tif", ".tiff", ".jp2", ".img"):
        if k.endswith(ext):
            k = k[: -len(ext)]
            break
    return k.rsplit("/", 1)[-1]


def _key_matches(key, pattern):
    k = _asset_key_name(key)
    p = pattern.lower()
    if k == p:
        return True
    if k.endswith(p):
        prefix = k[: -len(p)]
        return prefix == "" or prefix.endswith("_") or prefix.endswith("-")
    return False


def _role_by_key(key):
    for role, patterns in KEY_PATTERNS.items():
        for p in patterns:
            if _key_matches(key, p):
                return role
    return None


def _asset_role_bands(asset):
    """{роль: номер канала (1-базный)} по eo:bands ассета."""
    out = {}
    for i, cn in enumerate(asset.get("bands") or []):
        r = CN_ALIASES.get((cn or "").lower())
        if r:
            out.setdefault(r, i + 1)
    return out


def resolve_roles(assets, roles):
    """Сопоставляет роли каналов ассетам айтема.

    assets — {ключ: {'href', 'bands' (список common_name), 'roles', 'type'}}.
    roles  — последовательность ролей, напр. ('nir', 'red', 'green').

    Возвращает план или None:
      {'mode': 'single', 'asset', 'href', 'bands': {роль: номер канала}}
        — все роли есть в одном многослойном ассете (NAIP, visual/TCI);
      {'mode': 'stack', 'bands': {роль: {'key', 'href', 'band'}}}
        — каналы надо склеить в VRT (Sentinel-2/Landsat/MODIS: ассет на канал).
    """
    assets = assets or {}
    # 1) один многослойный ассет со всеми ролями
    for key, a in assets.items():
        m = _asset_role_bands(a)
        if m and all(r in m for r in roles):
            return {"mode": "single", "asset": key, "href": a.get("href") or "",
                    "bands": {r: m[r] for r in roles}}
    # 2) стек: по общим именам eo:bands, затем по ключу ассета
    out = {}
    for r in roles:
        found = None
        for key, a in assets.items():
            m = _asset_role_bands(a)
            if r in m:
                found = {"key": key, "href": a.get("href") or "", "band": m[r]}
                break
        if not found:
            for key, a in assets.items():
                if _role_by_key(key) == r and (a.get("href") or ""):
                    found = {"key": key, "href": a.get("href") or "", "band": 1}
                    break
        if not found or not found["href"]:
            return None
        out[r] = found
    return {"mode": "stack", "bands": out}


def find_composite(key):
    for c in COMPOSITES:
        if c["key"] == key:
            return c
    return None


def find_index(key):
    for i in INDICES:
        if i["key"] == key:
            return i
    return None


def plan_for(assets, preset):
    """План построения слоя для пресета: {'roles', 'plan', 'def'} | None."""
    d = find_composite(preset) or find_index(preset)
    if not d:
        return None
    plan = resolve_roles(assets, d["roles"])
    if not plan:
        return None
    return {"roles": list(d["roles"]), "plan": plan, "def": d}


def menu_options(assets):
    """Доступные варианты отображения для комбо UI: [(ключ, подпись)]."""
    opts = []
    assets = assets or {}
    for c in COMPOSITES:
        if resolve_roles(assets, c["roles"]):
            opts.append((c["key"], c["label"]))
    for ind in INDICES:
        if resolve_roles(assets, ind["roles"]):
            opts.append((ind["key"], ind["label"]))
    opts.append(("plain", "Как есть (выбранный ассет)"))
    return opts


def index_expression(index_def, ref_a, ref_b):
    """Формула индекса для QgsRasterCalculator: ссылки 'слой@канал' в кавычках."""
    return index_def["expr"].format(
        a='"{}"'.format(ref_a), b='"{}"'.format(ref_b))


def build_vrt_xml(width, height, geo_transform, srs_wkt, sources):
    """VRT, склеивающая одноцветные источники в W x H многослойный растр.

    sources — [{'href', 'dtype', 'src_w', 'src_h', 'band'}] по одному на канал;
    геометрия каждого источника растягивается на полный грид VRT.
    """
    gt = geo_transform
    parts = [
        '<VRTDataset rasterXSize="{w}" rasterYSize="{h}">'.format(w=int(width), h=int(height)),
        '<SRS dataAxisToSRSAxisMapping="1,2">{}</SRS>'.format(_sax.escape(srs_wkt or "")),
        "<GeoTransform>{0}, {1}, 0, {2}, 0, {3}</GeoTransform>".format(
            float(gt[0]), float(gt[1]), float(gt[3]), float(gt[5])),
    ]
    for i, s in enumerate(sources, start=1):
        parts.append(
            '<VRTRasterBand dataType="{dt}" band="{n}">'
            "<ColorInterp>{ci}</ColorInterp>"
            "<SimpleSource>"
            '<SourceFilename relativeToVRT="0">{href}</SourceFilename>'
            "<SourceBand>{sb}</SourceBand>"
            '<SrcRect xOff="0" yOff="0" xSize="{sw}" ySize="{sh}"/>'
            '<DstRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/>'
            "</SimpleSource>"
            "</VRTRasterBand>".format(
                dt=s.get("dtype") or "Float32", n=i,
                ci=VRT_COLOR_INTERP.get(i - 1, "Undefined"),
                href=_sax.escape(s["href"], {'"': "&quot;"}),
                sb=int(s.get("band") or 1),
                sw=int(s["src_w"]), sh=int(s["src_h"]),
                w=int(width), h=int(height)))
    parts.append("</VRTDataset>")
    return "".join(parts)
