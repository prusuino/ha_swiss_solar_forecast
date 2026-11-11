"""The texts: every language carries every key of strings.json, and the
list entries of the flow that are not a roof or a plane have a name."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BASE = Path(__file__).parent.parent / "custom_components" / "swiss_solar_forecast"
LANGUAGES = ("en", "de", "fr", "it")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(node, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    return set().union(*(_keys(value, f"{prefix}.{key}" if prefix else key) for key, value in node.items()))


def test_english_is_the_source() -> None:
    assert _load(BASE / "translations" / "en.json") == _load(BASE / "strings.json")


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_language_has_every_key(language: str) -> None:
    assert _keys(_load(BASE / "translations" / f"{language}.json")) == _keys(_load(BASE / "strings.json"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_list_entries_have_a_name(language: str) -> None:
    """The flow sends "manual" and "plant" with an English fallback; the
    frontend shows the selector translation in their place."""
    texts = _load(BASE / "translations" / f"{language}.json")
    assert texts["selector"]["roofs"]["options"]["manual"]
    assert texts["selector"]["plane"]["options"]["plant"]
    for flow in ("config", "options"):
        assert "{entry}" in texts[flow]["error"]["meter_in_use"]
