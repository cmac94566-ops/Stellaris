"""Script audit — verify that mod script only references things the game knows.

This is the safety net that makes the autonomy layer trustworthy.  A wrong name
or ID in Clausewitz **fails silently**: the effect simply does nothing (that is
how this project once shipped an ``industrial`` district request that was
quietly resolved to a nomad-ark-only district, and how five invented trigger
names survived into a release).  The game logs nothing useful, so the only
defence is to check every identifier against the installed data files before
shipping.

Two independent checks run over every ``.txt`` in the mod:

1. **ID sweep** — any bare token in a known ID namespace (``building_``,
   ``district_``, ``zone_`` …) must be defined in the corresponding folder of
   the game installation *or* by the mod itself.
2. **Key sweep** — every left-hand-side key (``add_district``, ``free_housing``
   in ``key = value`` or ``key = {``) must appear somewhere in the game's own
   script corpus, unless it is mod-local.  This is what catches *invented
   trigger and effect names*, which check 1 cannot see at all.
3. **Entry-schema sweep** — every key sitting directly inside a top-level entry
   must also appear in an entry of the *same folder* in the game.  Check 2 is a
   global vocabulary test and therefore blind to a key borrowed from a
   different folder's schema; this one is not.  It caught
   ``ai_will_do`` in ``common/edicts`` (the game uses ``ai_weight`` there),
   which halted edict parsing at load.

Both checks are deliberately conservative about two things that produced false
positives in an earlier revision (see ``docs/BACKLOG.md`` D-1/D-2):

* a token in **key position** (``district_type = district_mining``) is a key,
  never an ID, so only the value is checked;
* IDs the mod defines itself (``opinion_overmind_goodwill``) are legitimate.

The auditor must be *accurate*, not merely strict — a noisy gate gets disabled,
and then it protects nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Registry: ID prefix -> the game folders that define that namespace
# ---------------------------------------------------------------------------
ID_SOURCES: dict[str, tuple[str, ...]] = {
    "building_": ("common/buildings",),
    "district_": ("common/districts",),
    "zone_": ("common/zones",),
    "pc_": ("common/planet_classes",),
    "col_": ("common/colony_types",),
    "living_standard_": ("common/species_rights/living_standards",),
    "opinion_": ("common/opinion_modifiers",),
    "ethic_": ("common/ethics",),
    "civic_": ("common/governments/civics",),
    "ap_": ("common/ascension_perks",),
    "trait_": ("common/traits",),
    "tech_": ("common/technology",),
    "sr_": ("common/strategic_resources",),
    "origin_": ("common/governments/civics", "common/origins"),
    "colony_type_": ("common/colony_types",),
    "tradition_": ("common/traditions",),
    "tr_": ("common/traditions",),
    "megastructure_": ("common/megastructures",),
    "job_": ("common/pop_jobs",),
    "army_": ("common/armies",),
}

#: Namespaces that belong to the mod itself and must not be checked.  Anything
#: starting with one of these is either a mod-local key or a mod-local ID.
MOD_LOCAL_PREFIXES = ("overmind", "om_")

POLICY_DIR = "common/policies"
DESIGN_DIR = "common/global_ship_designs"

#: Folders whose top-level names are the **mod's own inventions** (scripted
#: effect/trigger names, scripted variables), so a same-folder vocabulary check
#: would be meaningless — the mod is the only authority on those names.
OPEN_NAMESPACE_DIRS: tuple[str, ...] = (
    "common/scripted_effects",
    "common/scripted_triggers",
    "common/scripted_variables",
    "common/inline_scripts",
)

#: Game folders scanned when building the "known key" vocabulary.  Anything the
#: mod writes as a key must appear here.  ``common/`` already contains
#: ``common/inline_scripts/``, so one entry covers both.
KEY_CORPUS_DIRS: tuple[str, ...] = ("common", "events", "decisions", "gfx")

_TOP_KEY = re.compile(r"^([a-z][a-z_0-9]*)\s*=\s*\{", re.M)
_OPTION_NAME = re.compile(r'name\s*=\s*"?([a-z][a-z_0-9]*)"?')
_DESIGN_NAME = re.compile(r'name\s*=\s*"?([A-Za-z_0-9]+)"?')

#: Every left-hand side that starts a ``key <op> value`` pair.  The operator may
#: be an assignment or a **comparison** — ``num_traditions < 49`` is just as much
#: a use of a trigger name as ``has_technology = tech_x``, and an earlier version
#: of this regex missed every comparison, which made the whole key sweep blind to
#: a large class of invented triggers.  Used both to harvest the game's key
#: vocabulary and to find keys inside mod files.
_LHS = re.compile(r"(?<![\w\"'])([a-z][a-z_0-9]{2,})\s*(?:[<>]=?|!?=)(?!=)", re.M)
#: A left-hand side that refers to a *string* value, e.g. ``design = "NAME_AI_Core"``.
_STRING_ASSIGN = re.compile(r'^\s*([A-Za-z_0-9]+)\s*=\s*"([^"]+)"', re.M)

#: Engine keywords that legitimately appear on the left of ``=`` in mod script
#: but are not data-defined names (numbers, booleans, resource shorthands).
_KEYWORD_STOPLIST: frozenset[str] = frozenset(
    {
        "yes",
        "no",
        "root",
        "from",
        "who",
        "this",
        "prev",
        "owner",
        "value",
        "amount",
        "type",
        "name",
        "trigger",
        "limit",
        "hidden_trigger",
        "inline_script",
        "script",
        "and",
        "or",
        "not",
        "nor",
        "nand",
        "if",
        "else",
        "else_if",
        "while",
        "switch",
        "case",
        "default",
    }
)


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    kind: str
    token: str
    hint: str = ""

    def __str__(self) -> str:  # pragma: no cover - formatting helper
        loc = f"{self.file}:{self.line}"
        extra = f"  ({self.hint})" if self.hint else ""
        return f"{loc}: {self.kind}: {self.token}{extra}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def strip_comments(text: str) -> str:
    """Blank out comments while preserving line numbering."""
    out: list[str] = []
    for line in text.splitlines():
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def iter_txt(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.txt") if p.is_file())


def collect_namespace(prefix: str, dirs: tuple[str, ...], game_dir: Path) -> set[str]:
    """Collect every top-level ``prefix*`` key defined in the given folders."""
    found: set[str] = set()
    pattern = re.compile(rf"^({re.escape(prefix)}[a-z_0-9]*)\s*=\s*\{{", re.M)
    for rel in dirs:
        for path in iter_txt(game_dir / rel):
            text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            found.update(m.group(1) for m in pattern.finditer(text))
    return found


def collect_all_ids(root: Path) -> dict[str, set[str]]:
    """Every ID in every namespace, harvested from ``root``.

    ``root`` may be the game installation **or** the mod directory; the auditor
    merges both so that IDs the mod defines for itself are accepted.
    """
    return {p: collect_namespace(p, d, root) for p, d in ID_SOURCES.items()}


def collect_game_keys(game_dir: Path) -> set[str]:
    """Every left-hand-side key the game's own scripts use.

    This is the vocabulary an invented trigger name would be missing from.
    """
    keys: set[str] = set()
    for rel in KEY_CORPUS_DIRS:
        for path in iter_txt(game_dir / rel):
            text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            keys.update(m.group(1) for m in _LHS.finditer(text))
    return keys


#: The engine stores its own identifier pool as NUL-delimited strings, which
#: makes it a precise oracle for names that never appear in script at all.
_EXE_TOKEN = re.compile(rb"\x00([a-z][a-z_0-9]{2,60})\x00")
_EXE_NAME = "stellaris.exe"
_exe_cache: dict[tuple[str, float], set[str]] = {}


def collect_exe_vocabulary(game_dir: Path) -> set[str]:
    """Identifiers the engine itself knows, read from ``stellaris.exe``.

    The script corpus alone is **not** sufficient authority.  Static-modifier
    keys such as ``country_ship_upgrade_cost_mult`` are real engine modifiers
    that no script ever writes down, so a script-only sweep reports them as
    invented — a false positive that would make the gate unusable.  The exe's
    NUL-delimited token pool contains exactly those names, and does *not*
    contain genuinely invented ones, so it is the right third source.

    Returns an empty set when the binary is absent (e.g. unit tests).
    """
    exe = game_dir / _EXE_NAME
    if not exe.exists():
        return set()
    try:
        key = (str(exe), exe.stat().st_mtime)
    except OSError:  # pragma: no cover - unreadable file
        return set()
    cached = _exe_cache.get(key)
    if cached is not None:
        return cached

    data = exe.read_bytes()
    # The pool is not clean — it also holds short fragments like ``aaaa`` that
    # are not identifiers at all.  Requiring an underscore removes that noise
    # without losing any real modifier name, and a real one-word name would be
    # caught by the script corpus anyway.  Without this, a typo could in
    # principle be waved through by coincidence.
    found = {
        m.group(1).decode("ascii")
        for m in _EXE_TOKEN.finditer(data)
        if b"_" in m.group(1)
    }
    _exe_cache.clear()  # one game install at a time; keeps memory bounded
    _exe_cache[key] = found
    return found


def collect_policies(game_dir: Path) -> dict[str, set[str]]:
    """``{policy_key: {option_key, ...}}`` from ``common/policies``."""
    out: dict[str, set[str]] = {}
    for path in iter_txt(game_dir / POLICY_DIR):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in _TOP_KEY.finditer(text):
            key = m.group(1)
            body = _block(text, m.end() - 1)
            options = {om.group(1) for om in _OPTION_NAME.finditer(body)}
            if options:
                out.setdefault(key, set()).update(options)
    return out


def collect_designs(game_dir: Path) -> set[str]:
    """Ship-design names usable in ``create_ship = { design = ... }``."""
    out: set[str] = set()
    for path in iter_txt(game_dir / DESIGN_DIR):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in re.finditer(r"ship_design\s*=\s*\{", text):
            body = _block(text, m.end() - 1)
            nm = _DESIGN_NAME.search(body)
            if nm:
                out.add(nm.group(1))
    return out


def _block(text: str, open_brace_index: int) -> str:
    """Return the contents of the ``{ ... }`` starting at the given index."""
    depth = 0
    for i in range(open_brace_index, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_index + 1 : i]
    return text[open_brace_index + 1 :]


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def scan_tokens(text: str) -> list[tuple[int, str]]:
    """``(line, token)`` for every ID-shaped token used as a *value*.

    Tokens in key position are dropped: in ``district_type = district_mining``
    only ``district_mining`` is an ID reference.  Without this rule the
    auditor reported ``district_type`` as an unknown district (D-1).
    """
    results: list[tuple[int, str]] = []
    prefixes = tuple(ID_SOURCES)
    token_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in prefixes) + r")[a-z_0-9]+")
    stripped = strip_comments(text)
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        for m in token_re.finditer(line):
            token = m.group(0)
            if token.startswith(MOD_LOCAL_PREFIXES):
                continue
            # Key position?  Look at what follows the token on the same line.
            rest = line[m.end() :]
            if re.match(r"\s*=[^=]", rest):
                continue
            results.append((line_no, token))
    return results


def scan_keys(text: str) -> list[tuple[int, str]]:
    """``(line, key)`` for every left-hand-side key in the text."""
    out: list[tuple[int, str]] = []
    stripped = strip_comments(text)
    for m in _LHS.finditer(stripped):
        key = m.group(1)
        if key in _KEYWORD_STOPLIST or key.startswith(MOD_LOCAL_PREFIXES):
            continue
        out.append((stripped[: m.start()].count("\n") + 1, key))
    return out


def scan_entry_keys(text: str) -> list[tuple[int, str]]:
    """``(line, key)`` for the **direct keys of each top-level entry block**.

    This is the third check, and it exists because the key sweep is a *global*
    vocabulary test: it asks "does this name appear anywhere in the game's
    script?", which cannot tell a legal key from one borrowed out of a
    different folder's schema.  A real failure walked straight through it:

        #  common/edicts/overmind_autonomy_edicts.txt
        ai_will_do = { factor = 0 }     #  -> "Unexpected token: ai_will_do"

    ``ai_will_do`` is a genuine key — in ``common/decisions``.  Edicts use
    ``ai_weight``.  Both are in the global corpus, so only a *per-directory*
    check can see the difference.

    The rule is deliberately shallow.  Only the keys that sit **directly**
    inside an entry are schema; everything deeper (``has_country_flag`` inside
    ``potential``) is trigger/effect language, whose vocabulary is global and
    would produce false positives here.

    Scanned character-wise rather than line-wise: several vanilla files write an
    inner block on one line (``resources = { category = edicts cost = { ... } }``),
    and a line-based reader would misjudge the depth of everything after it.
    """
    out: list[tuple[int, str]] = []
    src = strip_comments(text)
    depth = 0
    line_no = 1
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "\n":
            line_no += 1
            i += 1
        elif ch == '"':
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\n":
                    line_no += 1
                i += 1
            i += 1
        elif ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            i += 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            if depth == 1:
                k = j
                while k < n and src[k] in " \t":
                    k += 1
                if k < n and src[k] == "=" and (k + 1 >= n or src[k + 1] != "="):
                    out.append((line_no, word))
                    i = k + 1  # step past '=' so the value cannot read as a key
                    continue
            i = j
        else:
            i += 1
    return out


def vocabulary_dir(rel: Path) -> str | None:
    """The vanilla folder whose entry schema governs ``rel``.

    ``common/<sub>/x.txt`` maps to ``common/<sub>``; a top-level folder such as
    ``events/`` maps to itself.  Returns ``None`` for folders whose top-level
    names the mod is *supposed* to invent (see ``OPEN_NAMESPACE_DIRS``).
    """
    parts = rel.parts
    if len(parts) < 2:
        return None
    key = "/".join(parts[:2]) if parts[0] == "common" else parts[0]
    if key in OPEN_NAMESPACE_DIRS:
        return None
    return key


def collect_entry_schemas(game_dir: Path, dirs: tuple[str, ...]) -> dict[str, set[str]]:
    """``{dir: {direct entry key, ...}}`` harvested from the game's own files."""
    out: dict[str, set[str]] = {}
    for rel in dirs:
        found: set[str] = set()
        for path in iter_txt(game_dir / rel):
            found.update(k for _, k in scan_entry_keys(
                path.read_text(encoding="utf-8", errors="replace")
            ))
        out[rel] = found
    return out


