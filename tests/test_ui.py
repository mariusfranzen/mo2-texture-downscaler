"""Offscreen smoke tests for the Qt dialog and the MO2 glue (with a stub `mobase`)."""

import enum
import os
import sys
import time
import types
from pathlib import Path

import pytest

from helpers import write_fake_dds, write_rgba_dds

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from texture_downscaler.core.output import Manifest, RunMode  # noqa: E402
from texture_downscaler.core.scanner import LooseModSource  # noqa: E402
from texture_downscaler.core.texture_types import TextureType  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _wait(app, cond, timeout=20.0):
    end = time.time() + timeout
    while not cond():
        app.processEvents()
        assert time.time() < end, "timed out"
        time.sleep(0.01)


def _make_mods(tmp_path: Path):
    a = tmp_path / "mods" / "Mod A"
    write_rgba_dds(a / "textures" / "a" / "wall.dds", 128, 128)
    write_rgba_dds(a / "textures" / "a" / "wall_n.dds", 128, 64)
    write_fake_dds(a / "textures" / "a" / "tiny.dds", 16, 16)
    return [LooseModSource("Mod A", a)]


def _dialog(app, tmp_path, mode=RunMode.SKIP_DONE):
    from texture_downscaler.ui.main_dialog import MainDialog

    out = tmp_path / "mods" / "Output"
    written = []
    dlg = MainDialog(
        sources=_make_mods(tmp_path),
        output_dir=out,
        output_name="Output",
        mode=mode,
        workers=2,
        ensure_output_mod=lambda: out,
        on_output_written=lambda: written.append(True),
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    return dlg, out, written


def test_dialog_scan_filter_bulk_and_run(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    dlg, out, written = _dialog(app, tmp_path)

    assert len(dlg.model.entries) == 3
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))
    assert dlg.proxy.rowCount() == 3
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(512))
    assert dlg.proxy.rowCount() == 0  # everything is smaller than 512
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))

    dlg.type_combo.setCurrentIndex(dlg.type_combo.findData(TextureType.NORMAL))
    assert dlg.proxy.rowCount() == 1
    dlg.type_combo.setCurrentIndex(0)

    rows = [i for i, e in enumerate(dlg.model.entries) if e.key.endswith("wall.dds")]
    dlg.model.set_target(rows, 64)
    dlg.model.set_target([i for i, e in enumerate(dlg.model.entries) if e.key.endswith("tiny.dds")], 64)
    assert sum(e.target is not None for e in dlg.model.entries) == 1  # tiny ignored (already small)
    assert "1 marked" in dlg.summary.text()

    dlg._run()
    _wait(app, lambda: written)
    wall = next(e for e in dlg.model.entries if e.key.endswith("wall.dds"))
    assert wall.done_target == 64 and wall.status.startswith("Done"), wall.status
    assert (out / "textures" / "a" / "wall.dds").is_file()
    assert "textures/a/wall.dds" in Manifest.load(out).entries
    dlg.done(0)

    # Re-open in skip mode: pre-filled and nothing to do.
    dlg2, _, _ = _dialog(app, tmp_path)
    wall2 = next(e for e in dlg2.model.entries if e.key.endswith("wall.dds"))
    assert wall2.target == 64 and wall2.done_target == 64
    dlg2.done(0)


def test_plugin_glue_with_stub_mobase(app, tmp_path, monkeypatch):
    class ModState(enum.IntFlag):
        ACTIVE = 2

    class Base:
        def __init__(self):
            pass

        def _parentWidget(self):
            return None

    stub = types.SimpleNamespace(
        IPluginTool=Base,
        IOrganizer=object,
        VersionInfo=lambda *a: a,
        PluginSetting=lambda *a: a,
        ModState=ModState,
        GuessedString=str,
    )
    monkeypatch.setitem(sys.modules, "mobase", stub)
    sys.modules.pop("texture_downscaler.plugin", None)
    import texture_downscaler

    plugin = texture_downscaler.createPlugin()

    class Mod:
        def __init__(self, path, sep=False):
            self.path, self.sep = path, sep

        def absolutePath(self):
            return str(self.path)

        def isSeparator(self):
            return self.sep

        def isForeign(self):
            return False

    mods = {
        "Sep_separator": (Mod(tmp_path / "s", sep=True), ModState.ACTIVE),
        "On": (Mod(tmp_path / "on"), ModState.ACTIVE),
        "Off": (Mod(tmp_path / "off"), ModState(0)),
        "Texture Downscaler Output": (Mod(tmp_path / "out"), ModState.ACTIVE),
    }
    mod_list = types.SimpleNamespace(
        allModsByProfilePriority=lambda: list(mods),
        state=lambda n: mods[n][1],
        getMod=lambda n: mods[n][0] if n in mods else None,
    )
    plugin.init(types.SimpleNamespace(modList=lambda: mod_list))
    sources = plugin._collect_sources("Texture Downscaler Output")
    assert [s.name for s in sources] == ["On"]
    assert plugin.name() == "Texture Downscaler"


