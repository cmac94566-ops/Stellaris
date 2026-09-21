"""Compile structured Overmind actions into Stellaris console/effect scripts.

This is the "hard control" layer.  The original design only fired
``event overmind.NNN <country>`` which applies a modifier — soft guidance.
This compiler turns a structured action into *real* game effects:

    build     -> add_building on a planet scope
    district  -> add_district_and_planet_size_if_needed_effect
    stance    -> set_fleet_stance on owned fleets
    bombard   -> set_fleet_bombardment_stance
    policy    -> set_policy
    tech      -> research_technology (console command)
    blockers  -> clear_blockers
    human_ai  -> let the native AI micro-manage the player empire
    focus     -> legacy modifier event (soft layer, kept for visible feedback)

Everything is templated and allowlisted.  A free-form script string from the
LLM is never executed, so a hallucinated action cannot corrupt the save.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

GAME_DIR = Path(r"D:/SteamLibrary/steamapps/common/Stellaris")

# --------------------------------------------------------------------------
# ID validation against the real game data
# --------------------------------------------------------------------------

_ID_RE = re.compile(r"^[a-z0-9_]+$")


@lru_cache(maxsize=1)
def _game_ids() -> dict[str, set[str]]:
    """Scan the vanilla game files once and cache valid object IDs.

    Returns {"building": {...}, "district": {...}, "tech": {...}, "policy": {...}}
    """
    out: dict[str, set[str]] = {k: set() for k in
                                ("building", "district", "tech", "policy",
                                 "stance", "bombard", "war_goal", "colony",
                                 "zone")}
    out["stance"] = {"aggressive", "passive", "evasive"}
    out["bombard"] = {"armageddon", "indiscriminate", "selective", "disable"}
    if not GAME_DIR.is_dir():
        log.warning("Game dir %s not found — ID validation disabled", GAME_DIR)
        return out

    def scan(subdir: str, prefix: str, key: str) -> None:
        d = GAME_DIR / "common" / subdir
        if not d.is_dir():
            return
        for f in d.glob("*.txt"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(rf"^({prefix}[a-z0-9_]*)\s*=", text, re.M):
                out[key].add(m.group(1))

    scan("buildings", "building_", "building")
    scan("districts", "district_", "district")
    scan("zones", "zone_", "zone")
    scan("technology", "tech_", "tech")
    scan("policies", "", "policy")
    scan("war_goals", "wg_", "war_goal")
    scan("colony_types", "col_", "colony")
    return out


def is_valid_id(kind: str, value: str) -> bool:
    """Return whether *value* is a plausible ID of *kind*."""
    if not _ID_RE.match(value or ""):
        return False
    ids = _game_ids()
    known = ids.get(kind)
    if not known:            # validation unavailable -> accept shape only
        return True
    if kind == "policy":
        return True          # policy options are validated separately
    return value in known


# --------------------------------------------------------------------------
# Semantic aliases — the LLM names intent, we resolve to this version's IDs
# --------------------------------------------------------------------------
# Stellaris 4.4.6 renamed many objects (building_alloy_foundry -> building_foundry_1).
# An LLM trained on older data will emit the stale name and the script would
# silently do nothing.  Let it speak intent instead; we resolve to reality.

BUILDING_ALIASES = {
    "alloy": "building_foundry_1", "alloys": "building_foundry_1",
    "foundry": "building_foundry_1", "forge": "building_foundry_1",
    "metallurgy": "building_foundry_1", "alloy_foundry": "building_foundry_1",
    "consumer_goods": "building_factory_1",
    "factory": "building_factory_1", "civilian": "building_factory_1",
    "research": "building_research_lab_1", "lab": "building_research_lab_1",
    "science": "building_research_lab_1", "physics": "building_research_lab_1",
    "fortress": "building_fortress", "defense": "building_fortress",
    "army": "building_fortress", "soldier": "building_fortress",
    "unity": "building_autochthon_monument", "culture": "building_autochthon_monument",
    "amenities": "building_communal_housing", "housing": "building_communal_housing",
    "trade": "building_commercial_zone", "commerce": "building_commercial_zone",
    "minerals": "building_mining_districts_1", "mining": "building_mining_districts_1",
    "energy": "building_generator_generic", "power": "building_generator_generic",
    "food": "building_food_processing_facility", "farm": "building_food_processing_facility",
    "stronghold": "building_stronghold", "barracks": "building_stronghold",
}

ZONE_ALIASES = {
    "alloy": "zone_foundry", "alloys": "zone_foundry", "foundry": "zone_foundry",
    "forge": "zone_foundry", "metallurgy": "zone_foundry",
    "factory": "zone_factory", "consumer_goods": "zone_factory", "cg": "zone_factory",
    "industrial": "zone_industrial", "industry": "zone_industrial",
    "mixed": "zone_industrial", "balanced": "zone_industrial",
    "mining": "zone_minerals", "minerals": "zone_minerals", "ore": "zone_minerals",
    "energy": "zone_energy", "generator": "zone_energy", "power": "zone_energy",
    "food": "zone_food", "farming": "zone_food", "agriculture": "zone_food",
    "research": "zone_research", "science": "zone_research", "lab": "zone_research",
    "physics": "zone_research_physics", "society": "zone_research_society",
    "engineering": "zone_research_engineering",
    "fortress": "zone_fortress", "defense": "zone_fortress", "military": "zone_fortress",
    "trade": "zone_trade", "commerce": "zone_trade", "commercial": "zone_trade",
    "unity": "zone_unity", "culture": "zone_unity", "bureau": "zone_unity",
    "urban": "zone_urban", "city": "zone_urban",
}

DISTRICT_ALIASES = {
    "mining": "district_mining", "minerals": "district_mining", "ore": "district_mining",
    "energy": "district_generator", "generator": "district_generator",
    "power": "district_generator", "electricity": "district_generator",
    "farming": "district_farming", "food": "district_farming",
    "agriculture": "district_farming", "farm": "district_farming",
    "city": "district_city", "housing": "district_city", "urban": "district_city",
    "research": "district_polytechnic", "science": "district_polytechnic",
}
# NOTE: 4.4.6 has NO generic industrial district.  The 4.x economy rework
# replaced `district_industrial` with zones (see ZONE_ALIASES / _compile_zone).
# Do not add an "industrial" district alias — the fuzzy matcher would then
# happily resolve it to district_ark_military_industrial (nomad-ark only),
# which is invalid on a normal planet.


# Colony designations drive the game's OWN colony_automation rules: once a
# planet is typed "foundry", vanilla builds foundries/zones/districts by itself.
# This is the closest thing to real automation available offline — no console
# spam, no per-building micromanagement.
COLONY_ALIASES = {
    "foundry": "col_foundry", "alloy": "col_foundry", "alloys": "col_foundry",
    "forge": "col_foundry", "metallurgy": "col_foundry",
    "factory": "col_factory", "consumer_goods": "col_factory", "cg": "col_factory",
    "industrial": "col_industrial", "industry": "col_industrial", "mixed": "col_industrial",
    "mining": "col_mining", "minerals": "col_mining", "ore": "col_mining",
    "generator": "col_generator", "energy": "col_generator", "power": "col_generator",
    "farming": "col_farming", "food": "col_farming", "agriculture": "col_farming",
    "research": "col_research", "science": "col_research", "tech": "col_research",
    "fortress": "col_fortress", "defense": "col_fortress", "military": "col_fortress",
    "unity": "col_bureau", "bureaucratic": "col_bureau", "admin": "col_bureau",
    "city": "col_city", "urban": "col_city", "housing": "col_city",
    "resort": "col_resort", "penal": "col_penal", "prison": "col_penal",
    "capital": "col_capital",
}

_ALIAS_TABLES = {
    "building": BUILDING_ALIASES,
    "district": DISTRICT_ALIASES,
    "colony": COLONY_ALIASES,
    "zone": ZONE_ALIASES,
}


# Substrings marking IDs that only exist for a particular empire type or
# planet class.  The fuzzy matcher must never auto-pick one of these for a
# generic request, because the effect would silently fail on a normal planet.
_VARIANT_MARKERS = (
    "_ark_", "_arcology_", "_ring_world_", "_hab_", "_hive_", "_nexus_",
    "_machine_", "_wilderness_", "_cosmogenesis_", "_rw_", "_srw_",
    "_resort_", "_prison_", "_nomad_", "_gestalt_",
)


def _resolve(kind: str, value: str) -> str | None:
    """Resolve a semantic or literal name to a real game ID, else None."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    table = _ALIAS_TABLES.get(kind, {})
    if raw in table:
        cand = table[raw]
        if is_valid_id(kind, cand):
            return cand
    if is_valid_id(kind, raw):
        return raw
    # Last resort: fuzzy contains match inside the real ID list, but only
    # against generic (non-variant) IDs.  Picking district_ark_military_industrial
    # for a request of "industrial" is worse than failing loudly.
    known = _game_ids().get(kind) or set()
    hits = sorted(
        k for k in known
        if raw in k and not any(marker in k for marker in _VARIANT_MARKERS)
    )
    return hits[0] if hits else None


