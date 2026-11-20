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


def test_hide_already_downscaled(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    dlg, _, written = _dialog(app, tmp_path)
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))
    dlg.hide_done.setChecked(True)
    assert dlg.proxy.rowCount() == 3

    dlg.model.set_target([i for i, e in enumerate(dlg.model.entries) if e.key.endswith("wall.dds")], 64)
    dlg._run()
    _wait(app, lambda: written)
    assert dlg.proxy.rowCount() == 2  # wall.dds is done -> hidden right after the run

    wall = next(i for i, e in enumerate(dlg.model.entries) if e.key.endswith("wall.dds"))
    dlg._set_rows([wall], None)  # unmarking makes it pending (output will be removed) -> visible
    assert dlg.proxy.rowCount() == 3
    dlg.done(0)


def test_summary_and_run_button_wording(app, tmp_path):
    dlg, _, _ = _dialog(app, tmp_path)
    assert dlg.run_btn.text() == "Downscale marked textures..."
    dlg.model.set_target([i for i, e in enumerate(dlg.model.entries) if e.key.endswith("wall.dds")], 64)
    assert dlg.run_btn.text() == "Downscale 1 marked texture..."
    assert "VRAM if all loaded" in dlg.summary.text() and "on disk" in dlg.summary.text()
    dlg.done(0)


def test_secondary_sort(app, tmp_path):
    from PyQt6.QtCore import Qt

    from texture_downscaler.ui.main_dialog import MainDialog
    from texture_downscaler.ui.table_model import COL_MOD, COL_PATH, COL_RES

    sources = []
    for mod, size in (("Bravo", 2048), ("Alpha", 1024), ("Charlie", 2048), ("Alpha", 2048)):
        root = tmp_path / "mods" / mod
        write_fake_dds(root / "textures" / f"{mod}_{size}.dds", size, size)
        sources.append(LooseModSource(mod, root))
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

    def order():
        return [dlg.proxy.index(r, COL_PATH).data() for r in range(dlg.proxy.rowCount())]

    dlg.table.sortByColumn(COL_RES, Qt.SortOrder.DescendingOrder)
    dlg.set_secondary_column(COL_MOD)  # like Ctrl+click on the Mod header
    assert order() == [
        "textures/Alpha_2048.dds",
        "textures/Bravo_2048.dds",
        "textures/Charlie_2048.dds",
        "textures/Alpha_1024.dds",
    ]
    assert dlg.proxy.headerData(COL_MOD, Qt.Orientation.Horizontal).endswith("(2\u25b2)")

    dlg.set_secondary_column(COL_MOD)  # second Ctrl+click flips direction
    assert order()[:3] == ["textures/Charlie_2048.dds", "textures/Bravo_2048.dds", "textures/Alpha_2048.dds"]

    dlg.table.sortByColumn(COL_RES, Qt.SortOrder.AscendingOrder)
    assert order()[0] == "textures/Alpha_1024.dds"
    assert order()[1:] == ["textures/Charlie_2048.dds", "textures/Bravo_2048.dds", "textures/Alpha_2048.dds"]
    dlg.done(0)


