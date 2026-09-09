# -*- coding: utf-8 -*-
"""STAC-клиент плагина STAC Hub.

Модуль не зависит от QGIS: только requests. Используется плагином
и пригоден для автономного тестирования.

Возможности:
- поиск в STAC API (POST /search) с Basic/Bearer/произвольным заголовком;
- обход статических каталогов (Vantor/Maxar/Umbra) в ширину с локальной фильтрацией;
- выбор лучшего растрового ассета (COG) и миниатюры у айтема;
- сборка /vsicurl/-URL для стриминга в QGIS/GDAL.
"""

import base64
import json
import threading
import time

try:
    import requests
except ImportError:  # pragma: no cover - в QGIS requests есть всегда
    requests = None

USER_AGENT = "QGIS-STACHub/0.1"
TIMEOUT = 30
THUMB_TIMEOUT = 8
THUMB_MAX_BYTES = 2 * 1024 * 1024

# общая сессия для мелких запросов (миниатюры): переиспользует соединения
_SESSION = None


def _thumb_session():
    global _SESSION
    if _SESSION is None:
        import requests as _r
        _SESSION = _r.Session()
        _SESSION.headers.update({"User-Agent": USER_AGENT})
    return _SESSION

IMAGE_MIME_HINTS = ("tiff", "geotiff", "image/tiff", "image/jp2", "image/x.hdf", "image/nitf", "nitf")
SKIP_EXT = (".json", ".xml", ".txt", ".md", ".jpg", ".jpeg", ".png", ".gif", ".html", ".yml", ".yaml")
DENY_EXT = (".zip", ".parquet", ".cphd", ".cpk", ".pdf", ".kmz", ".exe", ".7z", ".gz")


