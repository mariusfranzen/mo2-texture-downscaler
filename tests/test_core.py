from pathlib import Path

import pytest

from helpers import dds_header, write_fake_dds, write_rgba_dds
from texture_downscaler.core.dds import DdsInfo, parse_dds_header, read_dds_info
from texture_downscaler.core.downscaler import TEXCONV_PATH, Job, estimated_size, run_job, target_size
from texture_downscaler.core.output import (
    Manifest,
    RunMode,
    apply_manifest,
    make_plan,
    record,
    remove_outputs,
)
from texture_downscaler.core.scanner import LooseModSource, scan
from texture_downscaler.core.texture_types import TextureType, classify

# ---------------------------------------------------------------------------------- dds ----


def test_parse_legacy_fourcc():
    info = parse_dds_header(dds_header(4096, 2048, mips=13, fourcc=b"DXT5"))
    assert info == DdsInfo(4096, 2048, 13, "BC3", False)


def test_parse_dx10():
    info = parse_dds_header(dds_header(2048, 2048, mips=12, dxgi=99))
    assert info.format == "BC7_SRGB" and info.max_dim == 2048


def test_parse_cubemap_and_uncompressed():
    assert parse_dds_header(dds_header(512, 512, fourcc=b"DXT1", cubemap=True)).is_cubemap
    assert parse_dds_header(dds_header(64, 64)).format == "RGBA32bit"


def test_parse_invalid():
    assert parse_dds_header(b"not a dds") is None
    assert parse_dds_header(b"DDS " + b"\0" * 124) is None  # zero size


# -------------------------------------------------------------------------------- types ----


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("textures/armor/iron/cuirass.dds", TextureType.DIFFUSE),
        ("textures/armor/iron/cuirass_n.dds", TextureType.NORMAL),
        ("textures/actors/character/male/MaleBody_1_msn.dds", TextureType.NORMAL),
        ("textures/armor/iron/cuirass_m.dds", TextureType.SPECULAR),
        ("textures/x/y_S.DDS", TextureType.SPECULAR),
        ("textures/x/lamp_g.dds", TextureType.GLOW),
        ("textures/x/rock_p.dds", TextureType.PARALLAX),
        ("textures/cubemaps/shiny_e.dds", TextureType.ENVIRONMENT),
        ("textures/x/metal_rmaos.dds", TextureType.SPECULAR),
        ("textures/x/sign.dds", TextureType.DIFFUSE),  # "_n" must be a suffix, not any "n"
    ],
)
def test_classify(path, expected):
    assert classify(path) is expected


def test_classify_cubemap_flag():
    assert classify("textures/a/b.dds", is_cubemap=True) is TextureType.ENVIRONMENT


# ------------------------------------------------------------------------------ scanner ----


def test_scan_winner_follows_priority(tmp_path: Path):
    low, high = tmp_path / "Low", tmp_path / "High"
    write_fake_dds(low / "textures" / "a" / "Rock.dds", 4096, 4096, fourcc=b"DXT1")
    write_fake_dds(low / "textures" / "a" / "only_low.dds", 1024, 1024, fourcc=b"DXT1")
    # Different case for folder + file: same virtual path.
    write_fake_dds(high / "Textures" / "A" / "rock.DDS", 2048, 2048, fourcc=b"DXT1")
    (high / "meshes").mkdir()

    entries = scan([LooseModSource("Low", low), LooseModSource("High", high)])
    by_key = {e.key: e for e in entries}
    assert set(by_key) == {"textures/a/rock.dds", "textures/a/only_low.dds"}
    rock = by_key["textures/a/rock.dds"]
    assert rock.mod == "High" and rock.info.width == 2048 and rock.provider_count == 2
    assert entries[0].key == "textures/a/rock.dds"  # sorted by resolution, largest first


def test_scan_skips_invalid_and_missing(tmp_path: Path):
    mod = tmp_path / "M"
    (mod / "textures").mkdir(parents=True)
    (mod / "textures" / "broken.dds").write_bytes(b"garbage")
    assert scan([LooseModSource("M", mod), LooseModSource("Gone", tmp_path / "nope")]) == []


# --------------------------------------------------------------------------- downscaler ----


def test_target_size():
    sq = DdsInfo(4096, 4096, 13, "BC7", False)
    assert target_size(sq, 2048) == (2048, 2048)
    assert target_size(sq, 8192) == (4096, 4096)
    wide = DdsInfo(4096, 1024, 13, "BC1", False)
    assert target_size(wide, 1024) == (1024, 256)
    tiny = DdsInfo(4096, 8, 1, "BC1", False)
    assert target_size(tiny, 512) == (512, 4)  # BC block minimum
    assert estimated_size(1000, sq, 2048) == 250