def test_sort_is_restored_and_saved(app, tmp_path):
    from PyQt6.QtCore import Qt

    from texture_downscaler.ui.main_dialog import MainDialog
    from texture_downscaler.ui.table_model import COL_MOD, COL_PATH, COL_SIZE

    saved = []
    dlg = MainDialog(
        sources=_make_mods(tmp_path),
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
        sort_state={"column": "Size", "descending": False, "secondary": "Mod", "secondary_desc": True},
        on_sort_changed=saved.append,
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    header = dlg.table.horizontalHeader()
    assert header.sortIndicatorSection() == COL_SIZE
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert dlg.secondary_combo.currentData() == COL_MOD and dlg.secondary_order.isChecked()
    assert saved == []  # restoring must not trigger a save

    dlg.table.sortByColumn(COL_PATH, Qt.SortOrder.DescendingOrder)
    assert saved[-1] == {"column": "Texture", "descending": True, "secondary": "Mod", "secondary_desc": True}
    dlg.secondary_combo.setCurrentIndex(0)
    assert saved[-1]["secondary"] is None
    dlg.done(0)


def test_scan_writes_header_cache(app, tmp_path):
    from texture_downscaler.ui.main_dialog import MainDialog

    cache = tmp_path / "data" / "header_cache.json"
    dlg = MainDialog(
        sources=_make_mods(tmp_path),
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
        header_cache_path=cache,
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    assert cache.is_file() and len(dlg.model.entries) == 3
    dlg.done(0)


def test_author_column_lookup_and_hide_author(app, tmp_path, monkeypatch):
    from texture_downscaler.core import nexus
    from texture_downscaler.core.nexus import AuthorCache, NexusRef
    from texture_downscaler.ui.main_dialog import MainDialog
    from texture_downscaler.ui.table_model import COL_AUTHOR, COL_MOD

    def mod(name, w):
        root = tmp_path / "mods" / name
        write_fake_dds(root / "textures" / f"{name}.dds", w, w)
        return LooseModSource(name, root)

    sources = [mod("Cached", 2048), mod("Fetched", 2048), mod("Local", 2048), mod("Fetched Too", 1024)]
    ref_cached = NexusRef("skyrimspecialedition", 1)
    ref_fetched = NexusRef("skyrimspecialedition", 2)
    cache_path = tmp_path / "authors.json"
    seed = AuthorCache(cache_path)
    seed.put(ref_cached, "Carol")
    seed.save()

    monkeypatch.setattr(nexus, "fetch_uploader", lambda ref, key: ("Fred", 1000))
    monkeypatch.setattr(nexus.time, "sleep", lambda s: None)
    saved_hidden = []
    dlg = MainDialog(
        sources=sources,
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
        mod_refs={"Cached": ref_cached, "Fetched": ref_fetched, "Fetched Too": ref_fetched, "Local": None},
        author_cache_path=cache_path,
        api_key_provider=lambda: "secret",
        on_hidden_authors_changed=saved_hidden.append,
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    _wait(app, lambda: not dlg.author_worker.isRunning())
    app.processEvents()
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))

    def authors():
        return {
            dlg.proxy.index(r, COL_MOD).data(): dlg.proxy.index(r, COL_AUTHOR).data()
            for r in range(dlg.proxy.rowCount())
        }

    assert authors() == {"Cached": "Carol", "Fetched": "Fred", "Fetched Too": "Fred", "Local": "Unknown"}
    assert AuthorCache.load(cache_path).get(ref_fetched) == "Fred"

    dlg._hide_authors(["Fred"])
    assert set(authors()) == {"Cached", "Local"}
    assert saved_hidden == [{"Fred"}] and dlg.unhide_btn.text() == "Unhide 1 author"
    dlg._hide_mods(["Local"])
    assert dlg.unhide_btn.text() == "Unhide 1 mod, 1 author"
    dlg.search.setText("carol")  # search also matches authors
    assert set(authors()) == {"Cached"}
    dlg.search.clear()
    dlg._unhide_all_mods()
    assert len(authors()) == 4 and saved_hidden[-1] == set()
    dlg.done(0)


def test_plugin_collects_nexus_refs(tmp_path, monkeypatch):
    class Base:
        def __init__(self):
            pass

    stub = types.SimpleNamespace(
        IPluginTool=Base,
        IOrganizer=object,
        VersionInfo=lambda *a: a,
        PluginSetting=lambda *a: a,
        ModState=enum.IntFlag("ModState", {"ACTIVE": 2}),
        GuessedString=str,
    )
    monkeypatch.setitem(sys.modules, "mobase", stub)
    sys.modules.pop("texture_downscaler.plugin", None)
    from texture_downscaler.core.nexus import NexusRef
    from texture_downscaler.plugin import TextureDownscalerTool

    def mod(mod_id, game="SkyrimSE"):
        return types.SimpleNamespace(nexusId=lambda: mod_id, gameName=lambda: game)

    mods = {"Nexus": mod(53000), "Manual": mod(0), "LE port": mod(12, "Skyrim"), "Broken": object()}
    games = {"SkyrimSE": types.SimpleNamespace(gameNexusName=lambda: "skyrimspecialedition")}
    org = types.SimpleNamespace(
        modList=lambda: types.SimpleNamespace(getMod=mods.get),
        getGame=lambda name: games.get(name),
    )
    plugin = TextureDownscalerTool()
    plugin.init(org)
    assert plugin._nexus_refs(list(mods)) == {
        "Nexus": NexusRef("skyrimspecialedition", 53000),
        "Manual": None,
        "LE port": NexusRef("skyrim", 12),  # falls back to the built-in mapping
        "Broken": None,
    }


def test_no_auto_fetch_shows_unknown_and_fetches_on_demand(app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMenu

    from texture_downscaler.core import nexus
    from texture_downscaler.core.nexus import NexusRef
    from texture_downscaler.ui.main_dialog import MainDialog
    from texture_downscaler.ui.table_model import COL_AUTHOR, COL_MOD

    def mod(name):
        root = tmp_path / "mods" / name
        write_fake_dds(root / "textures" / f"{name}.dds", 2048, 2048)
        return LooseModSource(name, root)

    fetched = []
    monkeypatch.setattr(
        nexus, "fetch_uploader", lambda ref, key: fetched.append(ref.mod_id) or (f"U{ref.mod_id}", 999)
    )
    monkeypatch.setattr(nexus.time, "sleep", lambda s: None)
    refs = {"A": NexusRef("skyrimspecialedition", 1), "B": NexusRef("skyrimspecialedition", 2), "Local": None}
    dlg = MainDialog(
        sources=[mod("A"), mod("B"), mod("Local")],
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
        auto_fetch_authors=False,
        mod_refs=refs,
        author_cache_path=tmp_path / "authors.json",
        api_key_provider=lambda: "key",
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))

    def authors():
        return {
            dlg.proxy.index(r, COL_MOD).data(): dlg.proxy.index(r, COL_AUTHOR).data()
            for r in range(dlg.proxy.rowCount())
        }

    assert not dlg.table.isColumnHidden(COL_AUTHOR) and dlg.author_worker is None
    assert authors() == {"A": "Unknown", "B": "Unknown", "Local": "Unknown"}

    menu = QMenu()
    dlg._add_fetch_actions(menu, ["A", "Local"])
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == ["Fetch author for 1 selected mod", "Fetch all mod authors (2 remaining)"]

    dlg.fetch_authors(["A"])
    assert authors()["A"] == "..."
    _wait(app, lambda: not dlg.author_worker.isRunning())
    app.processEvents()
    assert fetched == [1] and authors() == {"A": "U1", "B": "Unknown", "Local": "Unknown"}

    dlg.fetch_authors(None)
    _wait(app, lambda: not dlg.author_worker.isRunning())
    app.processEvents()
    assert fetched == [1, 2] and authors()["B"] == "U2"
    menu = QMenu()
    dlg._add_fetch_actions(menu, ["A", "B"])
    assert menu.actions() == []  # nothing left to fetch
    dlg.done(0)


@pytest.mark.parametrize(
    ("setting", "answer", "expected", "saved"),
    [
        ("ask", "Yes", True, "yes"),
        ("ask", "No", False, "no"),
        ("yes", None, True, None),
        ("no", None, False, None),
    ],
)
def test_author_lookup_prompt(app, monkeypatch, setting, answer, expected, saved):
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
        ModState=enum.IntFlag("ModState", {"ACTIVE": 2}),
        GuessedString=str,
    )
    monkeypatch.setitem(sys.modules, "mobase", stub)
    sys.modules.pop("texture_downscaler.plugin", None)
    from texture_downscaler.core.nexus import NexusRef
    from texture_downscaler.plugin import TextureDownscalerTool

    asked, writes = [], []

    def question(parent, title, text):
        asked.append(text)
        return getattr(QMessageBox.StandardButton, answer)

    monkeypatch.setattr(QMessageBox, "question", question)
    org = types.SimpleNamespace(
        pluginSetting=lambda name, key: setting,
        setPluginSetting=lambda name, key, value: writes.append((key, value)),
    )
    plugin = TextureDownscalerTool()
    plugin.init(org)
    refs = {"A": NexusRef("skyrimspecialedition", 1), "B": NexusRef("skyrimspecialedition", 1), "C": None}
    assert plugin._author_lookup_enabled(refs) is expected
    assert writes == ([("author_lookup", saved)] if saved else [])
    if answer:
        assert "(1 for your enabled mods)" in asked[0] and "API allowance" in asked[0]
    else:
        assert asked == []


