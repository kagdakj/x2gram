"""Plugins module for Instagram Auto Uploader."""

from .base import BasePlugin, PluginManager, register_plugin
from .anime_crop import AnimeCropPlugin
from .caption import CaptionPlugin
from .watermark import WatermarkPlugin

__all__ = [
    "BasePlugin",
    "PluginManager",
    "register_plugin",
    "AnimeCropPlugin",
    "CaptionPlugin",
    "WatermarkPlugin",
]
