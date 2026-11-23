import io
import json
import urllib.error
from pathlib import Path

import pytest

from texture_downscaler.core import nexus
from texture_downscaler.core.nexus import (
    AuthorCache,
    NexusError,
    NexusRef,
    Truncated,
    fetch_missing,
    fetch_uploaders,
    game_domain,
)


def ref(i: int) -> NexusRef:
    return NexusRef("skyrimspecialedition", i)


def test_game_domain():
    assert game_domain("SkyrimSE") == "skyrimspecialedition"
    assert game_domain("Skyrim") == "skyrim"
    assert game_domain("Fallout4") is None


# ------------------------------------------------------------------------ fetch_missing ----


def test_fetch_missing_batches_caches_and_marks_unknown(tmp_path: Path):
    cache = AuthorCache.load(tmp_path / "authors.json")
    cache.put(ref(1), "Cached")
    batches, results = [], []

    def fetch(batch):
        batches.append([r.mod_id for r in batch])
        return {r.key: f"U{r.mod_id}" for r in batch if r.mod_id != 4}  # mod 4: not on Nexus

    refs = [ref(i) for i in (1, 2, 3, 4, 5, 2)]
    msg = fetch_missing(
        refs, cache, lambda r, a: results.append((r.mod_id, a)), delay=0, batch_size=2, fetch=fetch
    )
    assert batches == [[2, 3], [4, 5]]  # cached and duplicate refs skipped, batches of 2
    assert results == [(2, "U2"), (3, "U3"), (4, ""), (5, "U5")]
    assert "4" in msg
    reloaded = AuthorCache.load(tmp_path / "authors.json")
    assert [reloaded.get(ref(i)) for i in (1, 2, 4, 5)] == ["Cached", "U2", "", "U5"]


def test_truncated_batches_are_split_and_retried():
    calls = []

    def fetch(batch):
        calls.append(len(batch))
        if len(batch) > 2:  # pretend Nexus only returns 2 results per response
            raise Truncated()
        return {r.key: "X" for r in batch}

    got = []
    fetch_missing(
        [ref(i) for i in range(5)],
        AuthorCache(),
        lambda r, a: got.append(r.mod_id),
        delay=0,
        batch_size=5,
        fetch=fetch,
    )
    assert calls == [5, 2, 3, 1, 2]
    assert got == [0, 1, 2, 3, 4]  # all resolved, in order


def test_errors_propagate_after_saving_partial_results(tmp_path: Path):
    def fetch(batch):
        if batch[0].mod_id >= 2:
            raise NexusError("boom", "details")
        return {r.key: "A" for r in batch}

    cache = AuthorCache(tmp_path / "a.json")
    with pytest.raises(NexusError):
        fetch_missing([ref(i) for i in range(4)], cache, lambda *a: None, delay=0, batch_size=2, fetch=fetch)
    saved = AuthorCache.load(tmp_path / "a.json")
    assert (saved.get(ref(0)), saved.get(ref(1)), saved.get(ref(2))) == ("A", "A", None)


def test_cancel_stops_between_batches():
    calls = []
    fetch_missing(
        [ref(i) for i in range(6)],
        AuthorCache(),
        lambda *a: None,
        cancelled=lambda: len(calls) >= 1,
        delay=0,
        batch_size=2,
        fetch=lambda b: calls.append(b) or {},
    )
    assert len(calls) == 1


# --------------------------------------------------------------------- fetch_uploaders ----


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _respond(monkeypatch, body=None, error=None):
    sent = {}

    def urlopen(request, timeout):
        sent["request"] = request
        if error is not None:
            raise error
        return FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(nexus.urllib.request, "urlopen", urlopen)
    return sent


def test_fetch_uploaders_sends_no_key_and_parses(monkeypatch):
    body = {
        "data": {
            "legacyModsByDomain": {
                "totalCount": 2,
                "nodes": [
                    {
                        "modId": 53000,
                        "game": {"domainName": "skyrimspecialedition"},
                        "uploader": {"name": "Parapets"},
                    },
                    {"modId": 7, "game": {"domainName": "skyrimspecialedition"}, "uploader": None},
                ],
            }
        }
    }
    sent = _respond(monkeypatch, body)
    assert fetch_uploaders([ref(53000), ref(7), ref(8)]) == {
        "skyrimspecialedition:53000": "Parapets",
        "skyrimspecialedition:7": "",
    }
    request = sent["request"]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert request.full_url == nexus.GRAPHQL_URL and request.get_method() == "POST"
    assert not {"apikey", "authorization"} & set(headers)  # never any credentials
    assert headers["application-name"] == nexus.APP_NAME
    payload = json.loads(request.data)
    assert payload["variables"]["ids"][0] == {"gameDomain": "skyrimspecialedition", "modId": 53000}
    assert payload["variables"]["count"] == 3


def test_fetch_uploaders_detects_truncated_response(monkeypatch):
    _respond(monkeypatch, {"data": {"legacyModsByDomain": {"totalCount": 3, "nodes": []}}})
    with pytest.raises(Truncated):
        fetch_uploaders([ref(1), ref(2), ref(3)])


@pytest.mark.parametrize(
    ("error", "body", "expected"),
    [
        (
            urllib.error.HTTPError(nexus.GRAPHQL_URL, 429, "Too Many", {}, io.BytesIO(b"slow down")),
            None,
            "rate limit",
        ),
        (urllib.error.HTTPError(nexus.GRAPHQL_URL, 500, "Oops", {}, io.BytesIO(b"")), None, "HTTP 500"),
        (urllib.error.URLError("offline"), None, "couldn't be reached"),
        (None, {"errors": [{"message": "Field 'x' doesn't exist"}]}, "unexpected response"),
        (None, {"data": {"somethingElse": {}}}, "unexpected response"),
    ],
)
def test_fetch_uploaders_errors_become_nexus_errors(monkeypatch, error, body, expected):
    _respond(monkeypatch, body, error)
    with pytest.raises(NexusError) as info:
        fetch_uploaders([ref(1)])
    assert expected in str(info.value)


def test_default_fetch_is_looked_up_at_call_time(monkeypatch):
    monkeypatch.setattr(nexus, "fetch_uploaders", lambda batch: {r.key: "Patched" for r in batch})
    got = []
    fetch_missing([ref(1)], AuthorCache(), lambda r, a: got.append(a), delay=0)
    assert got == ["Patched"]
