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
