"""Translation files and the tr() lookup."""

import json
import re
import shutil
import string
from pathlib import Path

import pytest

from texture_downscaler.core import i18n
from texture_downscaler.core.i18n import tr

PLUGIN_DIR = Path(i18n.__file__).resolve().parent.parent
EN = json.loads((PLUGIN_DIR / "i18n" / "en.json").read_text(encoding="utf-8"))
EN_KEYS = {k for k in EN if not k.startswith("_")}


def _source_files():
    return [p for p in PLUGIN_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def _placeholders(value) -> set[str]:
    texts = value.values() if isinstance(value, dict) else [value]
    return {name for text in texts for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.fixture(autouse=True)
def english():
    i18n.load("en")
    yield
    i18n.load("en")


@pytest.fixture
def lang_dir(tmp_path, monkeypatch):
    """A private i18n folder with the real en.json, for writing test translations."""
    shutil.copy(PLUGIN_DIR / "i18n" / "en.json", tmp_path / "en.json")
    monkeypatch.setattr(i18n, "I18N_DIR", tmp_path)
    return tmp_path


def test_every_key_used_in_code_exists_and_every_english_key_is_used():
    source = "\n".join(p.read_text(encoding="utf-8") for p in _source_files())
    called = set(re.findall(r'\btr\(\s*"([^"]+)"', source))
    assert called - EN_KEYS == set(), "tr() keys missing from en.json"
    # Keys can also be referenced indirectly (column/type key tables), so look for any literal.
    unused = {k for k in EN_KEYS if f'"{k}"' not in source}
    assert unused == set(), "en.json keys not used anywhere"


def test_english_plural_entries_have_one_and_other():
    for key, value in EN.items():
        if isinstance(value, dict):
            assert {"one", "other"} <= set(value), key


def test_all_translations_are_valid_and_use_only_known_placeholders():
    for path in (PLUGIN_DIR / "i18n").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))  # must be valid JSON
        for key, value in data.items():
            if key.startswith("_"):
                continue
            assert key in EN_KEYS, f"{path.name}: unknown key {key}"
            assert isinstance(value, (str, dict)), f"{path.name}: {key}"
            assert _placeholders(value) <= _placeholders(EN[key]), f"{path.name}: {key} placeholders"


def test_no_hardcoded_english_in_ui_widgets():
    """Guard against new UI text that bypasses tr()."""
    pattern = re.compile(
        r"\b(QLabel|QPushButton|QCheckBox|QGroupBox|addItem|addAction|addMenu|addTab|addRow|setToolTip|"
        r'setText|setPlaceholderText|setLabelText|information|critical|question)\(\s*(?:[^,()]*,\s*)?f?"[A-Za-z]'
    )
    allowed = {'"Texture Downscaler"'}  # product name
    offenders = []
    for path in _source_files():
        if path.parent.name not in ("ui",) and path.name != "plugin.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not any(a in line for a in allowed):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


def test_english_text_and_plurals():
    assert tr("basic.mark") == "Mark for downscaling"
    assert tr("bottom.downscale", count=1) == "Downscale 1 marked texture..."
    assert tr("bottom.downscale", count=3) == "Downscale 3 marked textures..."
    assert tr("menu.hide_mod", mod="SMIM") == 'Hide mod "SMIM"'
    assert tr("no.such.key") == "no.such.key"


def test_translation_with_fallback_and_russian_plurals(lang_dir):
    (lang_dir / "ru.json").write_text(
        json.dumps(
            {
                "basic.mark": "Отметить для уменьшения",
                "bottom.downscale": {
                    "one": "Уменьшить {count} текстуру...",
                    "few": "Уменьшить {count} текстуры...",
                    "many": "Уменьшить {count} текстур...",
                },
                "menu.hide_mod": "Скрыть мод «{mod}» {oops}",  # broken placeholder -> English
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert i18n.load("ru") == "ru"
    assert tr("basic.mark") == "Отметить для уменьшения"
    assert tr("bottom.downscale", count=1) == "Уменьшить 1 текстуру..."
    assert tr("bottom.downscale", count=3) == "Уменьшить 3 текстуры..."
    assert tr("bottom.downscale", count=5) == "Уменьшить 5 текстур..."
    assert tr("bottom.downscale", count=11) == "Уменьшить 11 текстур..."
    assert tr("bottom.downscale", count=21) == "Уменьшить 21 текстуру..."
    assert tr("basic.unmark") == "Unmark (keep original size)"  # missing -> English
    assert tr("menu.hide_mod", mod="SMIM") == 'Hide mod "SMIM"'  # broken -> English


def test_unknown_or_broken_language_falls_back_to_english(lang_dir):
    assert i18n.load("xx") == "en"
    (lang_dir / "de.json").write_text("{ not json", encoding="utf-8")
    assert i18n.load("de") == "en"
    assert tr("basic.mark") == "Mark for downscaling"


def test_resolve_language(lang_dir):
    (lang_dir / "ru.json").write_text("{}", encoding="utf-8")
    assert i18n.resolve_language("", ["ru_RU", "en"]) == "ru"  # MO2 / Windows language
    assert i18n.resolve_language("", ["de-DE", "ru-RU"]) == "ru"  # first one with a translation
    assert i18n.resolve_language("", ["de-DE"]) == "en"
    assert i18n.resolve_language("en", ["ru_RU"]) == "en"  # explicit setting wins
    assert i18n.resolve_language("RU", []) == "ru"
    assert i18n.resolve_language("fr", ["ru_RU"]) == "en"  # explicit but not available