def scan_policy_pairs(text: str) -> list[tuple[int, str, str]]:
    """Yield ``(line, policy, option)`` for every ``policy/option`` pair."""
    out: list[tuple[int, str, str]] = []
    stripped = strip_comments(text)
    for m in re.finditer(
        r"policy\s*=\s*([a-z_0-9]+)\s*\n?\s*option\s*=\s*([a-z_0-9]+)", stripped
    ):
        line = stripped[: m.start()].count("\n") + 1
        out.append((line, m.group(1), m.group(2)))
    return out


def scan_designs(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    stripped = strip_comments(text)
    for m in re.finditer(r'design\s*=\s*"([^"]+)"', stripped):
        line = stripped[: m.start()].count("\n") + 1
        out.append((line, m.group(1)))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _merge_ids(*tables: dict[str, set[str]]) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for table in tables:
        for prefix, names in table.items():
            merged.setdefault(prefix, set()).update(names)
    return merged


def audit_mod(
    mod_dir: Path,
    game_dir: Path,
    *,
    check_keys: bool = True,
) -> list[Violation]:
    """Audit every script file under ``mod_dir`` against the installation.

    ``check_keys`` can be switched off for speed in tight loops; the ID sweep
    runs either way.
    """
    valid_ids = _merge_ids(collect_all_ids(game_dir), collect_all_ids(mod_dir))
    #: Names the mod defines as its own top-level IDs (``opinion_mod_goodwill``
    #: in ``common/opinion_modifiers/``).  Declaring one is legitimate, so the
    #: key sweep must not report the declaration itself as an invented key.
    self_defined = set().union(*collect_all_ids(mod_dir).values())
    valid_policies = collect_policies(game_dir)
    valid_designs = collect_designs(game_dir)
    game_keys = collect_game_keys(game_dir) if check_keys else set()
    engine_keys = collect_exe_vocabulary(game_dir) if check_keys else set()

    #: Per-directory entry schemas, gathered only for the folders this mod
    #: actually writes into — the game tree is large and the check is worthless
    #: for folders we do not touch.
    mod_files = iter_txt(mod_dir)
    governed_dirs = tuple(sorted({
        d for d in (
            vocabulary_dir(p.relative_to(mod_dir)) for p in mod_files
        ) if d is not None
    }))
    entry_schemas = (
        collect_entry_schemas(game_dir, governed_dirs) if check_keys else {}
    )

    violations: list[Violation] = []

    for path in mod_files:
        rel_path = path.relative_to(mod_dir)
        rel = str(rel_path)
        text = path.read_text(encoding="utf-8", errors="replace")

        for line, token in scan_tokens(text):
            for prefix, known in valid_ids.items():
                if not token.startswith(prefix):
                    continue
                if token in known:
                    break
                violations.append(
                    Violation(rel, line, f"unknown {prefix}* id", token)
                )
                break

        if check_keys:
            for line, key in scan_keys(text):
                if key in game_keys or key in engine_keys or key in self_defined:
                    continue
                violations.append(
                    Violation(
                        rel,
                        line,
                        "unknown key",
                        key,
                        hint="不在游戏脚本语料，也不在引擎标识符表中",
                    )
                )

            # Same-folder schema check: catches keys that exist somewhere in the
            # game but not in *this* folder's entry schema (the ai_will_do /
            # ai_weight confusion in common/edicts).
            gov = vocabulary_dir(rel_path)
            schema = entry_schemas.get(gov) if gov else None
            if schema:
                for line, key in scan_entry_keys(text):
                    if key in schema or key.startswith(MOD_LOCAL_PREFIXES):
                        continue
                    violations.append(
                        Violation(
                            rel,
                            line,
                            f"key not in the '{gov}' entry schema",
                            key,
                            hint="游戏同名目录的条目里没有这个键",
                        )
                    )

        for line, policy, option in scan_policy_pairs(text):
            if policy not in valid_policies:
                violations.append(Violation(rel, line, "unknown policy", policy))
            elif option not in valid_policies[policy]:
                violations.append(
                    Violation(
                        rel,
                        line,
                        f"policy '{policy}' has no option",
                        option,
                        hint=", ".join(sorted(valid_policies[policy])[:6]),
                    )
                )

        for line, design in scan_designs(text):
            if design not in valid_designs:
                violations.append(Violation(rel, line, "unknown ship design", design))

    violations.sort(key=lambda v: (v.file, v.line, v.token))
    return violations


def format_report(violations: list[Violation]) -> str:
    if not violations:
        return "OK: 脚本引用的 ID 与键名全部在游戏数据或 mod 自身定义中。"
    lines = [f"发现 {len(violations)} 处无效引用："]
    lines.extend("  " + str(v) for v in violations)
    return "\n".join(lines)


def game_dir_from_config(config_path: Path | None = None) -> Path:
    import tomllib

    path = config_path or (Path(__file__).resolve().parent.parent / "config.toml")
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    return Path(cfg["stellaris"]["install_dir"])


def mod_dir_from_config(config_path: Path | None = None) -> Path:
    import tomllib

    path = config_path or (Path(__file__).resolve().parent.parent / "config.toml")
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    return (
        Path(__file__).resolve().parent.parent
        / "mod"
        / cfg["stellaris"]["mod_name"]
    )