def test_plugin_metadata_methods(monkeypatch):
    """MO2 calls these on load; any NameError here means the plugin silently fails to load."""

    class Base:
        def __init__(self):
            pass

    stub = types.SimpleNamespace(
        IPluginTool=Base,
        IOrganizer=object,
        VersionInfo=lambda *a: a,
        PluginSetting=lambda *a: a,
        ModState=enum.IntFlag("ModState", {"ACTIVE": 2}),
        GuessedString=str,
    )
    monkeypatch.setitem(sys.modules, "mobase", stub)
    sys.modules.pop("texture_downscaler.plugin", None)
    import texture_downscaler
    from texture_downscaler.plugin import TextureDownscalerTool

    plugin = TextureDownscalerTool()
    major, minor, patch = (int(x) for x in texture_downscaler.__version__.split("."))
    assert plugin.version() == (major, minor, patch, 0)
    for method in ("name", "author", "description", "displayName", "tooltip", "settings", "requirements"):
        assert getattr(plugin, method)() is not None


def test_nexus_disabled_hides_everything_author_related(app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMenu

    from texture_downscaler.core import nexus
    from texture_downscaler.ui.main_dialog import MainDialog
    from texture_downscaler.ui.table_model import COL_AUTHOR

    assert nexus.NEXUS_ENABLED is False  # public builds must not talk to Nexus until registered

    dlg = MainDialog(
        sources=_make_mods(tmp_path),
        output_dir=tmp_path / "out",
        output_name="Output",
        mode=RunMode.SKIP_DONE,
        workers=1,
        ensure_output_mod=lambda: None,
        on_output_written=lambda: None,
        authors_enabled=False,
        sort_state={"column": "Author", "descending": True, "secondary": "Author", "secondary_desc": False},
    )
    _wait(app, lambda: dlg.run_btn.isEnabled())
    assert dlg.table.isColumnHidden(COL_AUTHOR) and dlg.author_worker is None
    assert dlg.secondary_combo.findData(COL_AUTHOR) == -1
    assert dlg.table.horizontalHeader().sortIndicatorSection() != COL_AUTHOR
    dlg.hide_combo.setCurrentIndex(dlg.hide_combo.findData(0))
    dlg.search.setText("unknown")  # must not match every row via a phantom author
    assert dlg.proxy.rowCount() == 0
    menu = QMenu()
    dlg._add_fetch_actions(menu, ["Mod A"])
    assert menu.actions() == []
    dlg.done(0)


def test_plugin_without_nexus_has_no_nexus_settings_or_prompt(monkeypatch):
    class Base:
        def __init__(self):
            pass

    stub = types.SimpleNamespace(
        IPluginTool=Base,
        IOrganizer=object,
        VersionInfo=lambda *a: a,
        PluginSetting=lambda *a: a,
        ModState=enum.IntFlag("ModState", {"ACTIVE": 2}),
        GuessedString=str,
    )
    monkeypatch.setitem(sys.modules, "mobase", stub)
    sys.modules.pop("texture_downscaler.plugin", None)
    from texture_downscaler.plugin import TextureDownscalerTool

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("must not ask about Nexus"))
    plugin = TextureDownscalerTool()
    plugin.init(types.SimpleNamespace())
    names = [s[0] for s in plugin.settings()]
    assert not {"author_lookup", "nexus_api_key", "hidden_authors"} & set(names)
    assert plugin._author_dialog_args([]) == {"authors_enabled": False}
