# STAC Hub — открытые геоданные в QGIS

**STAC Hub** — плагин для QGIS 3.22–3.4x: единая точка доступа к открытым геоданным по протоколу [STAC](https://stacspec.org) (SpatioTemporal Asset Catalog). Один запрос расходится сразу по 12 проверенным каталогам, включая 76 флагманских наборов NASA CMR-STAC.

Группировка источников — по категориям (космоснимки, рельеф, климат, океан, лёд, SAR, события…) и по охвату (глобальный / региональный), у каждого слоя указано происхождение: оператор, каталог, лицензия.

## Возможности

- **12 каталогов + 76 наборов NASA** в одном дереве источников, с чекбоксами;
- **глобальный поиск** сразу по всем отмеченным источникам: BBox, интервал дат, максимум облачности, лимит на источник;
- **COG-стриминг**: растр добавляется в проект через GDAL `/vsicurl/` — без скачивания файла целиком (Cloud-Optimized GeoTIFF, HTTP range-запросы);
- **авторизация там, где нужна**: Basic / Bearer / произвольный заголовок (NASA Earthdata Login, Copernicus Data Space, Terrascope, Planetary Computer), с запоминанием в настройках QGIS по желанию;
- **миниатюры** снимков в результатах (64 px);
- **провенанс**: для каждого результата видно, откуда данные (библиотека → оператор → лицензия).

## Источники

| Каталог | Что внутри | Охват | Авторизация |
|---|---|---|---|
| EarthSearch (Element 84 / AWS) | Sentinel-2 L2A COG, Landsat C2, NAIP, Copernicus DEM, Sentinel-1 RTC | Глобальный | не требуется |
| USGS LandsatLook | Landsat 4–9 C2 L1/L2, ARD, DSWE, fSCA | Глобальный | не требуется |
| Copernicus Data Space (ESA) | Sentinel-1/2/3/5P, CLMS | Глобальный | Bearer-токен для загрузок |
| Terrascope (VITO) | Sentinel, PROBA-V, GRASS NDVI | Глобальный (фокус — Европа) | логин/пароль |
| Planetary Computer (Microsoft) | ~136 коллекций: DEM, ERA5, LULC, климат | Глобальный | ключ API (опционально) |
| NASA CMR-STAC (61 провайдер) | HLS, EMIT, GEDI, ECOSTRESS, SRTM/NASADEM, MODIS/VIIRS, IMERG, MERRA-2, OCO-2/3, ICESat-2, GRACE-FO, OPERA, Black Marble… | Глобальный | Earthdata Login (открытые наборы — без него) |
| Vantor Open Data (бывш. Maxar) | катастрофы 2026, WorldView-2/3, 0.3–0.6 м, CC BY-NC 4.0 | Мир (события) | не требуется |
| Maxar Open Data (архив) | катастрофы 2023–2025, 57 событий | Мир (события) | не требуется |
| Umbra Open Data | SAR X-band, 25 см, открытые сюжеты | Мир (точки интереса) | не требуется |
| Digital Earth Africa | Sentinel/Landsat LS, CHIRPS, WOfS, GeoMAD | Африка | не требуется |
| Digital Earth Australia | Sentinel-2/Landsat ARD, Fractional Cover, Coastlines | Австралия | не требуется |
| Brazil Data Cube (INPE) | кубы данных Sentinel-2/Landsat, CBERS | Бразилия | токен BDC (опционально) |

Проверка источников выполнена живыми запросами (поиск, ассеты, COG-стриминг) 09.09.2026.

## Установка

**Из ZIP:**
1. Скачайте `STAC_Hub_qgis_plugin.zip` (см. [Releases](https://github.com/nadiopt-cell/stac_hub/releases) или соберите сами: `cd stac_hub && zip -r ../STAC_Hub_qgis_plugin.zip .`).
2. QGIS → «Модули» → «Управление модулями…» → «Установить из ZIP».
3. Включите «Показывать экспериментальные модули» в настройках менеджера модулей.
4. Появится кнопка STAC Hub на веб-панели инструментов и пункт меню **Web → STAC Hub**.

**Из git-клона** — скопируйте папку `stac_hub/` в профиль QGIS:
- Windows: `%AppData%\QGIS\QGIS3\profiles\default\python\plugins\`
- Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
- macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`

## Как пользоваться

1. **«Взять экстент карты»** — подставить текущий вид в BBox (или задайте его руками; «Весь мир» — снять ограничение).
2. При необходимости задайте интервал дат, максимум облачности, лимит на источник.
3. Отметьте нужные источники в дереве (переключатель группировки: **по категориям** или **по охвату**).
4. **«Найти»** — запрос расходится по всем отмеченным источникам параллельно.
5. **Двойной клик** по результату — слой добавляется в проект через `/vsicurl/` (COG-стриминг, без скачивания файла). **Правый клик** — копировать ссылку COG, открыть STAC-айтем в браузере, посмотреть провенанс.

## Учётные данные

Кнопка **«Учётные данные…»**:
- **NASA CMR-STAC** — бесплатный [Earthdata Login](https://urs.earthdata.nasa.gov): Basic (логин/пароль) или Bearer-токен LP DAAC;
- **Copernicus Data Space** — Bearer-токен с account.dataspace.copernicus.eu;
- **Terrascope** — логин/пароль бесплатного аккаунта;
- **Planetary Computer** — ключ API в заголовке `Ocp-Apim-Subscription-Key` (режим «Заголовок»).

Галочка «Запомнить» сохраняет данные в настройках QGIS (QSettings, без шифрования). Без неё данные живут до закрытия QGIS.

## Особенности и оговорки

- Авторизация для загрузки применяется через GDAL-конфигурацию (`GDAL_HTTP_AUTH`, `GDAL_HTTP_USERPWD`, `GDAL_HTTP_HEADERS`) и действует в пределах сессии QGIS.
- **DE Africa**: эндпоинт `/search` периодически недоступен на стороне сервера — источник оставлен, ошибки поиска показываются в строке статуса.
- **Planetary Computer**: без ключа поиск ограничен, ассеты отдаются по подписанным SAS-ссылкам.
- Статические каталоги (Vantor/Maxar/Umbra) обходятся локально: поиск идёт по подкаталогам с лимитами, поэтому большие каталоги могут отдавать не все совпадения.
- CMR-STAC ищет по 9 ключевым провайдерам, если не отмечены конкретные коллекции; при отмеченных коллекциях — только по их провайдерам.

## Архитектура

```
stac_hub/
├── metadata.txt     # манифест плагина QGIS
├── __init__.py      # classFactory
├── plugin.py        # интеграция с QGIS: тулбар, меню Web, dock-панель
├── dialog.py        # UI: дерево источников, поиск, результаты, потоки
├── stac_client.py   # ядро (без QGIS): STAC API, статические каталоги, /vsicurl/
├── sources.py       # реестр 12 источников + 76 наборов NASA
├── auth_store.py    # хранение учётных данных (QSettings)
└── icons/           # иконка
```

Ядро `stac_client.py` не зависит от QGIS и покрыто интеграционными тестами против живых эндпоинтов (28 проверок: POST-поиск + GET-фолбэк, BFS по статическим каталогам, нормализация href — пустые `href` Umbra, относительные ссылки Maxar, `s3://` → `https://`).

## Лицензия

Код плагина — [MIT](LICENSE). Данные каждого каталога распространяются на условиях его владельца (например, Vantor/Maxar Open Data — [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)) — проверяйте лицензию соответствующего источника перед использованием.
