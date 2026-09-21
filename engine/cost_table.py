"""Cost table — resolve what the game actually charges for a district/zone/building.

Why this module exists
----------------------
``add_district`` / ``add_zone`` / ``add_building`` are *instant* effects: they
bypass the construction queue, so the game never charges for them.  The
autonomy executor therefore has to debit the treasury itself, and the only
honest price to debit is **the price the game would have charged**.

An earlier revision hardcoded those prices by hand (120/150/300/350 minerals).
That was wrong in both directions: it overcharged zones (a zone in 4.4.6 costs
``@zone_cost``, not a made-up 300) and it would silently drift the moment the
game rebalanced.  So the prices are now *derived from the game's own data*.

How a price is expressed in 4.4.6
---------------------------------
A definition carries::

    resources = {
        cost = { minerals = @city_cost }          # or several, each with a trigger
        upkeep = { energy = 2 }
    }

Three complications, all handled here:

1. **Scripted variables.**  ``@city_cost`` is defined at the top of the very
   district file that uses it.  So the collector harvests ``@name = <expr>``
   from ``common/scripted_variables/`` *and* from the heads of the data files
   themselves.  Expressions may reference other variables (``@a * @b``).

2. **Inline scripts.**  Buildings and zones rarely inline their cost.  They
   write ``inline_script = { script = buildings/nomadic_cost_switcher COST = @b1_minerals }``,
   and the real ``cost`` block lives in ``common/inline_scripts/``.  So the body
   is expanded (one level, parameters substituted) before it is read.

3. **Triggered alternatives.**  A building may list one ``cost`` for regular
   empires and another for nomads.  We prefer the block whose trigger says
   ``is_nomadic = no`` — the Overmind only governs regular empires.

The result is a plain ``{id: {resource: amount}}`` table.  ``engine/costs.py``
turns it into the mod's scripted-variable file, so the debit is always the real
price.  ``tests/test_cost_table.py`` pins a few known values so a game patch
cannot silently change them without the suite going red.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Folders that define cost-bearing content, and the prefix of their keys.
COST_SOURCES: dict[str, str] = {
    "district_": "common/districts",
    "zone_": "common/zones",
    "building_": "common/buildings",
    # Outposts and their upgrades are ship sizes, and their price lives with
    # every other ship size.
    "starbase_": "common/ship_sizes",
}

#: Folders whose contents are cost-bearing but whose keys carry **no prefix**
#: (armies are just ``assault_army``, ``defense_army`` …), so every top-level
#: key in them is taken as an ID.
BARE_SOURCES: tuple[str, ...] = ("common/armies",)

#: Where ``@name = value`` may be defined.
VARIABLE_DIRS: tuple[str, ...] = (
    "common/scripted_variables",
    "common/inline_scripts",
)

INLINE_SCRIPT_ROOT = "common/inline_scripts"

#: Resources a build can be charged in.  Anything else in a ``cost`` block is
#: not a price (``trigger``/``mult``/macro parameters).
RESOURCE_NAMES: frozenset[str] = frozenset(
    {
        "energy",
        "minerals",
        "food",
        "alloys",
        "consumer_goods",
        "unity",
        "influence",
        "physics_research",
        "society_research",
        "engineering_research",
        "sr_dark_matter",
        "sr_living_metal",
        "sr_zro",
        "sr_crystals",
        "sr_gas",
        "sr_motes",
        "sr_astral_thread",
        "sr_minor_artifacts",
    }
)

_VAR_DEF = re.compile(r"^\s*@([a-z_0-9]+)\s*=\s*(.+?)\s*$", re.M)
_TOP_KEY = re.compile(r"^([a-z][a-z_0-9]*)\s*=\s*\{", re.M)
#: Inline-script parameters are conventionally *upper case* (``COST = @b1_minerals``),
#: so this must not be case-restricted to lower case like the other patterns.
_ASSIGN = re.compile(r"([A-Za-z_0-9]+)\s*=\s*(\S+)")
_VAR_REF = re.compile(r"@([a-z_0-9]+)")


# ---------------------------------------------------------------------------
# Scripted variables
# ---------------------------------------------------------------------------
def strip_comments(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def iter_txt(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.txt") if p.is_file())


def collect_variable_texts(game_dir: Path) -> dict[str, str]:
    """Every ``@name`` definition, as raw expression text."""
    texts: dict[str, str] = {}
    for rel in VARIABLE_DIRS:
        for path in iter_txt(game_dir / rel):
            for m in _VAR_DEF.finditer(strip_comments(path.read_text(encoding="utf-8", errors="replace"))):
                texts.setdefault(m.group(1), m.group(2))

    # Data files may define variables at their head (e.g. ``@city_cost = 500``
    # at the top of 00_urban_districts.txt).  Those definitions are what the
    # file's own ``cost`` blocks reference, so they must be harvested too.
    for rel in COST_SOURCES.values():
        for path in iter_txt(game_dir / rel):
            head = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            for m in _VAR_DEF.finditer(head):
                texts.setdefault(m.group(1), m.group(2))
    return texts


def resolve_variables(texts: dict[str, str]) -> dict[str, int]:
    """Evaluate the definitions to integers, following ``@`` references."""
    resolved: dict[str, int] = {}
    pending = dict(texts)
    for _ in range(12):  # generous: chains are short, cycles are impossible in practice
        progressed = False
        for name, expr in list(pending.items()):
            value = _eval_expr(expr, resolved)
            if value is not None:
                resolved[name] = value
                del pending[name]
                progressed = True
        if not pending or not progressed:
            break
    return resolved


def _eval_expr(expr: str, known: dict[str, int], depth: int = 0) -> int | None:
    """Evaluate a Clausewitz scripted-variable expression to an int."""
    if depth > 8:
        return None
    expr = expr.strip().rstrip(",")
    if not expr:
        return None
    # Drop trailing inline comments and any non-arithmetic decorators.
    expr = expr.split("#")[0].strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", expr):
        return int(float(expr))

    # Expand @refs into numbers where known; give up otherwise.
    def sub(m: re.Match[str]) -> str:
        value = known.get(m.group(1))
        return str(value) if value is not None else "None"

    expanded = _VAR_REF.sub(sub, expr)
    if "None" in expanded or not re.fullmatch(r"[-+*/()\d\s.]+", expanded):
        return None
    try:
        return int(eval(expanded, {"__builtins__": {}}, {}))  # noqa: S307 - digits/operators only
    except (SyntaxError, ZeroDivisionError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------
def extract_block(text: str, open_index: int) -> str:
    """Contents of the ``{...}`` whose opening brace is at ``open_index``."""
    depth = 0
    for i in range(open_index, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
    return text[open_index + 1 :]


def _sub_blocks(body: str, key: str) -> list[str]:
    """Every ``key = { ... }`` body inside ``body``, in order."""
    out: list[str] = []
    for m in re.finditer(rf"\b{re.escape(key)}\s*=\s*\{{", body):
        out.append(extract_block(body, m.end() - 1))
    return out


def _expand_inline_scripts(body: str, game_dir: Path, depth: int = 0) -> str:
    """Replace ``inline_script = { script = a/b P = v }`` with the file's text.

    One level of nesting is the norm; two are supported for safety.
    """
    if depth > 3 or "inline_script" not in body:
        return body

    def repl(m: re.Match[str]) -> str:
        block = extract_block(body, m.end() - 1)
        ref = re.search(r"\bscript\s*=\s*\"?([A-Za-z_0-9/\.]+)\"?", block)
        if not ref:
            return ""
        target = game_dir / INLINE_SCRIPT_ROOT / f"{ref.group(1)}.txt"
        if not target.exists():
            return ""
        text = strip_comments(target.read_text(encoding="utf-8", errors="replace"))
        params = {
            k: v
            for k, v in _ASSIGN.findall(block)
            if k not in ("script",) and not v.startswith("{")
        }
        for name, value in params.items():
            text = text.replace(f"${name}$", value)
        # Unsubstituted parameters would corrupt parsing; drop those lines.
        text = "\n".join(l for l in text.splitlines() if "$" not in l)
        return _expand_inline_scripts(text, game_dir, depth + 1)

    return re.sub(r"\binline_script\s*=\s*\{", repl, body)


# ---------------------------------------------------------------------------
# Cost extraction
# ---------------------------------------------------------------------------
@dataclass
class CostEntry:
    costs: dict[str, int] = field(default_factory=dict)
    upkeep: dict[str, int] = field(default_factory=dict)
    source: str = ""
    note: str = ""

    @property
    def is_free(self) -> bool:
        return not self.costs


def _pick_cost_block(blocks: list[str]) -> str | None:
    """Prefer the block meant for regular empires; otherwise the first."""
    if not blocks:
        return None
    for block in blocks:
        trigger = re.search(r"\btrigger\s*=\s*\{", block)
        if trigger:
            trigger_body = extract_block(block, trigger.end() - 1)
            if re.search(r"is_nomadic\s*=\s*no", trigger_body):
                return block
    for block in blocks:
        if "trigger" not in block:
            return block
    return blocks[0]


def _parse_cost_block(block: str, variables: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    # Only look at the block's own top level, so a nested trigger's numbers
    # cannot be mistaken for a price.
    body = block
    for m in re.finditer(r"\b([a-z_0-9]+)\s*=\s*(-?\d+|@[a-z_0-9]+)", body):
        key, raw = m.group(1), m.group(2)
        if key not in RESOURCE_NAMES:
            continue
        if raw.startswith("@"):
            value = variables.get(raw[1:])
            if value is None:
                continue
        else:
            value = int(raw)
        out[key] = out.get(key, 0) + value
    return out


def _resolve_one(text: str, game_dir: Path, variables: dict[str, int]) -> tuple[dict[str, int], dict[str, int], str]:
    """``(costs, upkeep, note)`` for a single definition body."""
    expanded = _expand_inline_scripts(text, game_dir)

    resources = _sub_blocks(expanded, "resources")
    # A zone may declare its cost without a ``resources`` wrapper.
    if not resources:
        resources = [expanded]

    costs: dict[str, int] = {}
    upkeep: dict[str, int] = {}
    for res in resources:
        chosen = _pick_cost_block(_sub_blocks(res, "cost"))
        if chosen:
            costs.update(_parse_cost_block(chosen, variables))
        for up in _sub_blocks(res, "upkeep"):
            upkeep.update(_parse_cost_block(up, variables))

    note = ""
    if not costs and not upkeep and any(_VAR_REF.search(b) for b in _sub_blocks(expanded, "cost")):
        note = "unresolved scripted variable"
    return costs, upkeep, note


def collect_costs(game_dir: Path) -> dict[str, CostEntry]:
    """Resolve the build price of every district / zone / building / army."""
    variables = resolve_variables(collect_variable_texts(game_dir))
    entries: dict[str, CostEntry] = {}

    sources: list[tuple[Path, str | None]] = [
        (game_dir / rel, prefix) for prefix, rel in COST_SOURCES.items()
    ]
    sources.extend((game_dir / rel, None) for rel in BARE_SOURCES)

    for root, prefix in sources:
        for path in iter_txt(root):
            text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            for m in _TOP_KEY.finditer(text):
                key = m.group(1)
                if prefix is not None and not key.startswith(prefix):
                    continue
                if key in entries:
                    continue
                body = extract_block(text, m.end() - 1)
                costs, upkeep, note = _resolve_one(body, game_dir, variables)
                entries[key] = CostEntry(
                    costs=costs,
                    upkeep=upkeep,
                    source=str(path.relative_to(game_dir)),
                    note=note,
                )
    return entries


def unresolved_variable_names(game_dir: Path) -> dict[str, str]:
    """``{name: raw expression}`` for definitions that never resolved."""
    texts = collect_variable_texts(game_dir)
    resolved = resolve_variables(texts)
    return {n: e for n, e in texts.items() if n not in resolved}
