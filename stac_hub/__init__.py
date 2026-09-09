# -*- coding: utf-8 -*-
"""Точка входа плагина STAC Hub для QGIS."""


def classFactory(iface):
    from .plugin import StacHubPlugin
    return StacHubPlugin(iface)
