"""Look up mod uploaders on Nexus Mods, with a permanent on-disk cache.

The API key is read from the Windows Credential Manager entry MO2 creates when the user logs in
to Nexus (`ModOrganizer2_APIKEY`), or passed in explicitly. The key is never logged or stored.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import __version__

# Switched off for public releases until the app is registered with Nexus Mods: their API
# Acceptable Use Policy doesn't allow personal API keys (or keys issued to another app, such as
# MO2's) in public-facing applications. With this False, nothing is ever sent to Nexus and the
# Author column, its settings and its menu entries are hidden. Re-enable together with SSO login
# using the slug Nexus issues on registration.
NEXUS_ENABLED = False

API_URL = "https://api.nexusmods.com/v1/games/{game}/mods/{mod_id}.json"
MO2_CREDENTIAL = "ModOrganizer2_APIKEY"
APP_NAME = "Texture Downscaler (MO2 plugin)"
UNKNOWN = "Unknown"
MIN_HOURLY_REMAINING = 50  # leave some quota for MO2's own update checks
CACHE_VERSION = 1

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


def read_mo2_api_key() -> str | None:
    """Read the Nexus API key MO2 stored in the Windows Credential Manager."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_ptr = ctypes.POINTER(CREDENTIAL)()
    CRED_TYPE_GENERIC = 1
    if not advapi.CredReadW(MO2_CREDENTIAL, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return None
    try:
        cred = cred_ptr.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        advapi.CredFree(cred_ptr)
    return _decode_key(blob)


def _decode_key(blob: bytes) -> str | None:
    # MO2 writes the key as UTF-16; fall back to UTF-8 just in case.
    for encoding in ("utf-16-le", "utf-8"):
        try:
            key = blob.decode(encoding).strip("\x00 \r\n")
        except UnicodeDecodeError:
            continue
        if key and key.isascii() and key.isprintable():
            return key
    return None


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


class RateLimited(Exception):
    pass


class AuthError(Exception):
    pass


def fetch_uploader(ref: NexusRef, api_key: str, timeout: float = 15.0) -> tuple[str, int | None]:
    """Return (uploaded_by, hourly requests remaining). uploaded_by is "" if the mod is unavailable."""
    request = urllib.request.Request(
        API_URL.format(game=ref.game, mod_id=ref.mod_id),
        headers={
            "apikey": api_key,
            "Accept": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": __version__,
            "User-Agent": f"{APP_NAME}/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            remaining = _int_or_none(response.headers.get("x-rl-hourly-remaining"))
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited() from None
        if e.code in (401, 403):
            raise AuthError() from None
        if e.code in (404, 410):
            return "", _int_or_none(e.headers.get("x-rl-hourly-remaining"))
        raise
    uploader = data.get("uploaded_by") or (data.get("user") or {}).get("name") or ""
    return str(uploader), remaining


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_missing(
    refs: list[NexusRef],
    cache: AuthorCache,
    api_key: str,
    on_result: Callable[[NexusRef, str], None],
    cancelled: Callable[[], bool] = lambda: False,
    delay: float = 0.1,
    fetch: Callable[[NexusRef, str], tuple[str, int | None]] | None = None,
) -> str:
    """Fetch uploaders for refs not in the cache, one at a time. Returns a short status message."""
    fetch = fetch or fetch_uploader
    todo = [r for r in dict.fromkeys(refs) if cache.get(r) is None]
    done = 0
    errors = 0
    for ref in todo:
        if cancelled():
            break
        try:
            author, remaining = fetch(ref, api_key)
        except RateLimited:
            return f"Nexus rate limit reached; looked up {done} of {len(todo)} authors. The rest next time."
        except AuthError:
            return "Nexus rejected the API key. Log in to Nexus again in MO2's settings."
        except (OSError, ValueError):  # network down, timeouts, bad JSON
            errors += 1
            if errors >= 5 and done == 0:
                return "Could not reach Nexus Mods; authors will be looked up next time."
            continue
        cache.put(ref, author)
        on_result(ref, author)
        done += 1
        if done % 25 == 0:
            cache.save()
        if remaining is not None and remaining < MIN_HOURLY_REMAINING:
            cache.save()
            return f"Stopped to save Nexus API quota; looked up {done} of {len(todo)} authors."
        time.sleep(delay)
    cache.save()
    if not todo:
        return ""
    return f"Looked up {done} mod authors on Nexus." + (f" {errors} failed." if errors else "")
