"""Tests for the launcher's engine-switch verifier.

``scripts/overmind_takeover.py`` exists because the project must start the game
*without* the console, which means it has to pass the engine's own
``-continuelastsave`` switch — and to trust that switch, it reads the switch
table straight out of ``stellaris.exe``.  A verifier that reports real switches
as unregistered is worse than no verifier: it trains you to ignore its output.

The regression pinned here is exactly that: the table stores ``game_paused``
with its default value attached (``game_paused false``), and an earlier
normalizer compared the whole string, so ``--unpause`` printed

    引擎不识别: -game_paused

for a switch the binary documents as "Toggles/Sets the game paused state".

These tests use synthetic blobs, so they need no game installation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "overmind_takeover.py"


def _load_module():
    """Import the launcher by path — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("overmind_takeover", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


takeover = _load_module()


# ---------------------------------------------------------------------------
# Table entry normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [
    ("berserk_ai", "berserk_ai"),            # bare switch
    ("humanai", "humanai"),                  # legacy spelling, no underscore
    ("game_paused false", "game_paused"),    # switch + boolean default
    ("threads=", "threads"),                 # switch with an inline value
    ("quick=", "quick"),
    ("-editor", "editor"),                   # dash already present
    ("  overnight  ", "overnight"),          # padded
])
def test_normalize_flag_token(raw: str, expected: str) -> None:
    assert takeover._normalize_flag_token(raw) == expected


def test_normalize_keeps_documented_paused_switch() -> None:
    """The exact string the engine stores must survive normalization."""
    assert takeover._normalize_flag_token("game_paused false") == "game_paused"


# ---------------------------------------------------------------------------
# Table extraction from a synthetic binary
# ---------------------------------------------------------------------------
def _synthetic_exe(tmp_path: Path) -> Path:
    anchor = b"common/resource_converters\x00"
    entries = [
        b"", b"start", b"-editor", b"threads=", b"userdir=",
        b"berserkai", b"berserk_ai", b"fullgalaxyspawn", b"overnight",
        b"game_paused false", b"gamestatetimer", b"logempirestats",
    ]
    blob = (
        anchor
        + b"\x00".join(entries)
        + b"\x00main task\x00"
        + b"-continuelastsave\x00"
    )
    exe = tmp_path / "fake_stellaris.exe"
    exe.write_bytes(blob)
    return exe


def test_table_pass_extracts_normalized_names(tmp_path: Path) -> None:
    flags = takeover.discover_cli_flags(_synthetic_exe(tmp_path), whole_file=False)
    assert "game_paused" in flags
    assert "berserk_ai" in flags
    assert "overnight" in flags
    assert "threads" in flags
    assert "userdir" in flags
    assert "gamestatetimer" in flags
    assert "editor" in flags
    # the table-end sentinel and the data-dir anchor must not leak through
    assert "main" not in flags
    assert "task" not in flags
    assert "common/resource_converters" not in flags


def test_table_pass_does_not_skip_every_other_entry(tmp_path: Path) -> None:
    """Regression: a NUL-consuming regex silently dropped half the table."""
    flags = takeover.discover_cli_flags(_synthetic_exe(tmp_path), whole_file=False)
    for neighbour_pair in (("berserkai", "berserk_ai"),
                           ("fullgalaxyspawn", "overnight"),
                           ("gamestatetimer", "logempirestats")):
        assert neighbour_pair[0] in flags, f"{neighbour_pair[0]} 被漏掉了"
        assert neighbour_pair[1] in flags, f"{neighbour_pair[1]} 被漏掉了"


def test_table_tokens_are_dash_free(tmp_path: Path) -> None:
    flags = takeover.discover_cli_flags(_synthetic_exe(tmp_path), whole_file=False)
    assert not [f for f in flags if f.startswith("-")]


def test_whole_file_pass_finds_the_switch_that_matters(tmp_path: Path) -> None:
    """``-continuelastsave`` lives outside the table; the sweep must catch it."""
    flags = takeover.discover_cli_flags(_synthetic_exe(tmp_path), whole_file=True)
    assert "continuelastsave" in flags


# ---------------------------------------------------------------------------
# verify_flags: the thing that prints the warning
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point ``discover_cli_flags`` at the synthetic binary, once."""
    exe = _synthetic_exe(tmp_path)
    real = takeover.discover_cli_flags

    def patched(*_a, **_k):
        return real(exe)

    monkeypatch.setattr(takeover, "discover_cli_flags", patched)
    return exe


def test_verify_accepts_the_table_switches(fake_table) -> None:
    ok, bad = takeover.verify_flags(["-continuelastsave", "-game_paused", "false"])
    assert ok == ["-continuelastsave", "-game_paused"]
    assert bad == []


def test_verify_still_rejects_an_invented_switch(fake_table) -> None:
    ok, bad = takeover.verify_flags(["-continuelastsave", "-definitely_not_a_flag"])
    assert ok == ["-continuelastsave"]
    assert bad == ["-definitely_not_a_flag"]


# ---------------------------------------------------------------------------
# Flag assembly policy (D-3)
# ---------------------------------------------------------------------------
def _args(**kw):
    import argparse

    base = dict(
        no_continue=False, human_ai=False, unpause=False, berserk=False,
        overnight=False, console=False, quick=False, extra=[],
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_continue_last_save_is_default() -> None:
    assert takeover.build_flags(_args()) == ["-continuelastsave"]


def test_human_ai_is_never_a_default() -> None:
    """D-3: handing the empire to the built-in AI defeats the whole project."""
    assert "-human_ai" not in takeover.build_flags(_args())
    assert "-human_ai" in takeover.build_flags(_args(human_ai=True))


def test_no_continue_drops_the_switch() -> None:
    assert takeover.build_flags(_args(no_continue=True)) == []


def test_unpause_expands_to_a_pair() -> None:
    assert takeover.build_flags(_args(unpause=True)) == [
        "-continuelastsave", "-game_paused", "false",
    ]
