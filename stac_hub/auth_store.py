# -*- coding: utf-8 -*-
"""Хранение учётных данных источников STAC Hub.

Пароли/токены хранятся в QSettings (реестр/конфиг QGIS). Режим «запомнить»
сохраняет их между сессиями; без него — только в памяти на время работы QGIS.
QSettings у QGIS шифрования не выполняет — предупредите пользователей в справке.
"""

from qgis.PyQt.QtCore import QSettings

_ORG = "STACHub"
_APP = "plugin"

# ключ -> (подпись для UI)
CREDENTIAL_SOURCES = [
    ("cmrstac", "NASA CMR-STAC — Earthdata Login (бесплатно): urs.earthdata.nasa.gov"),
    ("cdse", "Copernicus Data Space — Bearer-токен (account.dataspace.copernicus.eu)"),
    ("terrascope", "Terrascope (VITO) — бесплатный аккаунт terrascope.be"),
    ("planetarycomputer", "Planetary Computer — ключ в заголовке Ocp-Apim-Subscription-Key"),
    ("landsatlook", "USGS LandsatLook — обычно не требуется"),
]


class AuthStore(object):
    """Сессия + постоянное хранилище учётных данных по id источника."""

    def __init__(self):
        self._memory = {}  # source_id -> creds dict (без записи на диск)

    def get(self, source_id):
        """Учётные данные источника: dict или None."""
        if source_id in self._memory:
            return self._memory[source_id]
        s = QSettings(_ORG, _APP)
        mode = s.value("creds/{}/mode".format(source_id), "")
        if not mode or mode == "none":
            return None
        return {
            "mode": mode,
            "user": s.value("creds/{}/user".format(source_id), ""),
            "password": s.value("creds/{}/password".format(source_id), ""),
            "token": s.value("creds/{}/token".format(source_id), ""),
            "header_name": s.value("creds/{}/header_name".format(source_id), ""),
            "header_value": s.value("creds/{}/header_value".format(source_id), ""),
        }

    def set(self, source_id, creds, remember=False):
        """Сохранить учётные данные: в память всегда, на диск — по remember."""
        if creds and creds.get("mode") not in (None, "", "none"):
            self._memory[source_id] = dict(creds)
        else:
            self._memory.pop(source_id, None)
        s = QSettings(_ORG, _APP)
        s.beginGroup("creds/{}".format(source_id))
        if remember and creds and creds.get("mode") not in (None, "", "none"):
            s.setValue("mode", creds.get("mode", "none"))
            s.setValue("user", creds.get("user", ""))
            s.setValue("password", creds.get("password", ""))
            s.setValue("token", creds.get("token", ""))
            s.setValue("header_name", creds.get("header_name", ""))
            s.setValue("header_value", creds.get("header_value", ""))
        else:
            s.remove("")
        s.endGroup()

    def clear(self, source_id):
        self._memory.pop(source_id, None)
        s = QSettings(_ORG, _APP)
        s.remove("creds/{}".format(source_id))
