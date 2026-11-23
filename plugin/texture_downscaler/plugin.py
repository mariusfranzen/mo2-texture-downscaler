"""MO2 glue: the IPluginTool that collects enabled mods and opens the main dialog."""

from __future__ import annotations

import configparser
import json
from pathlib import Path

import mobase
from PyQt6.QtCore import QLocale
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox

from . import ICON_PATH, __version__
from .core import i18n
from .core.downscaler import TEXCONV_PATH, default_workers
from .core.i18n import tr
from .core.nexus import NEXUS_ENABLED, NexusRef, game_domain, read_mo2_api_key
from .core.output import Manifest, RunMode
from .core.scanner import LooseModSource, TextureSource
from .ui.main_dialog import MainDialog

DEFAULT_OUTPUT_NAME = "Texture Downscaler Output"


class TextureDownscalerTool(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None
        self._output_created = False
        self._manual_api_key: str | None = None

    # ---- IPlugin ---------------------------------------------------------------------------

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        self._load_language()
        return True

    def _load_language(self) -> None:
        """Language: the `language` setting, else MO2's interface language, else Windows', else English."""
        try:
            setting = str(self._organizer.pluginSetting(self.name(), "language") or "")
        except Exception:
            setting = ""
        candidates: list[str] = []
        try:
            ini = configparser.ConfigParser(interpolation=None, strict=False)
            ini.read(Path(self._organizer.basePath()) / "ModOrganizer.ini", encoding="utf-8")
            if mo2_language := ini.get("Settings", "language", fallback=""):
                candidates.append(mo2_language)
        except Exception:
            pass
        try:
            candidates.extend(QLocale.system().uiLanguages())
        except Exception:
            pass
        i18n.load(i18n.resolve_language(setting, candidates))

    def name(self) -> str:
        return "Texture Downscaler"

    def author(self) -> str:
        return "Bitbyte"

    def description(self) -> str:
        return tr("plugin.description")

    def version(self) -> mobase.VersionInfo:
        major, minor, patch = (int(x) for x in __version__.split("."))
        return mobase.VersionInfo(major, minor, patch, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("output_mod_name", tr("setting.output_mod_name"), DEFAULT_OUTPUT_NAME),
            mobase.PluginSetting("workers", tr("setting.workers"), 0),
            mobase.PluginSetting("language", tr("setting.language"), ""),
            mobase.PluginSetting("hidden_mods", tr("setting.hidden_mods"), "[]"),
            mobase.PluginSetting("sort_state", tr("setting.sort_state"), "{}"),
        ] + (self._nexus_settings() if NEXUS_ENABLED else [])

    def _nexus_settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("author_lookup", tr("setting.author_lookup"), "ask"),
            mobase.PluginSetting("hidden_authors", tr("setting.hidden_authors"), "[]"),
            mobase.PluginSetting("nexus_api_key", tr("setting.nexus_api_key"), ""),
        ]

    def requirements(self):
        return []

    # ---- IPluginTool -----------------------------------------------------------------------

    def displayName(self) -> str:
        return "Texture Downscaler"

    def tooltip(self) -> str:
        return tr("plugin.tooltip")

    def icon(self) -> QIcon:
        return QIcon(str(ICON_PATH)) if ICON_PATH.is_file() else QIcon()

    def display(self) -> None:
        org = self._organizer
        parent = self._parentWidget()
        if not TEXCONV_PATH.is_file():
            QMessageBox.critical(
                parent, "Texture Downscaler", tr("plugin.texconv_missing", path=TEXCONV_PATH)
            )
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

        sources = self._collect_sources(output_name)
        dialog = MainDialog(
            sources=sources,
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
            **self._author_dialog_args(sources),
        )
        dialog.exec()

    def _author_dialog_args(self, sources: list[TextureSource]) -> dict:
        """Dialog arguments for the Nexus author column; everything off while NEXUS_ENABLED is False."""
        if not NEXUS_ENABLED:
            return {"authors_enabled": False}
        # Read on the main thread; the lookup thread must not call into MO2.
        self._manual_api_key = (
            str(self._organizer.pluginSetting(self.name(), "nexus_api_key") or "").strip() or None
        )
        mod_refs = self._nexus_refs([s.name for s in sources])
        return {
            "auto_fetch_authors": self._author_lookup_enabled(mod_refs),
            "mod_refs": mod_refs,
            "author_cache_path": self._cache_path().with_name("nexus_authors.json"),
            "api_key_provider": self._api_key,
            "hidden_authors": set(self._load_json_setting("hidden_authors", list)),
            "on_hidden_authors_changed": lambda authors: self._save_json_setting(
                "hidden_authors", sorted(authors, key=str.lower)
            ),
        }

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
        box.setText(tr("run_mode.text", count=len(manifest.entries), mod=output_name))
        box.setInformativeText(tr("run_mode.info"))
        keep = box.addButton(tr("run_mode.keep"), QMessageBox.ButtonRole.AcceptRole)
        rebuild = box.addButton(tr("run_mode.rebuild"), QMessageBox.ButtonRole.DestructiveRole)
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

    def _author_lookup_enabled(self, mod_refs: dict[str, NexusRef | None]) -> bool:
        """Return whether to look up authors automatically, asking once if the user hasn't decided yet.

        Either way the user can fetch authors on demand from the table's context menu.
        """
        setting = str(self._organizer.pluginSetting(self.name(), "author_lookup") or "ask").strip().lower()
        if setting in ("yes", "true", "1", "on"):
            return True
        if setting in ("no", "false", "0", "off"):
            return False
        pages = len({r.key for r in mod_refs.values() if r is not None})
        answer = QMessageBox.question(
            self._parentWidget(), tr("authors_prompt.title"), tr("authors_prompt.text", count=pages)
        )
        enabled = answer == QMessageBox.StandardButton.Yes
        self._organizer.setPluginSetting(self.name(), "author_lookup", "yes" if enabled else "no")
        return enabled

    def _nexus_refs(self, mod_names: list[str]) -> dict[str, NexusRef | None]:
        """Mod name -> Nexus mod page (game domain + mod id), or None when it isn't a Nexus mod."""
        mod_list = self._organizer.modList()
        domains: dict[str, str | None] = {}
        refs: dict[str, NexusRef | None] = {}
        for name in mod_names:
            ref = None
            try:
                mod = mod_list.getMod(name)
                mod_id = int(mod.nexusId()) if mod is not None else 0
                if mod_id > 0:
                    game = mod.gameName()
                    if game not in domains:
                        domains[game] = self._game_domain(game)
                    if domains[game]:
                        ref = NexusRef(domains[game], mod_id)
            except Exception:
                ref = None
            refs[name] = ref
        return refs

    def _game_domain(self, game_name: str) -> str | None:
        try:
            game = self._organizer.getGame(game_name)
            if game is not None and game.gameNexusName():
                return game.gameNexusName()
        except Exception:
            pass
        return game_domain(game_name)

    def _api_key(self) -> str | None:
        """Called from the lookup thread. A key in the plugin settings wins over MO2's stored key."""
        return self._manual_api_key or read_mo2_api_key()

    def _ensure_output_mod(self, output_name: str) -> Path | None:
        """Return the output mod folder, creating the mod (enabled, last priority) if needed."""
        org = self._organizer
        mod_list = org.modList()
        mod = mod_list.getMod(output_name)
        if mod is None:
            mod = org.createMod(mobase.GuessedString(output_name))
            if mod is None:
                QMessageBox.critical(
                    self._parentWidget(),
                    "Texture Downscaler",
                    tr("plugin.create_mod_failed", mod=output_name),
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
