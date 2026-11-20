from pathlib import Path

from texture_downscaler.core import nexus
from texture_downscaler.core.nexus import (
    AuthError,
    AuthorCache,
    NexusRef,
    RateLimited,
    _decode_key,
    fetch_missing,
    game_domain,
)

A = NexusRef("skyrimspecialedition", 1)
B = NexusRef("skyrimspecialedition", 2)
C = NexusRef("skyrimspecialedition", 3)


def test_decode_key_utf16_and_utf8():
    assert _decode_key("abc123==".encode("utf-16-le")) == "abc123=="
    assert _decode_key(b"abc123==") == "abc123=="
    assert _decode_key(b"") is None


def test_game_domain():
    assert game_domain("SkyrimSE") == "skyrimspecialedition"
    assert game_domain("Skyrim") == "skyrim"
    assert game_domain("Fallout4") is None


def test_fetch_missing_uses_cache_and_saves(tmp_path: Path):
    cache = AuthorCache.load(tmp_path / "authors.json")
    cache.put(A, "Alice")
    calls, results = [], []

    def fetch(ref, key):
        calls.append(ref)
        return {B: "Bob", C: ""}[ref], 1000

    msg = fetch_missing([A, B, C, B], cache, "key", lambda r, a: results.append((r, a)), delay=0, fetch=fetch)
    assert calls == [B, C]  # cached A skipped, duplicate B fetched once
    assert results == [(B, "Bob"), (C, "")]
    assert "2" in msg
    reloaded = AuthorCache.load(tmp_path / "authors.json")
    assert (reloaded.get(A), reloaded.get(B), reloaded.get(C)) == ("Alice", "Bob", "")


def test_fetch_missing_stops_on_rate_limit_and_low_quota(tmp_path: Path):
    def limited(ref, key):
        raise RateLimited()

    cache = AuthorCache(tmp_path / "a.json")
    assert "rate limit" in fetch_missing([A, B], cache, "k", lambda *a: None, delay=0, fetch=limited)
    assert cache.get(A) is None

    seen = []
    msg = fetch_missing(
        [A, B, C], cache, "k", lambda r, a: seen.append(r), delay=0, fetch=lambda r, k: ("X", 10)
    )
    assert seen == [A] and "quota" in msg


def test_fetch_missing_auth_and_network_errors(tmp_path: Path):
    def bad_key(ref, key):
        raise AuthError()

    assert "API key" in fetch_missing([A], AuthorCache(), "k", lambda *a: None, delay=0, fetch=bad_key)

    def offline(ref, key):
        raise OSError("no network")

    refs = [NexusRef("skyrimspecialedition", i) for i in range(10)]
    msg = fetch_missing(refs, AuthorCache(), "k", lambda *a: None, delay=0, fetch=offline)
    assert "Could not reach" in msg


def test_default_fetch_is_looked_up_at_call_time(monkeypatch):
    monkeypatch.setattr(nexus, "fetch_uploader", lambda ref, key: ("Patched", 999))
    got = []
    fetch_missing([A], AuthorCache(), "k", lambda r, a: got.append(a), delay=0)
    assert got == ["Patched"]
