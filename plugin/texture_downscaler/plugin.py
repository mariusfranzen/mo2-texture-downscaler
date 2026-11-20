"""MO2 glue: the IPluginTool that collects enabled mods and opens the main dialog."""

from __future__ import annotations

from pathlib import Path

import mobase
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox

from .core.downscaler import TEXCONV_PATH, default_workers
from .core.output import Manifest, RunMode
from .core.scanner import LooseModSource, TextureSource
from .ui.main_dialog import MainDialog

DEFAULT_OUTPUT_NAME = "Texture Downscaler Output"


class TextureDownscalerTool(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None

    # ---- IPlugin ---------------------------------------------------------------------------

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self) -> str:
        return "Texture Downscaler"

    def author(self) -> str:
        return "Marius"

    def description(self) -> str:
        return (
            "Finds the highest-resolution textures in your enabled mods and downscales the ones "
            "you choose into a separate output mod."
        )

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(0, 1, 0, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting(
                "output_mod_name", "Name of the mod that receives downscaled textures", DEFAULT_OUTPUT_NAME
            ),
            mobase.PluginSetting("workers", "Parallel texconv processes (0 = automatic)", 0),
        ]

    def requirements(self):
        return []

    # ---- IPluginTool -----------------------------------------------------------------------

    def displayName(self) -> str:
        return "Texture Downscaler"

    def tooltip(self) -> str:
        return "Find and downscale high-resolution textures"

    def icon(self) -> QIcon:
        return QIcon()

    def display(self) -> None:
        org = self._organizer
        parent = self._parentWidget()
        if not TEXCONV_PATH.is_file():
            QMessageBox.critical(parent, "Texture Downscaler", f"texconv.exe is missing:\n{TEXCONV_PATH}")
            return

        output_name = str(org.pluginSetting(self.name(), "output_mod_name") or DEFAULT_OUTPUT_NAME).strip()
        output_dir = Path(org.modsPath()) / output_name

        mode = self._ask_run_mode(output_name, output_dir)
        if mode is None:
            return

        try:
            workers = int(org.pluginSetting(self.name(), "workers") or 0)
        except (TypeError, ValueError):
            workers = 0

        dialog = MainDialog(
            sources=self._collect_sources(output_name),
            output_dir=output_dir,
            output_name=output_name,
            mode=mode,
            workers=workers or default_workers(),
            ensure_output_mod=lambda: self._ensure_output_mod(output_name),
            on_output_written=lambda: self._on_output_written(output_name),
            parent=parent,
        )
        dialog.exec()

    # ---- helpers ---------------------------------------------------------------------------

    def _ask_run_mode(self, output_name: str, output_dir: Path) -> RunMode | None:
        manifest = Manifest.load(output_dir)
        if not manifest.entries:
            return RunMode.SKIP_DONE
        box = QMessageBox(self._parentWidget())
        box.setWindowTitle("Texture Downscaler")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"The output mod '{output_name}' already contains {len(manifest.entries)} downscaled textures "
            "from a previous run.\n\nWhat do you want to do?"
        )
        box.setInformativeText(
            "Keep: already downscaled textures are skipped and pre-selected with their previous target.\n"
            "Rebuild: previous outputs are deleted and everything you select is downscaled again."
        )
        keep = box.addButton("Keep && skip already done", QMessageBox.ButtonRole.AcceptRole)
        rebuild = box.addButton("Rebuild from scratch", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            return RunMode.SKIP_DONE
        if clicked is rebuild:
            return RunMode.REBUILD
        return None

    def _collect_sources(self, output_name: str) -> list[TextureSource]:
        """Enabled, non-separator, MO2-managed mods in ascending priority (last one wins)."""
        mod_list = self._organizer.modList()
        sources: list[TextureSource] = []
        for name in mod_list.allModsByProfilePriority():
            if name == output_name or not (mod_list.state(name) & mobase.ModState.ACTIVE):
                continue
            mod = mod_list.getMod(name)
            if mod is None or mod.isSeparator() or mod.isForeign():
                continue
            sources.append(LooseModSource(name, Path(mod.absolutePath())))
        return sources

    def _ensure_output_mod(self, output_name: str) -> Path | None:
        """Return the output mod folder, creating the mod (enabled, last priority) if needed."""
        org = self._organizer
        mod_list = org.modList()
        mod = mod_list.getMod(output_name)
        if mod is None:
            mod = org.createMod(mobase.GuessedString(output_name))
            if mod is None:
                QMessageBox.critical(
                    self._parentWidget(), "Texture Downscaler", f"Could not create the mod '{output_name}'."
                )
                return None
            try:
                mod_list.setActive(output_name, True)
                mod_list.setPriority(output_name, len(mod_list.allMods()) - 1)
            except Exception:
                pass  # user can enable/move it manually; the end-of-run message says so
        return Path(mod.absolutePath())

    def _on_output_written(self, output_name: str) -> None:
        mod = self._organizer.modList().getMod(output_name)
        if mod is not None:
            self._organizer.modDataChanged(mod)