def test_hide_mods(app, tmp_path):
    from texture_downscaler.ui.main_dialog import MainDialog

    b = tmp_path / "mods" / "Mod B"
    write_fake_dds(b / "textures" / "b" / "x.dds", 4096, 4096)
    sources = [*_make_mods(tmp_path), LooseModSource("Mod B", b)]
    dlg = MainDialog(
        sources=sources,
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))
    assert dlg.proxy.rowCount() == 4

    dlg._hide_mods(["Mod A"])
    assert dlg.proxy.rowCount() == 1 and not dlg.unhide_btn.isHidden()
    assert dlg.unhide_btn.text() == "Unhide 1 mod"
    # Bulk actions only touch shown rows, so hidden mods are left alone.
    dlg.rule_above.setCurrentIndex(dlg.rule_above.findData(128))
    dlg.rule_target.setCurrentIndex(dlg.rule_target.findData(128))
    dlg._apply_size_rule()
    assert [e.mod for e in dlg.model.entries if e.target] == ["Mod B"]

    dlg._unhide_all_mods()
    assert dlg.proxy.rowCount() == 4 and dlg.unhide_btn.isHidden()
    dlg.done(0)


def test_new_output_mod_is_refreshed_then_activated(tmp_path, monkeypatch):
    calls = []
    callbacks = []

    class ModState(enum.IntFlag):
        ACTIVE = 2

    class Base:
        def __init__(self):
            pass

        def _parentWidget(self):
            return None

    stub = types.SimpleNamespace(
        IPluginTool=Base,
        IOrganizer=object,
        VersionInfo=lambda *a: a,
        PluginSetting=lambda *a: a,
        ModState=ModState,
        GuessedString=str,
    )
    monkeypatch.setitem(sys.modules, "mobase", stub)
    sys.modules.pop("texture_downscaler.plugin", None)
    from texture_downscaler.plugin import TextureDownscalerTool

    known: dict = {}
    new_mod = types.SimpleNamespace(absolutePath=lambda: str(tmp_path / "Out"))
    mod_list = types.SimpleNamespace(
        getMod=lambda n: known.get(n),
        allMods=lambda: ["a", "b", "Out"],
        setActive=lambda n, v: calls.append(("active", n, v)),
        setPriority=lambda n, p: calls.append(("priority", n, p)),
    )

    def refresh(*_):
        calls.append("refresh")
        known["Out"] = new_mod  # MO2 only knows the mod after a refresh
        for cb in callbacks:
            cb()

    org = types.SimpleNamespace(
        modList=lambda: mod_list,
        createMod=lambda name: new_mod,
        refresh=refresh,
        onNextRefresh=lambda cb, immediate: callbacks.append(cb) or True,
        modDataChanged=lambda m: calls.append("changed"),
    )
    plugin = TextureDownscalerTool()
    plugin.init(org)
    assert plugin._ensure_output_mod("Out") == tmp_path / "Out"
    plugin._on_output_written("Out")
    assert calls == ["refresh", ("active", "Out", True), ("priority", "Out", 2)]

    calls.clear()
    plugin._on_output_written("Out")  # existing mod: cheap update only
    assert calls == ["changed"]


def test_tabs_selection_label_and_deselect_by_type(app, tmp_path):
    from PyQt6.QtWidgets import QTabWidget

    dlg, _, _ = _dialog(app, tmp_path)
    tabs = dlg.findChild(QTabWidget)
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Basic", "Batch"]

    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))
    dlg.table.selectAll()
    assert "<b>3</b> selected" in dlg.selection_label.text()

    dlg._deselect_where(lambda e: e.ttype is TextureType.NORMAL)
    keys = {dlg.model.entries[r].key for r in dlg._selected_rows()}
    assert keys == {"textures/a/wall.dds", "textures/a/tiny.dds"}

    dlg.table.selectAll()
    dlg._deselect_where(lambda e: e.ttype is not TextureType.NORMAL)  # "keep only normals"
    assert {dlg.model.entries[r].key for r in dlg._selected_rows()} == {"textures/a/wall_n.dds"}

    dlg.sel_target.setCurrentIndex(dlg.sel_target.findData(64))
    dlg._set_selected(64)
    assert [e.key for e in dlg.model.entries if e.target] == ["textures/a/wall_n.dds"]
    dlg.done(0)


def test_hidden_mods_are_restored_and_saved(app, tmp_path):
    from texture_downscaler.ui.main_dialog import MainDialog

    saved = []
    dlg = MainDialog(
        sources=_make_mods(tmp_path),
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
        hidden_mods={"Mod A"},
        on_hidden_mods_changed=saved.append,
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))
    assert dlg.proxy.rowCount() == 0 and dlg.unhide_btn.text() == "Unhide 1 mod"
    assert saved == []  # restoring must not trigger a save
    dlg._unhide_all_mods()
    assert saved == [set()] and dlg.proxy.rowCount() == 3
    dlg.done(0)