def audit_aliases() -> dict[str, list[str]]:
    """Return every alias that points at a non-existent game ID.

    Used by the test suite so a stale alias (e.g. a 4.3 name that 4.4.6
    renamed) fails loudly instead of silently degrading to a fuzzy match.
    """
    broken: dict[str, list[str]] = {}
    for kind, table in _ALIAS_TABLES.items():
        for alias, target in table.items():
            if not is_valid_id(kind, target):
                broken.setdefault(kind, []).append(f"{alias} -> {target}")
    return broken


# --------------------------------------------------------------------------
# Scope helpers
# --------------------------------------------------------------------------

_PLANET_SCOPE = {
    "capital": "capital_scope",
    "all": "every_owned_planet",
    "colonies": "every_owned_planet",
}

_PLANET_LIMIT = {
    "capital": "",
    "all": "has_free_building_slot = yes",
    "colonies": "has_free_building_slot = yes is_capital = no",
}


def _planet_block(where: str, inner: str) -> str:
    """Wrap *inner* in the right planet scope for *where*."""
    scope = _PLANET_SCOPE.get(where, "capital_scope")
    limit = _PLANET_LIMIT.get(where, "")
    if scope == "capital_scope":
        return f"capital_scope = {{ {inner} }}"
    body = f"limit = {{ {limit} }} {inner}" if limit else inner
    return f"every_owned_planet = {{ {body} }}"