@pytest.mark.skipif(not TEXCONV_PATH.is_file(), reason="texconv.exe not bundled")
def test_run_job_with_texconv(tmp_path: Path):
    src = write_rgba_dds(tmp_path / "src" / "Test Tex_n.dds", 64, 32)
    dest = tmp_path / "out" / "textures" / "x" / "Test Tex_n.dds"
    result = run_job(Job("k", src, dest, 32, 16, mipmaps=False))
    assert result.ok, result.message
    info = read_dds_info(dest)
    assert (info.width, info.height, info.mip_count) == (32, 16, 1)


# ------------------------------------------------------------------------ output/plan ----


def _setup(tmp_path: Path):
    mod = tmp_path / "mods" / "Big Mod"
    write_fake_dds(mod / "textures" / "a" / "big.dds", 4096, 4096, mips=13, fourcc=b"DXT1")
    write_fake_dds(mod / "textures" / "a" / "small.dds", 1024, 1024, mips=11, fourcc=b"DXT1")
    out = tmp_path / "mods" / "Output"
    return scan([LooseModSource("Big Mod", mod)]), out


def _fake_run(plan, entries, manifest, out):
    by_key = {e.key: e for e in entries}
    remove_outputs(plan.remove, manifest, out)
    for job in plan.jobs:
        job.dest.parent.mkdir(parents=True, exist_ok=True)
        job.dest.write_bytes(b"x")
        record(manifest, by_key[job.key], job)
    manifest.save(out)


def test_plan_and_skip_done(tmp_path: Path):
    entries, out = _setup(tmp_path)
    big = next(e for e in entries if e.key.endswith("big.dds"))
    big.target = 2048
    plan = make_plan(entries, Manifest(), out, RunMode.SKIP_DONE)
    assert [(j.width, j.height, j.mipmaps) for j in plan.jobs] == [(2048, 2048, True)]
    assert plan.jobs[0].dest == out / "textures/a/big.dds"

    manifest = Manifest()
    _fake_run(plan, entries, manifest, out)

    # Second session: rescan, reload manifest -> nothing to do, target pre-filled.
    entries2, _ = _setup(tmp_path)
    manifest2 = Manifest.load(out)
    apply_manifest(entries2, manifest2, out)
    big2 = next(e for e in entries2 if e.key.endswith("big.dds"))
    assert big2.target == 2048 and big2.done_target == 2048
    plan2 = make_plan(entries2, manifest2, out, RunMode.SKIP_DONE)
    assert plan2.is_empty and plan2.skipped == 1

    # Changing the target redoes it; clearing it removes the output.
    big2.target = 1024
    assert len(make_plan(entries2, manifest2, out, RunMode.SKIP_DONE).jobs) == 1
    big2.target = None
    plan3 = make_plan(entries2, manifest2, out, RunMode.SKIP_DONE)
    assert plan3.remove == ["textures/a/big.dds"] and not plan3.jobs
    _fake_run(plan3, entries2, manifest2, out)
    assert not (out / "textures/a/big.dds").exists()
    assert Manifest.load(out).entries == {}


def test_rebuild_mode_and_stale(tmp_path: Path):
    entries, out = _setup(tmp_path)
    for e in entries:
        e.target = 512
    manifest = Manifest()
    _fake_run(make_plan(entries, manifest, out, RunMode.SKIP_DONE), entries, manifest, out)

    rebuild = make_plan(entries, manifest, out, RunMode.REBUILD)
    assert sorted(rebuild.remove) == sorted(manifest.entries) and len(rebuild.jobs) == 2

    # Source mod disabled -> its outputs are stale and get removed.
    stale = make_plan([], manifest, out, RunMode.SKIP_DONE)
    assert sorted(stale.remove) == sorted(manifest.entries)


def test_source_changed_is_redone(tmp_path: Path):
    entries, out = _setup(tmp_path)
    big = next(e for e in entries if e.key.endswith("big.dds"))
    big.target = 2048
    manifest = Manifest()
    _fake_run(make_plan(entries, manifest, out, RunMode.SKIP_DONE), entries, manifest, out)
    big.size += 1  # mod updated
    apply_manifest(entries, manifest, out)
    assert big.done_target is None and "changed" in big.status
    assert len(make_plan(entries, manifest, out, RunMode.SKIP_DONE).jobs) == 1
