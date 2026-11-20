"""Regenerate the plugin icon from the logo: python scripts/make_icon.py (needs PyQt6)."""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "logo.png"
TARGET = ROOT / "plugin" / "texture_downscaler" / "icon.png"
SIZE = 256

image = QImage(str(SOURCE))
if image.isNull():
    raise SystemExit(f"Could not read {SOURCE}")
icon = image.scaled(
    SIZE, SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
)
if not icon.save(str(TARGET)):
    raise SystemExit(f"Could not write {TARGET}")
print(f"Wrote {TARGET} ({icon.width()}x{icon.height()}, {TARGET.stat().st_size // 1024} KB)")