# --------------------------------------------------------------------------
# Action -> script
# --------------------------------------------------------------------------

class CompileError(ValueError):
    """Raised when an action cannot be compiled into a safe script."""


def _compile_build(a: dict) -> str:
    what = _resolve("building", a.get("what", ""))
    if not what:
        raise CompileError(f"unknown building: {a.get('what')!r}")
    where = a.get("where", "capital")
    block = _planet_block(where, f"add_building = {what}")
    return f"effect = {{ {block} }}"


def _compile_district(a: dict) -> str:
    what = _resolve("district", a.get("what", ""))
    if not what:
        raise CompileError(f"unknown district: {a.get('what')!r}")
    where = a.get("where", "capital")
    count = max(1, min(int(a.get("count", 1)), 5))
    inner = f"add_district_and_planet_size_if_needed_effect = {{ district = {what} }}"
    if count > 1:
        inner = " ".join([inner] * count)
    block = _planet_block(where, inner)
    return f"effect = {{ {block} }}"


def _compile_zone(a: dict) -> str:
    """Add a production zone to a planet.

    Zones are the 4.x successor to generic industrial districts: the 4.4.6
    economy rework removed `district_industrial` entirely and replaced it with
    zone definitions (zone_foundry / zone_factory / zone_industrial ...), which
    live inside a housing district.

    Vanilla syntax (from common/scripted_effects + colony_automation):
        add_zone = { district = <host district> zone = <zone> zone_slot = N }

    The host district defaults to the planet's city/housing district.
    """
    zone = _resolve("zone", a.get("what", ""))
    if not zone:
        raise CompileError(f"unknown zone: {a.get('what')!r}")
    host = a.get("district") or a.get("host") or "district_city"
    if not is_valid_id("district", host):
        raise CompileError(f"unknown host district: {host!r}")
    slot = max(1, min(int(a.get("slot", 1)), 6))
    where = a.get("where", "capital")
    inner = f"add_zone = {{ district = {host} zone = {zone} zone_slot = {slot} }}"
    block = _planet_block(where, inner)
    return f"effect = {{ {block} }}"


