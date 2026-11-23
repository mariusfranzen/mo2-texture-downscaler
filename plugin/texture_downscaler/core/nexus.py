"""Look up mod uploaders on Nexus Mods via the public GraphQL API, with a permanent local cache.

No API key or account is used: Nexus' GraphQL v2 endpoint serves public mod metadata without
authentication. Only (game domain, mod ID) pairs are sent.

Measured against the live API (601 mod pages): `legacyModsByDomain` accepts a long id list but
returns at most 80 results per response, and offset paging over a long list is unreliable (results
went missing). So ids are sent in chunks of BATCH_SIZE (< 80) and each chunk is read in one go. If a
response still comes back short (totalCount > returned), the chunk is split in half and retried.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from .i18n import tr

NEXUS_ENABLED = True  # master switch for the Author column / any Nexus traffic

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
APP_NAME = "Texture Downscaler (MO2 plugin)"
UNKNOWN = "Unknown"
CACHE_VERSION = 1
BATCH_SIZE = 50  # the API returns at most 80 results per response

QUERY = """query Uploaders($ids: [CompositeDomainWithIdInput!]!, $count: Int) {
  legacyModsByDomain(ids: $ids, count: $count) {
    totalCount
    nodes { modId game { domainName } uploader { name } }
  }
}"""

# meta.ini gameName -> Nexus domain, for when MO2 can't tell us.
GAME_DOMAINS = {
    "skyrimse": "skyrimspecialedition",
    "skyrimspecialedition": "skyrimspecialedition",
    "skyrim": "skyrim",
    "skyrimvr": "skyrimspecialedition",
    "enderal": "enderal",
    "enderalse": "enderalspecialedition",
}


@dataclass(frozen=True, slots=True)
class NexusRef:
    game: str  # Nexus domain, e.g. "skyrimspecialedition"
    mod_id: int

    @property
    def key(self) -> str:
        return f"{self.game}:{self.mod_id}"


def game_domain(game_name: str) -> str | None:
    return GAME_DOMAINS.get(game_name.strip().lower().replace(" ", ""))


class AuthorCache:
    """ref.key -> uploader name ("" = looked up, but Nexus has no such mod / it is hidden)."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.authors: dict[str, str] = {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path) -> AuthorCache:
        cache = cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                cache.authors = {str(k): str(v) for k, v in data.get("authors", {}).items()}
        except (OSError, ValueError, AttributeError):
            pass
        return cache

    def get(self, ref: NexusRef) -> str | None:
        return self.authors.get(ref.key)

    def put(self, ref: NexusRef, author: str) -> None:
        with self._lock:
            self.authors[ref.key] = author

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            data = {"version": CACHE_VERSION, "authors": dict(sorted(self.authors.items()))}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=0, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


class NexusError(Exception):
    """A lookup failed; the message is shown to the user, `details` in the popup's details."""

    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.details = details


class Truncated(Exception):
    """Nexus returned fewer results than it found (a per-response limit)."""


def fetch_uploaders(refs: list[NexusRef], timeout: float = 30.0) -> dict[str, str]:
    """One GraphQL request. Returns ref.key -> uploader for the mods Nexus knows.

    Mods Nexus doesn't return (deleted, hidden, wrong id) are simply absent.
    Raises NexusError on HTTP/API errors and Truncated if the response was cut short.
    """
    payload = {
        "query": QUERY,
        "variables": {"ids": [{"gameDomain": r.game, "modId": r.mod_id} for r in refs], "count": len(refs)},
    }
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": __version__,
            "User-Agent": f"{APP_NAME}/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            text = ""
        if e.code == 429:
            raise NexusError(tr("authors.error_rate_limited"), f"HTTP 429\n{text}") from None
        raise NexusError(tr("authors.error_http", code=e.code), text) from None
    except (OSError, ValueError) as e:  # network down, timeout, invalid JSON
        raise NexusError(tr("authors.error_network"), f"{type(e).__name__}: {e}") from None

    if body.get("errors"):
        raise NexusError(tr("authors.error_api"), json.dumps(body["errors"], indent=1)[:2000])
    try:
        page = body["data"]["legacyModsByDomain"]
        nodes = page["nodes"]
        total = int(page.get("totalCount") or 0)
        result = {
            NexusRef(n["game"]["domainName"], int(n["modId"])).key: str(
                (n.get("uploader") or {}).get("name") or ""
            )
            for n in nodes
        }
    except (KeyError, TypeError, ValueError) as e:
        raise NexusError(tr("authors.error_api"), f"Unexpected response: {e!r}") from None
    if total > len(nodes):
        raise Truncated()
    return result


def fetch_missing(
    refs: list[NexusRef],
    cache: AuthorCache,
    on_result: Callable[[NexusRef, str], None],
    cancelled: Callable[[], bool] = lambda: False,
    delay: float = 0.2,
    batch_size: int | None = None,
    fetch: Callable[[list[NexusRef]], dict[str, str]] | None = None,
) -> str:
    """Look up uploaders for refs not in the cache, in batches. Returns a short status message.

    Raises NexusError (after saving what was found so far) when a batch fails, so the caller can
    tell the user instead of failing silently.
    """
    fetch = fetch or (lambda batch: fetch_uploaders(batch))
    batch_size = batch_size or BATCH_SIZE
    todo = [r for r in dict.fromkeys(refs) if cache.get(r) is None]
    pending = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    done = 0
    try:
        while pending:
            if cancelled():
                break
            batch = pending.pop(0)
            try:
                found = fetch(batch)
            except Truncated:
                if len(batch) == 1:
                    raise NexusError(
                        tr("authors.error_api"), "Nexus returned an incomplete result."
                    ) from None
                half = len(batch) // 2
                pending[:0] = [batch[:half], batch[half:]]  # smaller requests, same order
                continue
            for ref in batch:
                author = found.get(ref.key, "")  # not returned: not on Nexus (anymore) / hidden
                cache.put(ref, author)
                on_result(ref, author)
                done += 1
            if pending:
                time.sleep(delay)
    finally:
        cache.save()
    if not todo:
        return ""
    return tr("authors.done", count=done)