# --------------------------------------------------------------------------
# Авторизация
# --------------------------------------------------------------------------
def build_headers(source, creds):
    """HTTP-заголовки запроса с учётом учётных данных.

    creds: None или dict {mode: none|basic|bearer|header, user, password,
    token, header_name, header_value}
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"}
    if not creds:
        return headers
    mode = (creds or {}).get("mode") or "none"
    if mode == "basic":
        pair = "{}:{}".format(creds.get("user", ""), creds.get("password", ""))
        token = base64.b64encode(pair.encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    elif mode == "bearer":
        token = (creds.get("token") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
    elif mode == "header":
        name = (creds.get("header_name") or "").strip()
        value = (creds.get("header_value") or "").strip()
        if name and value:
            headers[name] = value
    return headers


def curl_robust_options():
    """Опции GDAL для устойчивого чтения /vsicurl/ через CDN/Azure.

    Симптом, который лечим: «ReadBlock failed ... TIFFReadEncodedFile() failed»
    при отрисовке COG (обрывы range-запросов). Крупный чанк — меньше запросов,
    ретраи по сетевым кодам, HTTP/1.1 (обход обрывов HTTP/2 у части CDN).
    """
    return {
        "GDAL_HTTP_MAX_RETRY": "8",
        "GDAL_HTTP_RETRY_CODES": "408,429,500,502,503,504",
        "GDAL_HTTP_CONNECTTIMEOUT": "20",
        "GDAL_HTTP_TIMEOUT": "180",
        "GDAL_HTTP_VERSION": "1.1",
        "CPL_VSIL_CURL_CHUNK_SIZE": "1048576",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    }


def gdal_auth_options(creds):
    """Опции GDAL для /vsicurl/ при загрузке защищённых ассетов.

    Возвращает dict конфигурационных опций (пустой, если авторизация не нужна).
    """
    if not creds:
        return {}
    mode = creds.get("mode") or "none"
    opts = {}
    if mode == "basic":
        opts["GDAL_HTTP_AUTH"] = "BASIC"
        opts["GDAL_HTTP_USERPWD"] = "{}:{}".format(creds.get("user", ""), creds.get("password", ""))
    elif mode == "bearer":
        token = (creds.get("token") or "").strip()
        if token:
            opts["GDAL_HTTP_HEADERS"] = "Authorization: Bearer " + token
    elif mode == "header":
        name = (creds.get("header_name") or "").strip()
        value = (creds.get("header_value") or "").strip()
        if name and value:
            opts["GDAL_HTTP_HEADERS"] = "{}: {}".format(name, value)
    return opts


# --------------------------------------------------------------------------
# Фильтры
# --------------------------------------------------------------------------
def _to_rfc3339(date_str, end_of_day=False):
    """'2025-01-01' -> '2025-01-01T00:00:00Z' (CMR требует время); прочие форматы — как есть."""
    s = (date_str or "").strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s + ("T23:59:59Z" if end_of_day else "T00:00:00Z")
    return s


def datetime_param(date_from, date_to):
    """Строка datetime для STAC-поиска: 'from/to', открытые концы допускаются."""
    if not date_from and not date_to:
        return None
    a = _to_rfc3339(date_from) if date_from else ".."
    b = _to_rfc3339(date_to, end_of_day=True) if date_to else ".."
    return "{}/{}".format(a, b)


def bbox_intersects(item_bbox, user_bbox):
    """Пересечение bbox [w,s,e,n] (антимеридиан не обрабатываем специально)."""
    if not item_bbox or not user_bbox:
        return True
    w, s, e, n = user_bbox
    iw, is_, ie, in_ = item_bbox[:4]
    return not (ie < w or iw > e or in_ < s or is_ > n)


def item_datetime(item):
    """ISO-строка даты айтема (properties.datetime или start/end)."""
    props = item.get("properties") or {}
    return props.get("datetime") or props.get("start_datetime") or props.get("end_datetime") or ""


def item_cloud(item):
    props = item.get("properties") or {}
    for key in ("eo:cloud_cover", "cloud_cover"):
        if key in props:
            try:
                return float(props[key])
            except (TypeError, ValueError):
                pass
    return None


def item_bbox(item):
    b = item.get("bbox")
    if isinstance(b, list) and len(b) >= 4:
        return b[:4]
    geom = item.get("geometry") or {}
    if geom.get("type") == "Polygon":
        xs, ys = [], []
        for ring in geom.get("coordinates") or []:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
        if xs:
            return [min(xs), min(ys), max(xs), max(ys)]
    return None


def item_passes(item, user_bbox, dt_pair, max_cloud):
    """Локальный фильтр айтема: bbox, интервал дат, облачность."""
    if not bbox_intersects(item_bbox(item), user_bbox):
        return False
    dt = item_datetime(item)
    if dt_pair and dt:
        d = (dt or "")[:10]
        d_from, d_to = dt_pair
        if d_from and d < d_from:
            return False
        if d_to and d > d_to:
            return False
    if max_cloud is not None and max_cloud < 100:
        cc = item_cloud(item)
        if cc is not None and cc > max_cloud:
            return False
    return True


# --------------------------------------------------------------------------
# STAC API поиск
# --------------------------------------------------------------------------
def search_api(source, user_bbox=None, dt_param=None, collections=None, limit=30,
               creds=None, extra_headers=None, timeout=TIMEOUT):
    """POST {search_url}/search. Возвращает (items, error).

    collections — ограничение набором коллекций (None = весь источник).
    """
    if requests is None:
        return [], "модуль requests недоступен"
    url = source["url"].rstrip("/")
    if not url.endswith("/search"):
        url += "/search"
    body = {"limit": int(limit)}
    if user_bbox:
        body["bbox"] = list(user_bbox)
    if dt_param:
        body["datetime"] = dt_param
    if collections:
        body["collections"] = list(collections)
    headers = build_headers(source, creds)
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - сеть/таймаут/прокси
        return [], str(exc)
    if r.status_code in (403, 405):
        # часть серверов (напр., Digital Earth Australia) разрешает только GET-поиск
        params = {"limit": limit}
        if user_bbox:
            params["bbox"] = ",".join(str(v) for v in user_bbox)
        if dt_param:
            params["datetime"] = dt_param
        if collections:
            params["collections"] = ",".join(collections)
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return [], str(exc)
    if r.status_code != 200:
        return [], "HTTP {}: {}".format(r.status_code, r.text[:160])
    try:
        data = r.json()
    except ValueError:
        return [], "ответ не JSON"
    items = data.get("features") or []
    for it in items:
        absolutize_item_assets(it, item_self_url(it))
    # серверная сортировка неизвестна — стабильно сортируем по дате (свежие сверху)
    items.sort(key=lambda it: item_datetime(it) or "", reverse=True)
    return items, None


def search_cmr_providers(source, providers, user_bbox=None, dt_param=None,
                         collections_by_provider=None, limit=30, creds=None,
                         timeout=TIMEOUT):
    """Поиск по нескольким провайдерам NASA CMR-STAC.

    Возвращает (items, errors) — items объединены и отсортированы по дате.
    """
    all_items, errors = [], []
    for prov in providers:
        sub = dict(source)
        sub["url"] = CMR_PROVIDER_URL.format(prov=prov)
        cols = (collections_by_provider or {}).get(prov)
        # CMR возвращает айтемы без облачности; лимит на провайдера умеренный
        items, err = search_api(sub, user_bbox, dt_param, cols, limit, creds, timeout=timeout)
        if err:
            errors.append("{}: {}".format(prov, err))
        for it in items:
            it["_cmr_provider"] = prov
        all_items.extend(items)
    all_items.sort(key=lambda it: item_datetime(it) or "", reverse=True)
    return all_items, errors


CMR_PROVIDER_URL = "https://cmr.earthdata.nasa.gov/stac/{prov}/search"


# --------------------------------------------------------------------------
# Статические каталоги (Vantor / Maxar / Umbra)
# --------------------------------------------------------------------------
def _get_json(url, creds, timeout=TIMEOUT):
    headers = build_headers(source=None, creds=creds)
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError("HTTP {} {}".format(r.status_code, url[-80:]))
    return r.json()


def _absolutize(base_url, href):
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("s3://"):
        return None
    root = base_url.rsplit("/", 1)[0]
    while href.startswith("../"):
        href = href[3:]
        root = root.rsplit("/", 1)[0]
    return root + "/" + href.lstrip("./")


def absolutize_item_assets(item, base_url):
    """Достраивает href ассетов: пустые (Umbra — имя файла = ключ) и относительные (Maxar).

    base_url — URL, из которого был загружен айтем (или его self-ссылка).
    Ассеты с s3:// и доступным https-вариантом в alternate -> https.
    """
    if not base_url:
        return
    for key, a in (item.get("assets") or {}).items():
        href = a.get("href") or ""
        if not href:
            # имя файла обычно совпадает с ключом ассета
            a["href"] = _absolutize(base_url, "./" + key)
            continue
        if href.startswith("s3://"):
            alt = (a.get("alternate") or {})
            for v in alt.values():
                h = (v or {}).get("href") if isinstance(v, dict) else v
                if isinstance(h, str) and h.startswith("https://"):
                    a["href"] = h
                    break
            else:
                # стандартное отображение s3://bucket/key -> https://bucket.s3.amazonaws.com/key
                parts = href[5:].split("/", 1)
                if len(parts) == 2:
                    a["href"] = "https://{}.s3.amazonaws.com/{}".format(parts[0], parts[1])
            continue
        if not href.startswith("http"):
            a["href"] = _absolutize(base_url, href)


def _iter_children(doc, base_url):
    """Дочерние ссылки каталога/коллекции: item-, child- и self-относительные."""
    for link in doc.get("links") or []:
        rel = link.get("rel")
        if rel in ("child", "item"):
            href = _absolutize(base_url, link.get("href"))
            if href:
                yield rel, href


def walk_static(source, user_bbox=None, dt_pair=None, max_cloud=None,
                creds=None, max_items=40, max_children=100, depth=3, timeout=TIMEOUT):
    """Обход статического каталога в ширину + локальная фильтрация.

    Возвращает (items, warnings). max_children — лимит ПОСЕЩЁННЫХ подкаталогов.
    """
    if requests is None:
        return [], "модуль requests недоступен"
    items, warnings = [], []
    queue = [(source["url"], 0)]
    seen = set()
    visited_children = 0
    truncated = False
    while queue and len(items) < max_items:
        url, lvl = queue.pop(0)
        if url in seen or lvl > depth:
            continue
        if url != source["url"]:  # лимит считаем по посещённым подкаталогам
            visited_children += 1
            if visited_children > max_children:
                truncated = True
                break
        seen.add(url)
        try:
            doc = _get_json(url, creds, timeout)
        except Exception as exc:  # noqa: BLE001
            warnings.append(str(exc))
            continue
        doc_type = doc.get("type")
        if doc_type == "Feature":  # айтем
            absolutize_item_assets(doc, url)
            if item_passes(doc, user_bbox, dt_pair, max_cloud):
                items.append(doc)
            continue
        # каталог или коллекция: идём вглубь
        for rel, href in _iter_children(doc, url):
            if rel == "item":
                if len(items) >= max_items:
                    break
                try:
                    it = _get_json(href, creds, timeout)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(str(exc))
                    continue
                absolutize_item_assets(it, href)
                if item_passes(it, user_bbox, dt_pair, max_cloud):
                    items.append(it)
            else:
                queue.append((href, lvl + 1))
        if truncated:
            break
    if truncated:
        warnings.append("обход остановлен по лимиту подкаталогов ({})".format(max_children))
    return items, warnings


# --------------------------------------------------------------------------
# Ассеты
# --------------------------------------------------------------------------
def _asset_roles(asset):
    return set(asset.get("roles") or [])


def _is_raster_asset(key, asset):
    mime = (asset.get("type") or "").lower()
    href = (asset.get("href") or "").lower()
    roles = _asset_roles(asset)
    low_key = key.lower()
    if "thumbnail" in roles or "metadata" in roles:
        return False
    if any(low_key.endswith(ext) or href.endswith(ext) for ext in DENY_EXT):
        return False
    if any(ext in href for ext in SKIP_EXT):
        return False
    if mime and any(h in mime for h in IMAGE_MIME_HINTS):
        return True
    if not mime and any(href.endswith(ext) or low_key.endswith(ext) for ext in (".tif", ".tiff", ".jp2")):
        return True
    return "data" in roles or "visual" in roles or "cog" in roles


def pick_asset(item):
    """Лучший растровый ассет айтема: (ключ, asset) или (None, None).

    Приоритет: visual > data с image/tiff > прочие растровые.
    """
    assets = item.get("assets") or {}
    raster = [(k, a) for k, a in assets.items() if _is_raster_asset(k, a)]
    if not raster:
        return None, None

    def rank(pair):
        key, a = pair
        roles = _asset_roles(a)
        score = 0
        if "visual" in roles or key.lower() in ("visual", "rgb"):
            score -= 3
        if "data" in roles:
            score -= 2
        mime = (a.get("type") or "").lower()
        if "tiff" in mime:
            score -= 1
        href = (a.get("href") or "")
        if "/vsicurl" not in href and href.startswith("s3://"):
            score += 2  # s3-ссылки без https бесполезны для стриминга
        return score

    raster.sort(key=rank)
    return raster[0]


def pick_thumbnail(item):
    """URL миниатюры или None."""
    assets = item.get("assets") or {}
    for key, a in assets.items():
        roles = _asset_roles(a)
        mime = (a.get("type") or "").lower()
        if "thumbnail" in roles or mime.startswith("image/jpeg") or mime.startswith("image/png") \
                or key.lower() in ("thumbnail", "rendered_preview"):
            href = a.get("href") or ""
            if href.startswith("http"):
                return href
    return None


# --------------------------------------------------------------------------
# SAS-подпись ссылок Microsoft Planetary Computer
# --------------------------------------------------------------------------
MPC_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{}"
_SAS_CACHE = {}          # коллекция -> (token, время получения)
_SAS_LOCK = threading.Lock()
_SAS_TTL = 45 * 60       # реальный срок жизни ~1 ч, обновляем заранее


def mpc_needs_signing(href):
    """True для ссылок на хранилища MPC (Azure blob) без подписи (409 без токена)."""
    h = (href or "").lower()
    return "core.windows.net" in h or "azureedge.net" in h


def get_mpc_token(collection, timeout=15):
    """SAS-токен коллекции MPC (анонимно, кэш 45 мин). '' при ошибке."""
    if not collection:
        return ""
    now = time.time()
    with _SAS_LOCK:
        hit = _SAS_CACHE.get(collection)
        if hit and now - hit[1] < _SAS_TTL:
            return hit[0]
    token = ""
    if requests is not None:
        try:
            r = requests.get(MPC_SAS_URL.format(collection),
                             headers={"User-Agent": USER_AGENT}, timeout=timeout)
            if r.status_code == 200:
                token = (r.json() or {}).get("token") or ""
        except Exception:  # noqa: BLE001 - сеть/таймаут
            token = ""
    if token:
        with _SAS_LOCK:
            _SAS_CACHE[collection] = (token, now)
    return token


def sign_url(href, collection):
    """Добавляет SAS-подпись MPC к blob-ссылкам; остальные — без изменений."""
    if not href or not mpc_needs_signing(href):
        return href
    token = get_mpc_token(collection)
    if not token:
        return href
    return href + ("&" if "?" in href else "?") + token


def sign_item_assets(item, collection=None):
    """Подписывает href ассетов айтема MPC (миниатюры/COG без подписи отдают 409)."""
    coll = collection or item.get("collection") or ""
    for a in (item.get("assets") or {}).values():
        href = a.get("href") or ""
        if mpc_needs_signing(href):
            a["href"] = sign_url(href, coll)


def asset_bands(asset):
    """Список common_name каналов ассета из eo:bands."""
    out = []
    for b in asset.get("eo:bands") or []:
        if isinstance(b, dict):
            nm = b.get("common_name") or b.get("name") or ""
            if nm:
                out.append(str(nm))
    return out


def raster_asset_map(item):
    """Растровые ассеты айтема: {ключ: {href, type, roles, bands}}.

    Используется для пресетов синтеза (render_presets): содержит все
    канальные ассеты (B04, B08, sr_b5...), а не только выбранный pick_asset.
    """
    out = {}
    for k, a in (item.get("assets") or {}).items():
        bands = asset_bands(a)
        if bands or _is_raster_asset(k, a):
            out[k] = {
                "href": a.get("href") or "",
                "type": a.get("type") or "",
                "roles": list(a.get("roles") or []),
                "bands": bands,
            }
    return out


def item_gsd(item):
    props = item.get("properties") or {}
    for key in ("gsd", "eo:gsd", "pan_gsd"):
        if key in props:
            try:
                return float(props[key])
            except (TypeError, ValueError):
                pass
    return None


def item_self_url(item):
    for link in item.get("links") or []:
        if link.get("rel") == "self" and (link.get("href") or "").startswith("http"):
            return link["href"]
    return ""


def normalize(item, source, provider=None):
    """Айтем -> dict для таблицы результатов UI."""
    key, asset = pick_asset(item)
    prov = provider or item.pop("_cmr_provider", None)
    operator = source["operator"]
    src_name = source["name"] + (" · " + prov if prov else "")
    return {
        "item_id": item.get("id", ""),
        "collection": item.get("collection", ""),
        "date": (item_datetime(item) or "")[:10],
        "cloud": item_cloud(item),
        "gsd": item_gsd(item),
        "source_id": source["id"],
        "source_name": src_name,
        "operator": operator,
        "license": source.get("license", ""),
        "access": source.get("access", ""),
        "asset_key": key,
        "asset_url": (asset or {}).get("href") or "",
        "asset_mime": (asset or {}).get("type") or "",
        "thumb_url": pick_thumbnail(item),
        "self_url": item_self_url(item),
        "assets": raster_asset_map(item),
    }


def vsicurl_url(http_url):
    """HTTPS-ссылка -> GDAL /vsicurl/ (стриминг COG без скачивания)."""
    if http_url.startswith("/vsicurl/"):
        return http_url
    return "/vsicurl/" + http_url


def fetch_thumbnail(url, creds=None, max_bytes=None, timeout=THUMB_TIMEOUT):
    """Байты миниатюры (<=2 МБ) или None. Ограничение по времени жёсткое."""
    if requests is None or not url:
        return None
    max_bytes = max_bytes or THUMB_MAX_BYTES
    try:
        r = _thumb_session().get(url, headers=build_headers(None, creds),
                                 timeout=(5, timeout), stream=True)
        if r.status_code != 200:
            return None
        total = int(r.headers.get("content-length") or 0)
        if total > max_bytes:
            return None
        buf = b""
        for chunk in r.iter_content(131072):
            buf += chunk
            if len(buf) > max_bytes:
                return None
        return buf or None
    except Exception:  # noqa: BLE001
        return None
