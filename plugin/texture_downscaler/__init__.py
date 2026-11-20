"""Texture Downscaler - a Mod Organizer 2 tool plugin.

Copy (or link) this folder into MO2's `plugins` directory.
"""

__version__ = "1.0.1"


def createPlugin():
    from .plugin import TextureDownscalerTool

    return TextureDownscalerTool()
