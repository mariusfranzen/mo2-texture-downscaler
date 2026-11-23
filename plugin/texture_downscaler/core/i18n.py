"""UI text from JSON language files in `texture_downscaler/i18n/` (no Qt, no MO2).

`en.json` is the reference and always loaded; a selected language is layered on top, so any
string missing from a translation falls back to English.

A value is either a string or, for text that depends on a number, an object of plural forms:
    "files_found": {"one": "{count} file", "other": "{count} files"}
English uses "one"/"other". Languages with more forms (e.g. Russian: one/few/many) add them;
`PLURAL_RULES` picks the form for a count. Placeholders like {count} are filled with str.format.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

_log = logging.getLogger(__name__)

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"
DEFAULT_LANGUAGE = "en"


def _plural_en(n: int) -> str:
    return "one" if n == 1 else "other"


def _plural_slavic(n: int) -> str:  # Russian, Ukrainian, Belarusian
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _plural_polish(n: int) -> str:
    if n == 1:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _plural_none(n: int) -> str:  # languages without plural forms
    return "other"


def _plural_french(n: int) -> str:
    return "one" if n in (0, 1) else "other"


PLURAL_RULES: dict[str, Callable[[int], str]] = {
    "ru": _plural_slavic,
    "uk": _plural_slavic,
    "be": _plural_slavic,
    "pl": _plural_polish,
    "cs": lambda n: "one" if n == 1 else ("few" if 2 <= n <= 4 else "other"),
    "fr": _plural_french,
    "pt": _plural_french,
    "zh": _plural_none,
    "ja": _plural_none,
    "ko": _plural_none,
    "tr": _plural_none,
}

_english: dict[str, object] = {}
_strings: dict[str, object] = {}
_language = DEFAULT_LANGUAGE


def _read(code: str) -> dict[str, object]:
    path = I18N_DIR / f"{code}.json"
    try:
        # utf-8-sig: Windows Notepad may save with a BOM, which plain utf-8 json parsing rejects.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    except ValueError as e:
        _log.warning("Texture Downscaler: ignoring broken language file %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        _log.warning("Texture Downscaler: ignoring language file %s: not a JSON object", path)
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and not k.startswith("_")}


def available_languages() -> list[str]:
    return sorted(p.stem for p in I18N_DIR.glob("*.json"))


def load(language: str | None) -> str:
    """Load `language` (e.g. "ru") on top of English. Returns the language actually in use."""
    global _english, _strings, _language
    _english = _read(DEFAULT_LANGUAGE)
    code = (language or DEFAULT_LANGUAGE).strip().lower()
    translated = _read(code) if code != DEFAULT_LANGUAGE else {}
    _language = code if translated else DEFAULT_LANGUAGE
    _strings = {**_english, **translated}
    return _language


def current_language() -> str:
    return _language


def resolve_language(setting: str | None, candidates: Iterable[str]) -> str:
    """Pick a language: an explicit setting wins, else the first candidate with a translation.

    Candidates are locale names in preference order ("ru_RU", "ru-RU", "de"); they're matched
    on their language part.
    """
    available = set(available_languages())
    explicit = (setting or "").strip().lower()
    if explicit and explicit != "auto":
        return explicit if explicit in available else DEFAULT_LANGUAGE
    for name in candidates:
        code = name.replace("-", "_").split("_")[0].strip().lower()
        if code in available:
            return code
    return DEFAULT_LANGUAGE


def _format(text: str, kwargs: dict) -> str | None:
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return None  # a translation used an unknown placeholder or broken braces


def _pick(value: object, count: int | None, language: str) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if count is None:
            form = "other"
        else:
            form = PLURAL_RULES.get(language, _plural_en)(abs(count))
        for key in (form, "other", "many", "one"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def tr(key: str, **kwargs) -> str:
    """Translated text for `key`. Pass `count=` for plural-aware strings."""
    if not _english:
        load(_language)
    count = kwargs.get("count")
    count = count if isinstance(count, int) else None
    for value, language in ((_strings.get(key), _language), (_english.get(key), DEFAULT_LANGUAGE)):
        text = _pick(value, count, language)
        if text is not None and (result := _format(text, kwargs)) is not None:
            return result
    return key  # unknown key: visible, so a missing string is easy to spot


def english_keys() -> dict[str, object]:
    if not _english:
        load(_language)
    return dict(_english)
