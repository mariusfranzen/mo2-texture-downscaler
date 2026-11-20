# MO2 Texture Downscaler

A [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer) tool plugin for **Skyrim Special / Anniversary Edition**. It finds the highest-resolution textures across your enabled mods, then downscales the ones you choose to save VRAM.

Your original mods are never modified. The downscaled textures go into a separate output mod at the bottom of your load order. Disable or delete that mod and everything is back to how it was.

## Features

- **Scans every enabled mod**: about 60,000 textures across 700 mods in a few seconds (tested on my setup), and under a second on later opens thanks to a header cache.
- **Only shows the textures the game actually loads.** When several mods provide the same texture, only the winning file (highest priority in MO2) is listed.
- **A sortable, filterable table** with these columns: texture path, mod, author, type, resolution, format, file size, target size, estimated new size and status.
  - Filter by search text, mod, texture type, "hide textures smaller than…", only marked, and hide already downscaled.
  - Hide whole mods or authors via right-click. Hidden mods and authors are remembered.
- **Bulk tools:**
  - *Basic* tab: mark the selected textures for downscaling to a size.
  - *Batch* tab: a size rule ("everything larger than 4K → 2K") and a maximum size per texture type (diffuse, normal, specular/mask, glow, parallax, environment).
  - Right-click: deselect or keep only one texture type in your selection.
- **Quality-preserving downscaling** with Microsoft's [texconv](https://github.com/microsoft/DirectXTex):
  - Keeps the original compression format (BC1/BC3/BC5/BC7, including sRGB).
  - Keeps the aspect ratio and regenerates mipmaps.
  - Downscales in parallel.
