# Skyrim Texture Downscaler

A **Mod Organizer 2** tool plugin (Tools menu → "Texture Downscaler") that scans the user's enabled
mods, lists the highest-resolution textures, and lets the user choose which textures to downscale and
to what resolution. Output goes to a separate mod; original mod files are never modified.

## Decisions (agreed with the user)

- **Game:** Skyrim (SE/AE) only for now.
- **Integration:** native MO2 Python plugin (`mobase.IPluginTool`), MO2 **2.5+** only
  (embedded Python 3.12, **PyQt6**). No PyQt5 support.
- **Sources:** loose files in **enabled** mods only (no disabled mods, no separators, no foreign/DLC
  entries, no Overwrite, not the output mod itself). BSA support is planned for later: keep new
  sources behind the `TextureSource` protocol in `core/scanner.py` so a BSA reader can plug in
  without touching the UI or downscaler.
- **Conflicts:** only the winning file per virtual path counts (highest MO2 priority wins),
  matched case-insensitively.
- **Output:** new mod (default name `Texture Downscaler Output`, plugin setting
  `output_mod_name`), created on first run, enabled and moved to the bottom of the load order.
- **Re-runs:** if the output mod has a manifest from a previous run, ask the user on launch:
  *Keep & skip already done* or *Rebuild from scratch* (or cancel).
- **Encoder:** `texconv.exe` (Microsoft DirectXTex, MIT) is bundled in
  `plugin/texture_downscaler/bin/` with its license.
- **Bulk selection tools:** size rule ("larger than X → Y"), max size per texture type, set/clear
  target for selected rows, filter by mod / folder / search text / type, right-click "Hide mod"
  (session only; hidden mods are untouched by bulk actions), and a
  "hide textures smaller than N" filter. Bulk actions apply to the rows currently shown.

## Layout

```text
plugin/texture_downscaler/      # the MO2 plugin package (copied/linked into <MO2>/plugins/)
  __init__.py                   # createPlugin() entry point
  plugin.py                     # IPluginTool: collects enabled mods, run-mode prompt, output mod
  core/                         # NO mobase / Qt imports - plain-Python and unit tested
    dds.py                      # DDS header parsing (legacy FourCC + DX10), no pixel decoding
    texture_types.py            # classify by suffix: _n/_msn normal, _s/_m/_em spec, _g glow, ...
    scanner.py                  # TextureSource protocol, LooseModSource, scan() -> TextureEntry
    downscaler.py               # target size math, texconv command/job runner (thread pool)
    output.py                   # manifest (texture_downscaler.json in output mod), make_plan()
  ui/                           # PyQt6 only, no mobase
    table_model.py              # QAbstractTableModel + filter proxy
    workers.py                  # QThread scan / downscale workers
    main_dialog.py              # main window, bulk actions, run flow
  bin/texconv.exe, LICENSE-texconv.txt
tests/                          # pytest; test_ui.py runs Qt offscreen with a stub `mobase`
scripts/dev_link.ps1            # junction plugin into an MO2 install
```

## Key behaviours

- **Format preservation:** texconv is called *without* `-f`, which keeps the source format
  (BC1/3/5/7, sRGB flag, uncompressed). Verified on real Skyrim textures. Textures without mips
  stay without mips (`-m 1`), otherwise a full chain is regenerated (`-m 0`).
- **Aspect ratio** kept; larger side becomes the target; BC formats are clamped to ≥4 px.
- A target ≥ current size means "keep original" (stored as `None`).
- **Manifest** (`texture_downscaler.json` in the output mod) records source mod, size, mtime and
  target per texture. A run (`make_plan`) then:
  - skips outputs whose target and source are unchanged (skip mode),
  - redoes ones where the source changed or the target changed,
  - deletes outputs whose target was cleared or whose source is no longer in any enabled mod,
  - in rebuild mode deletes all previous outputs first.
  Only files listed in the manifest are ever deleted.
- A newly created output mod only appears in MO2 after `organizer.refresh()`; enabling and
  moving it to the bottom is done in an `onNextRefresh` callback (before that, MO2 ignores it).
- MO2 API calls happen on the main thread. Scanning and texconv run in `QThread`s.
  texconv processes run in parallel (plugin setting `workers`, 0 = auto).

## Conventions

- Keep `core/` free of `mobase` and Qt; `ui/` free of `mobase`. Only `plugin.py` touches MO2.
- Must run on Python 3.12 (MO2's embedded interpreter): don't use 3.13+ features.
- Type hints, `ruff format`, `ruff check` (config in `pyproject.toml`).
- Never write into an existing mod folder or the game's Data folder. Only the output mod.
- Windows-only (MO2 is Windows-only). Treat virtual paths case-insensitively.

## Commands

```powershell
python -m venv .venv && .venv\Scripts\pip install pytest ruff PyQt6   # dev setup
.venv\Scripts\python -m pytest                                        # tests (incl. offscreen UI)
.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format .
powershell -File scripts\dev_link.ps1                                 # link into MO2 (then restart MO2)
```

The user's MO2 install for manual testing: `D:\D Games\SkyrimStuff\mo2` (MO2 2.5.2, ~740 mods,
~60k winning textures; a full header scan takes ~6 s).
