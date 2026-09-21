"""Tests for the action compiler — Stellaris 4.4.6.

The compiler turns structured LLM intents into console scripts.  Two things
must never regress:

1. A hallucinated ID must be rejected, never silently compiled.
2. A *variant* ID must never be auto-picked for a generic request.  In 4.4.6
   there is no generic industrial district; the fuzzy matcher used to resolve
   "industrial" to `district_ark_military_industrial`, which is nomad-ark-only
   and would fail on a normal planet.  That is silent corruption, so it is
   pinned by test.
"""

from __future__ import annotations

import pytest

from engine.action_compiler import (
    ACTION_TYPES,
    CompileError,
    audit_aliases,
    compile_action,
    compile_actions,
    is_valid_id,
)


class TestIdValidation:
    """Validation runs against the real game files, so skip if absent."""

    @pytest.fixture(autouse=True)
    def _require_game(self) -> None:
        if not is_valid_id("building", "building_foundry_1"):
            pytest.skip("Stellaris game files not available")

    def test_known_ids_are_valid(self) -> None:
        assert is_valid_id("building", "building_foundry_1")
        assert is_valid_id("district", "district_mining")
        assert is_valid_id("zone", "zone_foundry")
        assert is_valid_id("colony", "col_foundry")

    def test_fabricated_ids_are_rejected(self) -> None:
        assert not is_valid_id("building", "building_totally_fake")
        assert not is_valid_id("zone", "zone_nonexistent")

    def test_no_alias_points_at_a_missing_id(self) -> None:
        """Every alias must resolve to a real 4.4.6 ID.

        Aliases written against an older patch (e.g. a pre-4.4 building name)
        would otherwise silently fall through to the fuzzy matcher and pick
        something wrong.
        """
        broken = audit_aliases()
        assert not broken, f"stale aliases: {broken}"


class TestHallucinationBlocking:
    """A fabricated ID must produce an error, not a script."""

    @pytest.fixture(autouse=True)
    def _require_game(self) -> None:
        if not is_valid_id("building", "building_foundry_1"):
            pytest.skip("Stellaris game files not available")

    @pytest.mark.parametrize("action", [
        {"type": "build", "what": "building_totally_fake"},
        {"type": "zone", "what": "zone_totally_fake"},
        {"type": "tech", "what": "tech_nonexistent_zzz"},
        {"type": "colony", "what": "not_a_colony"},
        {"type": "stance", "what": "not_a_stance"},
    ])
    def test_rejects_hallucinated_id(self, action: dict) -> None:
        with pytest.raises(CompileError):
            compile_action(action)

    def test_unknown_action_type_rejected(self) -> None:
        with pytest.raises(CompileError):
            compile_action({"type": "delete_everything"})

    def test_non_dict_action_rejected(self) -> None:
        with pytest.raises(CompileError):
            compile_action("build a foundry")


class TestVariantTrap:
    """Generic requests must never silently resolve to variant-only IDs."""

    @pytest.fixture(autouse=True)
    def _require_game(self) -> None:
        if not is_valid_id("building", "building_foundry_1"):
            pytest.skip("Stellaris game files not available")

    def test_industrial_district_is_refused_not_mismatched(self) -> None:
        """4.4.6 has no generic industrial district — it must fail.

        The old fuzzy matcher returned district_ark_military_industrial.
        """
        with pytest.raises(CompileError, match="unknown district"):
            compile_action({"type": "district", "what": "industrial"})

    def test_industrial_zone_resolves_to_the_generic_one(self) -> None:
        line = compile_action({"type": "zone", "what": "industrial"})
        assert "zone = zone_industrial" in line
        assert "_ark_" not in line
        assert "_ring_world_" not in line

    def test_no_compiled_line_contains_a_variant_id(self) -> None:
        """Regression guard across every generic alias."""
        from engine.action_compiler import ZONE_ALIASES

        for word in ZONE_ALIASES:
            line = compile_action({"type": "zone", "what": word})
            for marker in ("_ark_", "_ring_world_", "_arcology_", "_hab_"):
                assert marker not in line, f"{word} -> {line}"


class TestZoneCompilation:
    @pytest.fixture(autouse=True)
    def _require_game(self) -> None:
        if not is_valid_id("building", "building_foundry_1"):
            pytest.skip("Stellaris game files not available")

    def test_zone_uses_vanilla_add_zone_syntax(self) -> None:
        line = compile_action({"type": "zone", "what": "foundry", "where": "capital"})
        assert line.startswith("effect = {")
        assert "add_zone = {" in line
        assert "district = district_city" in line
        assert "zone = zone_foundry" in line
        assert "zone_slot = 1" in line

    def test_zone_slot_is_clamped(self) -> None:
        assert "zone_slot = 1" in compile_action(
            {"type": "zone", "what": "foundry", "slot": 0}
        )
        assert "zone_slot = 6" in compile_action(
            {"type": "zone", "what": "foundry", "slot": 99}
        )

    def test_zone_honours_custom_host_district(self) -> None:
        line = compile_action(
            {"type": "zone", "what": "foundry", "district": "district_city"}
        )
        assert "district = district_city" in line

    def test_zone_rejects_unknown_host_district(self) -> None:
        with pytest.raises(CompileError, match="host district"):
            compile_action(
                {"type": "zone", "what": "foundry", "district": "district_nope"}
            )

    def test_zone_is_a_known_action_type(self) -> None:
        assert "zone" in ACTION_TYPES


class TestBatchCompilation:
    @pytest.fixture(autouse=True)
    def _require_game(self) -> None:
        if not is_valid_id("building", "building_foundry_1"):
            pytest.skip("Stellaris game files not available")

    def test_one_bad_action_does_not_drop_the_good_ones(self) -> None:
        lines, errors = compile_actions([
            {"type": "build", "what": "alloy_foundry", "where": "capital"},
            {"type": "build", "what": "building_fake_zzz", "where": "capital"},
            {"type": "policy", "policy": "economic_policy",
             "option": "economic_policy_military"},
        ])
        assert len(lines) == 2
        assert len(errors) == 1
        assert "building_fake_zzz" in errors[0]

    def test_alloy_foundry_alias_resolves(self) -> None:
        lines, errors = compile_actions(
            [{"type": "build", "what": "alloy_foundry", "where": "capital"}]
        )
        assert not errors
        assert "add_building = building_foundry_1" in lines[0]

    def test_policy_uses_state_override_effect(self) -> None:
        lines, errors = compile_actions([
            {"type": "policy", "policy": "economic_policy",
             "option": "economic_policy_military"},
        ])
        assert not errors
        assert "set_policy" in lines[0]
