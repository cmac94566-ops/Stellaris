"""Nomads support tests for Stellaris 4.4.6."""

from __future__ import annotations

from engine.meta_loader import load_meta
from engine.save_reader import _extract_empire_info


def test_voidfarers_origin_is_nomadic() -> None:
    empire = _extract_empire_info(
        {
            "government": {
                "type": "gov_democracy",
                "origin": "origin_voidfarers",
            },
        },
        "Voidfarers",
    )

    assert empire["is_nomadic"] is True


def test_nomadic_flag_is_preserved() -> None:
    empire = _extract_empire_info(
        {"is_nomadic": True, "government": {"type": "gov_democracy"}},
        "Nomads",
    )

    assert empire["is_nomadic"] is True


def test_settled_empire_is_not_nomadic() -> None:
    empire = _extract_empire_info(
        {"government": {"type": "gov_democracy", "origin": "origin_default"}},
        "Settled",
    )

    assert empire["is_nomadic"] is False


def test_446_meta_pack_contains_nomad_guidance() -> None:
    meta = load_meta("4.4.6")

    assert meta["version"] == "4.4.6"
    assert "Voidfarers" in meta["nomads"]["origins"]
    assert meta["forbidden_weapons"] == []