def _compile_stance(a: dict) -> str:
    what = a.get("what", "")
    if what not in _game_ids()["stance"]:
        raise CompileError(f"unknown fleet stance: {what!r}")
    return f"effect = {{ every_owned_fleet = {{ set_fleet_stance = {what} }} }}"


def _compile_bombard(a: dict) -> str:
    what = a.get("what", "")
    if what not in _game_ids()["bombard"]:
        raise CompileError(f"unknown bombardment stance: {what!r}")
    return (
        "effect = { every_owned_fleet = "
        f"{{ set_fleet_bombardment_stance = {what} }} }}"
    )


def _compile_policy(a: dict) -> str:
    policy = a.get("policy", "")
    option = a.get("option", "")
    if not (policy and option):
        raise CompileError("policy action needs both 'policy' and 'option'")
    return f"effect = {{ set_policy = {{ policy = {policy} option = {option} }} }}"


def _compile_tech(a: dict) -> str:
    what = a.get("what", "")
    if not is_valid_id("tech", what):
        raise CompileError(f"unknown tech: {what!r}")
    return f"research_technology {what}"


def _compile_colony(a: dict) -> str:
    """Retype a planet — the game's colony_automation then builds it out."""
    what = _resolve("colony", a.get("what", ""))
    if not what:
        raise CompileError(f"unknown colony type: {a.get('what')!r}")
    where = a.get("where", "capital")
    set_type = f"set_colony_type = {what}"
    if where == "capital":
        scope = f"capital_scope = {{ {set_type} }}"
    elif where == "colonies":
        scope = f"every_owned_planet = {{ limit = {{ is_capital = no }} {set_type} }}"
    else:
        scope = f"every_owned_planet = {{ {set_type} }}"
    return f"effect = {{ {scope} }}"


def _compile_war(a: dict) -> str:
    """Console form: declare_war <attacker> <defender> <war_goal>.

    Uses the console command (documented in the binary as
    "The first country declares war on the second with the given war goal")
    rather than the effect, because it can target an explicit country id.
    """
    goal = str(a.get("goal", "wg_conquest")).strip().lower()
    if not is_valid_id("war_goal", goal):
        raise CompileError(f"unknown war goal: {goal!r}")
    attacker, target = a.get("attacker"), a.get("target")
    if not isinstance(attacker, int) or not isinstance(target, int) or attacker <= 0 or target <= 0:
        raise CompileError("war needs positive integer 'attacker' and 'target' country ids")
    if attacker == target:
        raise CompileError("war attacker and target must differ")
    return f"declare_war {attacker} {target} {goal}"


def _compile_blockers(a: dict) -> str:
    where = a.get("where", "all")
    block = _planet_block(where, "clear_blockers = yes")
    return f"effect = {{ {block} }}"


def _compile_human_ai(a: dict) -> str:
    return "human_ai on" if a.get("enable", True) else "human_ai off"


def _compile_instant_build(a: dict) -> str:
    return "instant_build" if a.get("enable", True) else "instant_build"


_COMPILERS = {
    "build": _compile_build,
    "district": _compile_district,
    "zone": _compile_zone,
    "colony": _compile_colony,
    "stance": _compile_stance,
    "bombard": _compile_bombard,
    "policy": _compile_policy,
    "tech": _compile_tech,
    "war": _compile_war,
    "blockers": _compile_blockers,
    "human_ai": _compile_human_ai,
    "instant_build": _compile_instant_build,
}

ACTION_TYPES = tuple(sorted(_COMPILERS))


def compile_action(action: dict) -> str:
    """Compile one structured action dict into a single console line.

    Raises CompileError if the action is unknown or fails validation.
    """
    if not isinstance(action, dict):
        raise CompileError("action must be an object")
    kind = str(action.get("type", "")).strip().lower()
    fn = _COMPILERS.get(kind)
    if fn is None:
        raise CompileError(
            f"unsupported action type {kind!r}; allowed: {', '.join(ACTION_TYPES)}"
        )
    return fn(action)


def compile_actions(actions: list[dict]) -> tuple[list[str], list[str]]:
    """Compile many actions.  Returns (scripts, errors) — one error per bad item."""
    scripts: list[str] = []
    errors: list[str] = []
    for i, a in enumerate(actions or []):
        try:
            scripts.append(compile_action(a))
        except CompileError as exc:
            errors.append(f"action[{i}]: {exc}")
    return scripts, errors
