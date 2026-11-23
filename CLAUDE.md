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
- **Side panel has two tabs:**
  - *Basic* acts on the **selected rows**: mark them with a size, or unmark them. It shows a step-by-step hint
    (select → Mark selected → Downscale...) and a live selection summary.
  - *Batch* acts on **all rows shown** by the filters: a size rule ("larger than X → Y"), max size per
    texture type, and unmark all shown.
- **Filters:** search text, mod, type, "hide smaller than N", only marked, hide already downscaled
  (done for the current target; changing the target makes a row pending and visible again), and hidden mods.
- **Right-click:** set or clear the target, *Deselect by type* / *Keep only type selected* (when the
  selection mixes types), hide the mod(s) of the selected rows, show only this mod or folder, reset filters.
- **Hidden mods are remembered** in the plugin setting `hidden_mods` (a JSON list). Hidden mods are
  untouched by batch actions, but any targets they already have are kept.

## Layout

```text
plugin/texture_downscaler/      # the MO2 plugin package (copied/linked into <MO2>/plugins/)
  __init__.py                   # createPlugin() entry point
  plugin.py                     # IPluginTool: collects enabled mods, run-mode prompt, output mod
  core/                         # NO mobase / Qt imports - plain-Python and unit tested
    dds.py                      # DDS header parsing (legacy FourCC + DX10), no pixel decoding
    texture_types.py            # classify by suffix: _n/_msn normal, _s/_m/_em spec, _g glow, ...
    scanner.py                  # TextureSource protocol, LooseModSource, scan() -> TextureEntry
    nexus.py                    # Nexus uploader lookup via public GraphQL (no key) + cache
    header_cache.py             # persistent DDS header cache (path + size + mtime)
    downscaler.py               # target size math, texconv command/job runner (thread pool)
    i18n.py                     # tr(): JSON string lookup, English fallback, plural rules
    output.py                   # manifest (texture_downscaler.json in output mod), make_plan()
  i18n/en.json                  # all UI strings (English reference); translations go next to it
  ui/                           # PyQt6 only, no mobase
    table_model.py              # QAbstractTableModel + filter proxy
    workers.py                  # QThread scan / downscale workers
    main_dialog.py              # main window, bulk actions, run flow
  bin/texconv.exe, LICENSE-texconv.txt
tests/                          # pytest; test_ui.py runs Qt offscreen with a stub `mobase`
scripts/dev_link.ps1            # junction plugin into an MO2 install
scripts/make_release.ps1        # build the release ZIP from tag v<__version__> (never tags/pushes)
scripts/make_icon.py            # regenerate plugin icon.png (256px) from assets/logo.png
assets/logo.png                 # 2048px logo (README header); the plugin ships only icon.png
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
- **Sorting** never uses Qt's proxy sort (one Python `lessThan` callback per comparison took
  ~2.5 s for 60k rows). `TextureFilterProxy.sort()` sorts the source list in Python (two stable
  passes: secondary, then primary) and calls `TextureTableModel.reorder()`, which keeps
  selections. The proxy itself stays unsorted and only filters. Secondary sort: "Then by"
  dropdown or Ctrl+click on a header.
- **Header cache** (`core/header_cache.py`): `<MO2>/plugins/data/texture_downscaler/header_cache.json`
  (from `organizer.pluginDataPath()`), keyed by lowercase abs path + size + mtime. Size and mtime come
  free from `os.scandir`, so unchanged files are never opened. Measured on the user's setup:
  3.3 s → 0.9 s for 60k textures; the cache file is about 10 MB. Only entries used in a scan are saved,
  so deleted files drop out.
- **Persisted UI state** (plugin settings, JSON): `hidden_mods`, `sort_state` (primary column and
  direction, secondary column and direction). Restoring state must not trigger a save.
- **Author column** = the Nexus uploader's account name (agreed with the user, rather than the
  free-text `author` field). MO2 doesn't store authors, so `core/nexus.py` looks them up via the
  public **GraphQL v2** API (`https://api.nexusmods.com/v2/graphql`, `legacyModsByDomain` query)
  **without any API key or login**. Nexus support pointed us there, since we only read metadata; the
  user decided no registration is needed. Never add API keys, Credential Manager reads or
  `apikey`/`Authorization` headers back (a test asserts none are sent).
  - Measured on the live API: one request takes a long id list but returns at most **80** results,
    and offset paging over a long list lost results. So ids go in chunks of `BATCH_SIZE` = 50, each
    read in one go. If `totalCount` > returned, the chunk is split in half and retried.
    601 pages: 13 requests, about 5 s.
  - Mods missing from a response (deleted, hidden, wrong id) are cached as `""` and shown as
    "Unknown". Adult-content mods are returned anonymously.
  - Failures (HTTP, 429, network, GraphQL errors) raise `NexusError`. `AuthorWorker.failed` then shows
    a warning popup with technical details, never failing silently; authors found so far are kept.
  - Results are cached permanently in `plugins/data/texture_downscaler/nexus_authors.json`.
  - `NEXUS_ENABLED` (in `core/nexus.py`) is the master switch. It's True. When False, the dialog gets
    `authors_enabled=False`: the column is hidden and nothing is sent.
  - **Opt-in:** the plugin setting `author_lookup` (ask / yes / no) defaults to "ask". The first open
    asks once and saves the answer. "yes" fetches missing authors automatically; "no" doesn't.
    Either way the Author column stays visible: authors not looked up yet show "Unknown", and
    right-click (rows or the Author header) offers "Fetch author for N selected mods" and
    "Fetch all mod authors", so nobody is locked into their first answer.
  - Mods without a Nexus ID, or with a game domain that can't be resolved, show "Unknown".
  - "Hide author" is saved in the plugin setting `hidden_authors`; search also matches authors.
- Saved sort settings store column *names*, so adding columns doesn't scramble them.
- **i18n:** all user-visible text goes through `tr("key", **placeholders)` from `core/i18n.py`
  (plain Python). Strings live in `plugin/texture_downscaler/i18n/en.json`, the reference. Other
  languages are `<code>.json` files layered on top, with English as the per-key fallback.
  - Plural entries are `{"one": ..., "other": ...}` objects selected by `count=`. `PLURAL_RULES`
    maps languages like ru/pl/cs to their extra forms (few/many).
  - The language comes from the plugin setting `language`, else MO2's `[Settings] language` in
    `ModOrganizer.ini`, else Windows' UI languages, else English. It's loaded in `init()`.
  - Internal IDs stay English: `HEADERS` (saved in `sort_state`), `TextureType` values and the
    `UNKNOWN` author marker. Show them via `column_title()`, `type_name()` and `author_text()`.
  - Only English ships in this repo; translators contribute other files.
  - `tests/test_i18n.py` checks that every `tr()` key exists in en.json and every en.json key is
    used, that placeholders are valid, and that no hard-coded English remains in UI widget calls.
    When adding UI text, add the key to en.json.
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
powershell -File scripts\dev_link.ps1 -Mo2Path "<path to MO2>"       # link into MO2 (then restart MO2)
powershell -File scripts\make_release.ps1                             # dist\texture_downscaler-<ver>.zip from tag v<ver>
```

Manual testing happens in the user's own MO2 install (MO2 2.5.2, ~740 mods, ~60k winning textures;
an uncached header scan takes ~3-6 s).
