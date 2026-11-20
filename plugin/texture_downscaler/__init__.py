"""Texture Downscaler - a Mod Organizer 2 tool plugin.

Copy (or link) this folder into MO2's `plugins` directory.
"""

__version__ = "0.2.0"


def createPlugin():
    from .plugin import TextureDownscalerTool

    return TextureDownscalerTool()