- **Optional author column:** mod authors (Nexus uploader names) can be looked up on Nexus Mods. See [Mod authors](#mod-authors-nexus).

## Requirements

- Windows
- Mod Organizer 2 **2.5 or newer** (needs MO2's Python 3.12 / PyQt6 plugin support)
- Skyrim Special Edition or Anniversary Edition
- Your texture mods installed as **loose files**. Textures packed inside BSA archives aren't scanned yet.

`texconv.exe` is bundled, so nothing else needs installing.

## Installation

1. Download `texture_downscaler-x.y.z.zip` from the [latest release](https://github.com/mariusfranzen/mo2-texture-downscaler/releases/latest).
2. Extract it into your MO2 `plugins` folder, so that you end up with `<MO2>/plugins/texture_downscaler/__init__.py`.
3. Restart Mod Organizer 2.

The tool appears under the **Tools** menu (puzzle-piece icon) as **Texture Downscaler**.

## Usage

1. Open **Tools → Texture Downscaler**. It scans your enabled mods and lists their textures, largest first.
2. Narrow the list down with the filters, or hide mods and authors you never want to touch.
3. Mark textures for downscaling:
   - **Basic tab:** select rows (Ctrl/Shift-click, Ctrl+A), pick a size, click **Mark for downscaling**.
   - **Batch tab:** apply a size rule or per-type limits to every texture currently shown.
   - Or double-click a cell in the *Downscale to* column to set a single texture.

   Marked rows turn blue. The bottom bar shows how much VRAM the marked textures would save.
4. Click **Downscale N marked textures…**. Nothing is written before this step.
5. Make sure **Texture Downscaler Output** is enabled and at the bottom of your mod list. The tool does this for you when it creates the mod.

### Which textures to downscale?

The safest order, from "barely noticeable" to "most noticeable":

1. **Specular / mask, glow, parallax and environment maps.** These can usually go down one or two steps without any visible difference.
2. **Normal maps.** You lose fine surface detail, which is often hard to see at normal viewing distance.
3. **Diffuse (colour) maps.** Blur shows up here first, so keep these highest.

A good starting point for a 4K-heavy setup: diffuse 4K, normal 2K, specular/mask, glow and parallax 1K, environment 512.

Small clutter (bottles, books, food) can go much lower. Be more careful with faces, bodies and large surfaces you see up close.

### What the numbers mean

- **VRAM:** compressed textures take about as much video memory as their file size. The saving shown is an upper bound, because the game only loads textures for what's around you.
- **Disk:** disk usage goes **up** by the size of the downscaled copies. Your originals stay where they are; the output mod simply wins the conflict.

## Re-running and undoing

The output mod contains a `texture_downscaler.json` file recording what was downscaled, from which source file and to what size. When you open the tool again and that file exists, you're asked to either:

- **Keep & skip already done:** finished textures are pre-marked and skipped. Textures whose source mod changed are redone. Textures you unmark, or whose mod you disabled, have their output removed.
- **Rebuild from scratch:** all previous outputs are deleted and everything you mark is downscaled again.

To undo everything, disable or delete the **Texture Downscaler Output** mod. The tool only ever deletes files it wrote itself.

## Mod authors (Nexus)

MO2 doesn't store mod authors, so the *Author* column can look them up on Nexus Mods. It shows the uploader's account name.

- The first time you open the tool, it asks whether to look up authors **automatically**.
- Either way, you can right-click any texture (or the *Author* column header) and choose **Fetch author for N selected mods** or **Fetch all mod authors** at any time.
- Lookups use the Nexus API key MO2 already saved when you logged in to Nexus. You can also paste a key in the plugin settings.
- **API usage:** one request per Nexus mod page, made once and then cached. Only newly installed mods are looked up later. The lookup pauses itself if your hourly allowance gets low, so MO2's own update checks aren't affected. Only Nexus mod IDs are sent.
- Mods that don't come from Nexus show as *Unknown*.

## Settings

In MO2, go to **Settings → Plugins → Texture Downscaler**:

| Setting | Default | Description |
|---|---|---|
| `output_mod_name` | `Texture Downscaler Output` | Name of the mod that receives the downscaled textures |
| `workers` | `0` | Number of parallel texconv processes (0 = automatic) |
| `author_lookup` | `ask` | Look up mod authors automatically: `ask`, `yes` or `no` |
| `nexus_api_key` | *(empty)* | Nexus API key to use instead of MO2's saved key |
| `hidden_mods`, `hidden_authors`, `sort_state` | | Remembered UI state, managed by the tool |

Caches are stored in `<MO2>/plugins/data/texture_downscaler/`. You can safely delete them; they're rebuilt as needed.

## Limitations

- Only **loose** textures are scanned. Textures inside BSA archives are not yet supported.
- Textures in MO2's **Overwrite** folder aren't scanned. If a texture is in Overwrite, it also overrides the output mod.
- Skyrim SE/AE only for now.

## Development

The plugin lives in `plugin/texture_downscaler/`:

| Folder | Contents |
|---|---|
| `core/` | Plain Python, no MO2 or Qt dependencies: DDS parsing, scanning, texconv wrapper, output manifest, Nexus lookup |
| `ui/` | PyQt6 dialog, table model and background workers |
| `plugin.py` | The MO2 integration |

```powershell
python -m venv .venv
.venv\Scripts\pip install pytest ruff PyQt6
.venv\Scripts\python -m pytest            # tests, including offscreen UI tests
.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format .

# Link the plugin into an MO2 install for testing (directory junction), then restart MO2:
powershell -File scripts\dev_link.ps1 -Mo2Path "C:\Path\To\MO2"
```

### Making a release

1. Bump `__version__` in `plugin/texture_downscaler/__init__.py` (MO2 reads the plugin version from it), then commit and push.
2. Tag the commit and push the tag: `git tag -a v1.2.3 -m "Texture Downscaler 1.2.3"`, then `git push origin v1.2.3`.
3. Build the ZIP: `powershell -File scripts\make_release.ps1`. It writes `dist\texture_downscaler-1.2.3.zip` and refuses to build if the tag doesn't contain the same version.
4. Create a GitHub release for the tag and attach the ZIP.

`scripts\make_release.ps1 -FromHead` builds a test ZIP from the current commit without needing a tag.

Keep `core/` free of `mobase` and Qt imports. The code must stay compatible with Python 3.12, MO2's embedded interpreter.

## AI usage in development

This mod was made with claude code, but the code has been reviewed and thoroughly tested by a professional developer.

- At the point of writing this there are 48 unit tests
- It has been tested with my own personal mod setup with 700 mods


## Credits

- [DirectXTex / texconv](https://github.com/microsoft/DirectXTex) by Microsoft (MIT license, see `plugin/texture_downscaler/bin/LICENSE-texconv.txt`)
- [Mod Organizer 2](https://github.com/ModOrganizer2/modorganizer)
