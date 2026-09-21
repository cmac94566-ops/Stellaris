"""Tests for the script auditor — the gate that keeps silent failures out.

Two layers:

* **Unit tests** run against a synthetic game tree, so they need no game
  installation and they pin the exact false positives that shipped before
  (``docs/BACKLOG.md`` D-1/D-2) plus the checks that must *not* go quiet.
* **Integration tests** audit the real mod against the real installation.  They
  skip cleanly when the game is absent, because the auditor's whole job is to
  read game data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.script_audit import (  # noqa: E402
    OPEN_NAMESPACE_DIRS,
    Violation,
    audit_mod,
    collect_entry_schemas,
    collect_game_keys,
    game_dir_from_config,
    scan_entry_keys,
    scan_keys,
    scan_tokens,
    strip_comments,
    vocabulary_dir,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _game_dir() -> Path:
    try:
        return game_dir_from_config()
    except Exception:  # pragma: no cover - config missing
        return Path("__no_game__")


GAME_DIR = _game_dir()
MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"

requires_game = pytest.mark.skipif(
    not (GAME_DIR / "common").is_dir(),
    reason="需要已安装的 Stellaris 数据才能审计",
)


@pytest.fixture
def mini_game(tmp_path: Path) -> Path:
    """A tiny stand-in installation with just enough data to audit.

    The ``events`` file is the stand-in for "the game's own script corpus": it
    deliberately contains the key names the synthetic mods below use, including
    one written in **comparison** form, because that shape was invisible to an
    earlier version of the key sweep.
    """
    game = tmp_path / "game"
    (game / "common/districts").mkdir(parents=True)
    (game / "common/districts/00_districts.txt").write_text(
        "district_mining = {\n\tresources = { cost = { minerals = 100 } }\n}\n"
        "district_type = { }\n",
        encoding="utf-8",
    )
    (game / "common/buildings").mkdir(parents=True)
    (game / "common/buildings/00_b.txt").write_text(
        "building_lab = { }\n", encoding="utf-8"
    )
    (game / "common/edicts").mkdir(parents=True)
    (game / "common/edicts/00_edicts.txt").write_text(
        # The entry schema the mod's own edicts are measured against.  Note it
        # carries ``ai_weight`` and deliberately *not* ``ai_will_do`` — that
        # asymmetry is the real 4.4.6 shape and is what the new check pins.
        'edict_real = {\n'
        '\tlength = -1\n'
        '\ticon = "GFX_edict_type_policy"\n'
        '\tresources = {\n\t\tcategory = edicts\n\t\tcost = {\n\t\t\tunity = 1\n\t\t}\n\t}\n'
        '\tpotential = {\n\t\tis_ai = no\n\t}\n'
        '\tallow = {\n\t\talways = yes\n\t}\n'
        '\teffect = {\n\t\tadd_resource = { unity = 1 }\n\t}\n'
        '\tai_weight = {\n\t\tweight = 500\n\t}\n'
        '}\n',
        encoding="utf-8",
    )
    (game / "common/policies").mkdir(parents=True)
    (game / "common/policies/00_p.txt").write_text(
        "economic_policy = {\n\toption = { name = \"economic_policy_balanced\" }\n}\n",
        encoding="utf-8",
    )
    (game / "common/scripted_variables").mkdir(parents=True)
    (game / "common/scripted_variables/00_vars.txt").write_text(
        "@max_tradition_trees = 7\n", encoding="utf-8"
    )
    (game / "events").mkdir(parents=True)
    (game / "events/00_e.txt").write_text(
        "add_district = { district_type = district_mining }\n"
        "add_opinion_modifier = { modifier = opinion_real_one }\n"
        "opinion = { value > 0 }\n"
        "add_building = { building = building_lab }\n"
        "set_policy = { policy = economic_policy option = economic_policy_balanced }\n"
        "set_variable = { which = some_var value = 1 }\n"
        "check_variable = { which = some_var value >= 1 }\n"
        # Comparison form on purpose — see test_comparison_position_keys_are_collected.
        "free_building_slots > 0\n"
        "has_technology = tech_lasers_1\n",
        encoding="utf-8",
    )
    return game


@pytest.fixture
def mini_mod(tmp_path: Path) -> Path:
    mod = tmp_path / "mod"
    (mod / "common/scripted_effects").mkdir(parents=True)
    return mod


def _write(mod: Path, rel: str, text: str) -> None:
    path = mod / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# D-1: a token in key position is a key, not an ID
# ---------------------------------------------------------------------------
def test_key_position_token_is_not_an_id() -> None:
    """`district_type = district_mining` must yield only `district_mining`."""
    tokens = [t for _line, t in scan_tokens("add_district = { district_type = district_mining }")]
    assert "district_mining" in tokens
    assert "district_type" not in tokens


def test_d1_regression_no_district_type_violation(mini_game: Path, mini_mod: Path) -> None:
    _write(
        mini_mod,
        "common/scripted_effects/x.txt",
        "overmind_x = {\n\tadd_district = {\n\t\tdistrict_type = district_mining\n\t}\n}\n",
    )
    assert audit_mod(mini_mod, mini_game) == []


# ---------------------------------------------------------------------------
# D-2: IDs the mod defines for itself are legitimate
# ---------------------------------------------------------------------------
def test_d2_regression_mod_defined_id_accepted(mini_game: Path, mini_mod: Path) -> None:
    _write(
        mini_mod,
        "common/opinion_modifiers/own.txt",
        "opinion_mod_goodwill = {\n\topinion = 25\n}\n",
    )
    _write(
        mini_mod,
        "common/scripted_effects/use.txt",
        "overmind_use = {\n\tadd_opinion_modifier = { modifier = opinion_mod_goodwill }\n}\n",
    )
    assert audit_mod(mini_mod, mini_game) == []


def test_unknown_id_still_caught(mini_game: Path, mini_mod: Path) -> None:
    _write(
        mini_mod,
        "common/scripted_effects/y.txt",
        "overmind_y = {\n\tadd_district = { district_type = district_not_real }\n}\n",
    )
    violations = audit_mod(mini_mod, mini_game)
    assert [v.token for v in violations] == ["district_not_real"]


# ---------------------------------------------------------------------------
# Key sweep: invented trigger / effect names
# ---------------------------------------------------------------------------
def test_invented_trigger_and_effect_are_caught(mini_game: Path, mini_mod: Path) -> None:
    _write(
        mini_mod,
        "common/scripted_effects/z.txt",
        "overmind_z = {\n"
        "\tlimit = {\n"
        "\t\ttotally_invented_trigger = yes\n"
        "\t\tfree_building_slots > 0\n"
        "\t}\n"
        "\tadd_invented_thing = { x = 1 }\n"
        "\tadd_district = { district_type = district_mining }\n"
        "}\n",
    )
    caught = {v.token for v in audit_mod(mini_mod, mini_game) if v.kind == "unknown key"}
    assert "totally_invented_trigger" in caught
    assert "add_invented_thing" in caught
    # Real names must survive.
    assert "free_building_slots" not in caught
    assert "add_district" not in caught


def test_comparison_position_keys_are_collected(mini_game: Path) -> None:
    """`num_traditions < 49` style uses must count as key uses.

    An earlier regex only matched ``key = value``, which made the key sweep
    blind to every trigger written in comparison form — including the invented
    ones.  The fixture's corpus contains ``free_building_slots > 0`` in that
    exact shape, which is what this test is pinning.
    """
    text = "limit = {\n\tfree_building_slots > 0\n\thas_technology = tech_lasers_1\n}\n"
    keys = {k for _line, k in scan_keys(text)}
    assert {"free_building_slots", "has_technology"} <= keys

    corpus = collect_game_keys(mini_game)
    assert "free_building_slots" in corpus, "比较运算符位置的键没有被语料收集到"
    assert "has_technology" in corpus, "赋值位置的键没有被语料收集到"
    assert "num_traditions" not in corpus


def test_comments_and_mod_local_names_are_ignored(mini_game: Path, mini_mod: Path) -> None:
    _write(
        mini_mod,
        "common/scripted_effects/c.txt",
        "# invented_thing_in_a_comment = yes\n"
        "overmind_c = {\n"
        "\tovermind_helper_of_our_own = yes\n"
        "\tom_local_flag = yes\n"
        "}\n",
    )
    assert audit_mod(mini_mod, mini_game) == []


# ---------------------------------------------------------------------------
# Entry-schema sweep: a key can exist in the game and still be wrong here
# ---------------------------------------------------------------------------
def test_scan_entry_keys_reads_only_direct_keys() -> None:
    """Depth matters: only keys directly inside an entry are schema."""
    text = (
        "edict_x = {\n"
        "\tlength = -1\n"
        "\tpotential = {\n"
        "\t\tis_ai = no\n"          # depth 2 -> trigger language, not schema
        "\t\tNOT = { has_country_flag = f }\n"
        "\t}\n"
        "}\n"
    )
    keys = {k for _line, k in scan_entry_keys(text)}
    assert keys == {"length", "potential"}


def test_scan_entry_keys_survives_inline_blocks() -> None:
    keys = {k for _line, k in scan_entry_keys("a = { b = { c = 1 } d = 2 }\n")}
    assert keys == {"b", "d"}


def test_vocabulary_dir_mapping() -> None:
    assert vocabulary_dir(Path("common/edicts/x.txt")) == "common/edicts"
    assert vocabulary_dir(Path("events/x.txt")) == "events"
    # Folders where the mod authors the names itself are exempt.
    for rel in OPEN_NAMESPACE_DIRS:
        assert vocabulary_dir(Path(rel) / "x.txt") is None


def test_entry_key_from_another_folders_schema_is_caught(
    mini_game: Path, mini_mod: Path
) -> None:
    """The real 4.4.6 failure: edicts use ``ai_weight``, not ``ai_will_do``.

    ``ai_will_do`` is a genuine key — in ``common/decisions`` — so the global
    key sweep is satisfied by it.  Only the same-folder schema check can tell
    that an edict may not use it.  In game this surfaced as

        persistent.cpp: Error: "Unexpected token: ai_will_do, near line: 40"
    """
    _write(
        mini_mod,
        "common/edicts/own.txt",
        "overmind_edict = {\n"
        "\tlength = -1\n"
        "\tai_will_do = {\n\t\tfactor = 0\n\t}\n"
        "}\n",
    )
    caught = [
        v for v in audit_mod(mini_mod, mini_game)
        if v.kind.startswith("key not in the")
    ]
    assert [v.token for v in caught] == ["ai_will_do"]
    assert "common/edicts" in caught[0].kind


def test_entry_key_in_same_folder_schema_is_accepted(
    mini_game: Path, mini_mod: Path
) -> None:
    """The corrected spelling must pass — otherwise the gate is just noise."""
    _write(
        mini_mod,
        "common/edicts/own.txt",
        "overmind_edict = {\n"
        "\tlength = -1\n"
        "\ticon = \"GFX_edict_type_policy\"\n"
        "\tresources = {\n\t\tcategory = edicts\n\t\tcost = {\n\t\t\tunity = 1\n\t\t}\n\t}\n"
        "\tpotential = {\n\t\tis_ai = no\n\t}\n"
        "\tallow = {\n\t\talways = yes\n\t}\n"
        "\teffect = {\n\t\tovermind_engage = yes\n\t}\n"
        "\tai_weight = {\n\t\tweight = 0\n\t}\n"
        "}\n",
    )
    assert audit_mod(mini_mod, mini_game) == []


def test_trigger_keys_inside_an_entry_are_not_schema_checked(
    mini_game: Path, mini_mod: Path
) -> None:
    """``is_ai``/``always`` live at depth 2 — flagging them would be noise."""
    _write(
        mini_mod,
        "common/edicts/own.txt",
        "overmind_edict = {\n"
        "\tpotential = {\n"
        "\t\tis_something_the_edict_schema_never_saw = no\n"
        "\t}\n"
        "}\n",
    )
    schemas = [
        v for v in audit_mod(mini_mod, mini_game) if "entry schema" in v.kind
    ]
    assert schemas == []


def test_open_namespace_dirs_are_exempt(mini_game: Path, mini_mod: Path) -> None:
    """Scripted effects/triggers invent their own names on purpose."""
    _write(
        mini_mod,
        "common/scripted_effects/own.txt",
        "overmind_own_helper = {\n\tnot_a_real_key = yes\n}\n",
    )
    assert not [
        v for v in audit_mod(mini_mod, mini_game) if "entry schema" in v.kind
    ]


def test_collect_entry_schemas_reads_the_game(mini_game: Path) -> None:
    schemas = collect_entry_schemas(mini_game, ("common/edicts",))
    assert "ai_weight" in schemas["common/edicts"]
    assert "ai_will_do" not in schemas["common/edicts"]
    # Depth-2 keys must not leak into the schema.
    assert "is_ai" not in schemas["common/edicts"]


# ---------------------------------------------------------------------------
# Policy / option pairing
# ---------------------------------------------------------------------------
def test_policy_option_must_exist(mini_game: Path, mini_mod: Path) -> None:
    _write(
        mini_mod,
        "common/scripted_effects/p.txt",
        "overmind_p = {\n"
        "\tset_policy = { policy = economic_policy option = economic_policy_balanced }\n"
        "\tset_policy = { policy = economic_policy option = economic_policy_nonsense }\n"
        "}\n",
    )
    kinds = {(v.kind, v.token) for v in audit_mod(mini_mod, mini_game)}
    assert ("policy 'economic_policy' has no option", "economic_policy_nonsense") in kinds


# ---------------------------------------------------------------------------
# Integration: the real mod, the real game — AC-1 / NFR-02
# ---------------------------------------------------------------------------
@requires_game
def test_real_mod_audits_clean() -> None:
    """AC-1: zero violations, and (because D-1/D-2 are fixed) zero false positives."""
    violations = audit_mod(MOD_DIR, GAME_DIR)
    report = "\n".join(str(v) for v in violations)
    assert violations == [], f"审计未通过：\n{report}"


@requires_game
def test_real_game_key_vocabulary_is_substantial() -> None:
    keys = collect_game_keys(GAME_DIR)
    assert len(keys) > 20_000, "键名语料提取失效——审计会因此变成空壳"


@requires_game
def test_real_edicts_use_the_edict_schema() -> None:
    """Pin the fix for the load-halting ``ai_will_do`` in the real mod."""
    text = (MOD_DIR / "common/edicts/overmind_autonomy_edicts.txt").read_text(
        encoding="utf-8"
    )
    active = strip_comments(text)
    assert "ai_will_do" not in active, "法令里重新出现了 decisions 的键"
    assert active.count("ai_weight") == 2, "两条法令都该带 ai_weight"

    schema = collect_entry_schemas(GAME_DIR, ("common/edicts",))["common/edicts"]
    assert "ai_weight" in schema
    assert "ai_will_do" not in schema
