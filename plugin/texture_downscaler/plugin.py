"""MO2 glue: the IPluginTool that collects enabled mods and opens the main dialog."""

from __future__ import annotations

import json
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
        self._output_created = False

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
            mobase.PluginSetting("hidden_mods", "Mods hidden in the texture list (JSON list)", "[]"),
            mobase.PluginSetting("sort_state", "Table sort order (JSON)", "{}"),
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
            hidden_mods=self._load_hidden_mods(),
            on_hidden_mods_changed=self._save_hidden_mods,
            header_cache_path=self._cache_path(),
            sort_state=self._load_json_setting("sort_state", dict),
            on_sort_changed=lambda state: self._save_json_setting("sort_state", state),
            parent=parent,
        )
        dialog.exec()

    # ---- helpers ---------------------------------------------------------------------------

    def _cache_path(self) -> Path:
        try:
            base = Path(self._organizer.pluginDataPath())
        except Exception:
            base = Path(self._organizer.basePath()) / "plugins" / "data"
        return base / "texture_downscaler" / "header_cache.json"

    def _load_json_setting(self, key: str, expected: type):
        try:
            value = json.loads(str(self._organizer.pluginSetting(self.name(), key) or ""))
        except ValueError:
            return expected()
        return value if isinstance(value, expected) else expected()

    def _save_json_setting(self, key: str, value) -> None:
        self._organizer.setPluginSetting(self.name(), key, json.dumps(value, ensure_ascii=False))

    def _load_hidden_mods(self) -> set[str]:
        raw = self._organizer.pluginSetting(self.name(), "hidden_mods")
        try:
            value = json.loads(str(raw or "[]"))
        except ValueError:
            return set()
        return {str(m) for m in value} if isinstance(value, list) else set()

    def _save_hidden_mods(self, mods: set[str]) -> None:
        self._organizer.setPluginSetting(
            self.name(), "hidden_mods", json.dumps(sorted(mods, key=str.lower), ensure_ascii=False)
        )

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
            # MO2 only lists the new mod after a refresh; that happens in _on_output_written.
            self._output_created = True
        return Path(mod.absolutePath())

    def _on_output_written(self, output_name: str) -> None:
        org = self._organizer
        if not self._output_created:
            mod = org.modList().getMod(output_name)
            if mod is not None:
                org.modDataChanged(mod)
            return

        # Freshly created mod: refresh so MO2 shows it, then enable it and move it to the bottom.
        self._output_created = False

        def activate() -> None:
            mod_list = org.modList()
            if mod_list.getMod(output_name) is None:
                return
            try:
                mod_list.setActive(output_name, True)
                mod_list.setPriority(output_name, len(mod_list.allMods()) - 1)
            except Exception:
                pass  # the end-of-run message tells the user to enable/move it

        try:
            org.onNextRefresh(activate, False)
        except Exception:
            org.refresh()
            activate()
            return
        org.refresh()
