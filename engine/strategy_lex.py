"""Strategy Lex compiler — turn the LLM's strategy into in-game law.

Why this module exists
----------------------
Clausewitz accepts **no external input**: it cannot read files at runtime and
exposes no remote console.  The only moment the game is willing to read the
outside world is **when it loads scripts**.  So the only way for an external
large model to actually *govern* an empire — with no console, and without
typing anything — is for its decisions to be **compiled into mod script** and
picked up on the next load.

That compiled script is called the *Lex* (法案): not "one order per turn" but
"the standing law of this empire".  It is a small set of integer variables:

    om_lex_slot1..8     the monthly agenda (phase codes 1-8, one phase per month)
    om_lex_threat       threat level         1=low 2=moderate 3=high 4=critical
    om_lex_bottleneck   what RELIEF fixes    1=energy 2=minerals 3=food
    om_lex_stance       1=defensive 2=balanced 3=aggressive
    om_lex_focus_zone   1=industrial 2=foundry 3=research 4=trade 5=unity
    om_lex_tradition_mode 1=tech 2=wide 3=war 4=diplo
    om_lex_ap_mode      1=tech 2=wide 3=war 4=trade
    om_lex_bombardment  1=selective 2=indiscriminate 3=raiding
    om_lex_rev          revision stamp, so a reload swaps in the new law

The in-game executor (`mod/.../common/scripted_effects/overmind_autonomy.txt`)
reads those variables and acts.  Nothing free-form is ever emitted as script:
the LLM picks from a closed set of codes, which is why this compiler can
guarantee that the generated file is valid Stellaris script.

Only two files are ever written, and both are marked as generated:

    common/scripted_variables/overmind_lex_vars.txt   @om_lex_rev = <rev>
    common/scripted_effects/overmind_lex.txt          overmind_lex_apply

The revision stamp lives in ``common/scripted_variables/`` because that is the
only directory whose ``@name`` definitions are globally visible to other
script files.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Closed code sets — the whole vocabulary the LLM is allowed to speak
# ---------------------------------------------------------------------------

PHASE_RELIEF = 1
PHASE_HOUSING = 2
PHASE_JOBS = 3
PHASE_AMENITIES = 4
PHASE_SCIENCE = 5
PHASE_MILITARY = 6
PHASE_ASCENSION = 7
PHASE_WAR = 8

PHASE_NAMES: dict[int, str] = {
    PHASE_RELIEF: "relief",
    PHASE_HOUSING: "housing",
    PHASE_JOBS: "jobs",
    PHASE_AMENITIES: "amenities",
    PHASE_SCIENCE: "science",
    PHASE_MILITARY: "military",
    PHASE_ASCENSION: "ascension",
    PHASE_WAR: "war",
}

#: The agenda is one slot per phase, walked month by month.
AGENDA_SLOTS = len(PHASE_NAMES)

#: Planner action codes -> phase codes.  Several actions collapse onto one
#: phase because the in-game executor implements them with the same verbs.
ACTION_TO_PHASE: dict[str, int] = {
    "IMPROVE_ECONOMY": PHASE_RELIEF,
    "CONSOLIDATE": PHASE_RELIEF,
    "EXPAND": PHASE_HOUSING,
    "COLONIZE": PHASE_HOUSING,
    "BUILD_FLEET": PHASE_MILITARY,
    "DEFEND": PHASE_MILITARY,
    "BUILD_STARBASE": PHASE_MILITARY,
    "FOCUS_TECH": PHASE_SCIENCE,
    "DIPLOMACY": PHASE_AMENITIES,
    "ESPIONAGE": PHASE_AMENITIES,
    "ASCEND": PHASE_ASCENSION,
    "TAKE_TRADITION": PHASE_ASCENSION,
    "TAKE_ASCENSION_PERK": PHASE_ASCENSION,
    "WAR": PHASE_WAR,
    "PREPARE_WAR": PHASE_MILITARY,
    "BUILD_ARMY": PHASE_WAR,
}

THREAT_CODES: dict[str, int] = {
    "low": 1,
    "moderate": 2,
    "high": 3,
    "critical": 4,
}

BOTTLENECK_CODES: dict[str, int] = {
    "energy": 1,
    "minerals": 2,
    "food": 3,
}

STANCE_DEFENSIVE = 1
STANCE_BALANCED = 2
STANCE_AGGRESSIVE = 3

STANCE_NAMES: dict[int, str] = {
    STANCE_DEFENSIVE: "defensive",
    STANCE_BALANCED: "balanced",
    STANCE_AGGRESSIVE: "aggressive",
}

#: Focus keyword -> zone code.  Matched case-insensitively against the
#: planner's ``recommended_focus`` / ``arc_summary`` free text.
FOCUS_ZONE_KEYWORDS: list[tuple[tuple[str, ...], int]] = [
    (("foundry", "alloy", "megafactory", "militar", "war"), 2),
    (("tech", "research", "science", "repeatable"), 3),
    (("trade", "commerce", "federation", "diploma", "energy credit"), 4),
    (("unity", "tradition", "spiritual", "culture"), 5),
    (("industrial", "industry", "mineral", "economy", "expansion"), 1),
]

#: Tradition-tree preference -> the generated adoption chain of that code.
TRADITION_MODE_CODES: dict[str, int] = {
    "tech": 1,
    "wide": 2,
    "war": 3,
    "diplo": 4,
}

TRADITION_MODE_NAMES: dict[int, str] = {v: k for k, v in TRADITION_MODE_CODES.items()}

#: Ascension-perk preference -> the generated perk chain of that code.
PERK_MODE_CODES: dict[str, int] = {
    "tech": 1,
    "wide": 2,
    "war": 3,
    "trade": 4,
}

PERK_MODE_NAMES: dict[int, str] = {v: k for k, v in PERK_MODE_CODES.items()}

#: Orbital-bombardment posture.  1 is the restrained default; the harsher
#: stances are only ever selected when the law explicitly asks for them.
BOMBARDMENT_CODES: dict[str, int] = {
    "selective": 1,
    "indiscriminate": 2,
    "raiding": 3,
}

BOMBARDMENT_NAMES: dict[int, str] = {v: k for k, v in BOMBARDMENT_CODES.items()}

ZONE_NAMES: dict[int, str] = {
    1: "zone_industrial",
    2: "zone_foundry",
    3: "zone_research",
    4: "zone_trade",
    5: "zone_unity",
}

DEFAULT_AGENDA: list[int] = [
    PHASE_RELIEF,
    PHASE_HOUSING,
    PHASE_JOBS,
    PHASE_AMENITIES,
    PHASE_SCIENCE,
    PHASE_MILITARY,
    PHASE_ASCENSION,
    PHASE_WAR,
]

# ---------------------------------------------------------------------------
# S-18 批次④b：默认决策规则集（档 B，docs/默认决策规则集_规格.md §3）。
# 顺序即优先级；op：0/1=AND（cond2=0 即单条件退化）、2=OR、3=NOT（仅作用于 cond2）。
# 条件码表见 overmind_cond_1..20/99（triggers）；码 10 = 恒假占位严禁使用。
# 兜底 cond=99 有且仅有一条且必须收尾（第二条会被静默丢弃，规格 §4.3）。
# ---------------------------------------------------------------------------
DEFAULT_RULES: list[dict] = [
    {"cond": 5, "cond2": 0, "op": 0, "act": PHASE_WAR},
    {"cond": 6, "cond2": 5, "op": 2, "act": PHASE_MILITARY},
    {"cond": 1, "cond2": 2, "op": 2, "act": PHASE_RELIEF},
    {"cond": 3, "cond2": 4, "op": 2, "act": PHASE_RELIEF},
    {"cond": 7, "cond2": 0, "op": 0, "act": PHASE_RELIEF},
    {"cond": 8, "cond2": 0, "op": 0, "act": PHASE_HOUSING},
    {"cond": 9, "cond2": 0, "op": 0, "act": PHASE_AMENITIES},
    {"cond": 11, "cond2": 0, "op": 0, "act": PHASE_HOUSING},
    {"cond": 11, "cond2": 12, "op": 2, "act": PHASE_JOBS},
    {"cond": 12, "cond2": 13, "op": 2, "act": PHASE_SCIENCE},
    {"cond": 13, "cond2": 14, "op": 2, "act": PHASE_ASCENSION},
    {"cond": 99, "cond2": 0, "op": 0, "act": PHASE_SCIENCE},
]

LEX_VARS_RELPATH = Path("common/scripted_variables/overmind_lex_vars.txt")
LEX_EFFECTS_RELPATH = Path("common/scripted_effects/overmind_lex.txt")

#: Files this compiler owns.  The auditor/normaliser uses it to tell
#: generated content apart from hand-written mod script.
GENERATED_RELPATHS = (LEX_VARS_RELPATH, LEX_EFFECTS_RELPATH)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
@dataclass
class LexPayload:
    """A compiled law.  Serialisable so it can be audited after the fact."""

    rev: int
    slots: list[int] = field(default_factory=lambda: list(DEFAULT_AGENDA))
    threat: int = 2
    bottleneck: int = 1
    stance: int = STANCE_BALANCED
    focus_zone: int = 1
    tradition_mode: int = 1
    ap_mode: int = 1
    bombardment: int = 1
    source: str = "code"
    focus: str = ""
    year: int = 0
    rationale: str = ""
    rules: list[dict] = field(default_factory=lambda: [dict(r) for r in DEFAULT_RULES])
    parallel_max: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def agenda_names(self) -> list[str]:
        return [PHASE_NAMES.get(s, "?") for s in self.slots]


# ---------------------------------------------------------------------------
# Mapping: strategic context -> lex payload
# ---------------------------------------------------------------------------
def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _get(ctx: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dataclass or a plain mapping."""
    if isinstance(ctx, Mapping):
        return ctx.get(key, default)
    return getattr(ctx, key, default)


def agenda_from_priorities(priorities: Iterable[str]) -> list[int]:
    """Turn an ordered priority list into a six-slot monthly agenda.

    Weighting rule (deliberately simple and deterministic):

    * each listed priority gets ``3 - index`` weight, floored at 1
    * RELIEF always gets +1, because an untreated deficit compounds
    * every phase starts at weight 1 so no phase is ever fully starved
    * the slots are then filled greedily, highest weight first
    """
    weights: dict[int, int] = {code: 1 for code in PHASE_NAMES}
    for index, action in enumerate(priorities):
        phase = ACTION_TO_PHASE.get(str(action).upper())
        if phase is None:
            continue
        weights[phase] += max(1, 3 - index)
    weights[PHASE_RELIEF] += 1

    slots: list[int] = []
    while len(slots) < AGENDA_SLOTS:
        # Highest weight first; ties break on the canonical phase order so the
        # output is stable (important: tests compare generated text).
        phase = max(
            (p for p in PHASE_NAMES if p not in slots),
            key=lambda p: (weights[p], -p),
            default=None,
        )
        if phase is None:  # every phase already used once -> allow repeats
            phase = max(PHASE_NAMES, key=lambda p: (weights[p], -p))
        slots.append(phase)
    return slots


def bottleneck_from_health(health: str, bottleneck: str) -> int:
    """Which resource RELIEF should fix first."""
    code = BOTTLENECK_CODES.get(str(bottleneck or "").strip().lower())
    if code:
        return code
    if str(health or "").strip().lower() in ("deficit", "fragile"):
        return 1
    return 1


def stance_from_context(threat: str, defensive_priority: float, focus: str) -> int:
    """Fleet posture / war philosophy the law prescribes."""
    low_focus = (focus or "").lower()
    if "war preparation" in low_focus or "aggress" in low_focus:
        return STANCE_AGGRESSIVE
    if threat in ("critical", "high") or defensive_priority >= 0.6:
        return STANCE_DEFENSIVE
    if threat == "low" and defensive_priority <= 0.3:
        return STANCE_AGGRESSIVE
    return STANCE_BALANCED


def focus_zone_from_text(*texts: str) -> int:
    """Pick a zone specialisation from the planner's free-text focus."""
    blob = " ".join(t for t in texts if t).lower()
    for keywords, code in FOCUS_ZONE_KEYWORDS:
        if any(k in blob for k in keywords):
            return code
    return 1


def tradition_mode_from_focus(*texts: str) -> int:
    """Which tradition chain the law prefers, from free-text focus."""
    blob = " ".join(t for t in texts if t).lower()
    if any(k in blob for k in ("militar", "war", "fleet", "supremac", "conquer")):
        return TRADITION_MODE_CODES["war"]
    if any(k in blob for k in ("expand", "colon", "wide", "territor", "settle")):
        return TRADITION_MODE_CODES["wide"]
    if any(k in blob for k in ("diploma", "federation", "trade", "commerce", "relation")):
        return TRADITION_MODE_CODES["diplo"]
    return TRADITION_MODE_CODES["tech"]


def perk_mode_from_focus(*texts: str) -> int:
    """Which ascension-perk chain the law prefers, from free-text focus."""
    blob = " ".join(t for t in texts if t).lower()
    if any(k in blob for k in ("militar", "war", "fleet", "colossus", "conquer")):
        return PERK_MODE_CODES["war"]
    if any(k in blob for k in ("expand", "colon", "wide", "habit", "terraform")):
        return PERK_MODE_CODES["wide"]
    if any(k in blob for k in ("trade", "commerce", "energy credit", "mercantile")):
        return PERK_MODE_CODES["trade"]
    return PERK_MODE_CODES["tech"]


def bombardment_from_stance(stance: int) -> int:
    """Harsher bombardment only when the law is already aggressive.

    Deliberately conservative: an aggressive posture earns ``indiscriminate``,
    and nothing at all selects ``raiding`` on its own — that one has to come
    from an explicit planner priority.
    """
    if stance == STANCE_AGGRESSIVE:
        return BOMBARDMENT_CODES["indiscriminate"]
    return BOMBARDMENT_CODES["selective"]


def lex_from_context(ctx: Any, rev: int, rationale: str = "") -> LexPayload:
    """Compile a planner context (code or LLM sourced) into a Lex."""
    priorities = _get(ctx, "priorities", []) or []
    threat = str(_get(ctx, "threat_level", "low") or "low").strip().lower()
    health = str(_get(ctx, "economy_health", "stable") or "stable").strip().lower()
    bottleneck = str(_get(ctx, "economy_bottleneck", "") or "")
    defensive = float(_get(ctx, "defensive_priority", 0.3) or 0.0)
    focus = str(_get(ctx, "recommended_focus", "") or "")
    arc = str(_get(ctx, "arc_summary", "") or "")
    source = str(_get(ctx, "source", "code") or "code")
    year = int(_get(ctx, "year_generated", 0) or 0)

    stance = stance_from_context(threat, defensive, focus)
    return LexPayload(
        rev=rev,
        slots=agenda_from_priorities(priorities),
        threat=THREAT_CODES.get(threat, 2),
        bottleneck=bottleneck_from_health(health, bottleneck),
        stance=stance,
        focus_zone=focus_zone_from_text(focus, arc),
        tradition_mode=tradition_mode_from_focus(focus, arc),
        ap_mode=perk_mode_from_focus(focus, arc),
        bombardment=bombardment_from_stance(stance),
        source=source,
        focus=_sanitize("\n".join([focus, arc]).strip()),
        year=year,
        rationale=_sanitize(rationale),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_SAFE = re.compile(r"[^A-Za-z0-9 ,._:-]")


def _sanitize(text: str, limit: int = 200) -> str:
    """Make free text safe to embed in a Clausewitz log string.

    Brackets are stripped because ``[`` opens a data-function and produced a
    real "Invalid macro entry" error in this project before; quotes and
    newlines likewise.  ``$`` would start a script parameter.
    """
    text = _SAFE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


HEADER_VARS = """# ============================================================
# Stellaris Overmind — Lex（帝国法案）运行期变量
#
# 本文件由 engine/strategy_lex.py 自动生成，请勿手改。
# rev = {rev}
# source = {source}
# year = {year}
# focus = {focus}
# ============================================================

@om_lex_rev = {rev}
"""

HEADER_EFFECTS = """# ============================================================
# Stellaris Overmind — Lex Application（法案装载器）
#
# 本文件由 engine/strategy_lex.py 自动生成，请勿手改。
#
# rev = {rev}    source = {source}    year = {year}
# focus = {focus}
# agenda = {agenda}
#
# 相位编码 1=救济 2=住房 3=就业 4=民生 5=科研 6=军备 7=传统飞升 8=战争
# 生成器的取值域是封闭的（只有整数编码），因此这个文件必然是
# 合法的 Clausewitz 脚本——不存在"大模型写出语法错误脚本"的风险。
# ============================================================

overmind_lex_apply = {{
"""


def render_lex_files(payload: LexPayload) -> tuple[str, str]:
    """Render ``(vars_text, effects_text)`` for a payload."""
    focus = _sanitize(payload.focus, 120)
    vars_text = HEADER_VARS.format(
        rev=payload.rev,
        source=_sanitize(payload.source, 32),
        year=payload.year,
        focus=focus,
    )

    lines: list[str] = [
        HEADER_EFFECTS.format(
            rev=payload.rev,
            source=_sanitize(payload.source, 32),
            year=payload.year,
            focus=focus,
            agenda=" ".join(str(s) for s in payload.slots),
        )
    ]
    lines.append("\t# --- 版本号（必须与 overmind_lex_vars.txt 一致） ---")
    lines.append(f"\tset_variable = {{ which = om_lex_rev value = {payload.rev} }}")
    lines.append("")
    lines.append("\t# --- 月度日程表 ---")
    for index, phase in enumerate(payload.slots, start=1):
        lines.append(
            f"\tset_variable = {{ which = om_lex_slot{index} value = {phase} }}"
        )
    lines.append("")
    lines.append("\t# --- 战略旋钮 ---")
    lines.append(f"\tset_variable = {{ which = om_lex_threat value = {payload.threat} }}")
    lines.append(
        f"\tset_variable = {{ which = om_lex_bottleneck value = {payload.bottleneck} }}"
    )
    lines.append(f"\tset_variable = {{ which = om_lex_stance value = {payload.stance} }}")
    lines.append(
        f"\tset_variable = {{ which = om_lex_focus_zone value = {payload.focus_zone} }}"
    )
    lines.append(
        f"\tset_variable = {{ which = om_lex_tradition_mode value = {payload.tradition_mode} }}"
    )
    lines.append(f"\tset_variable = {{ which = om_lex_ap_mode value = {payload.ap_mode} }}")
    lines.append(
        f"\tset_variable = {{ which = om_lex_bombardment value = {payload.bombardment} }}"
    )
    lines.append("")
    # S-18 批次④b：决策规则集渲染。顺序重要：先 rules_apply（全零重置），
    # 再写入本版规则集 —— 换版即整组替换，无残留。
    lines.append("\tovermind_lex_rules_apply = yes")
    lines.append(f"\tset_variable = {{ which = om_lex_rule_count value = {len(payload.rules)} }}")
    for _n in range(1, 9):
        lines.append(f"\tset_variable = {{ which = om_lex_gain_p{_n} value = 0 }}")
        lines.append(f"\tset_variable = {{ which = om_lex_gain_p{_n}_streak value = 0 }}")
    lines.append(f"\tset_variable = {{ which = om_parallel_max value = {payload.parallel_max} }}")
    for _i, _r in enumerate(payload.rules, start=1):
        for _f in ("cond", "cond2", "op", "act"):
            lines.append(
                f"\tset_variable = {{ which = om_rule_{_i}_{_f} value = {_r[_f]} }}"
            )
    lines.append("")
    lines.append(
        "\tlog = \"OVERMIND: lex applied rev {rev} agenda {agenda} stance {stance} zone {zone} tr {tr} ap {ap} bomb {bomb}\"".format(
            rev=payload.rev,
            agenda="".join(str(s) for s in payload.slots),
            stance=STANCE_NAMES.get(payload.stance, "balanced"),
            zone=ZONE_NAMES.get(payload.focus_zone, "zone_industrial"),
            tr=TRADITION_MODE_NAMES.get(payload.tradition_mode, "tech"),
            ap=PERK_MODE_NAMES.get(payload.ap_mode, "tech"),
            bomb=BOMBARDMENT_NAMES.get(payload.bombardment, "selective"),
        )
    )
    lines.append("}")
    lines.append("")
    return vars_text, "\n".join(lines)


def write_lex(payload: LexPayload, mod_dir: Path) -> tuple[Path, Path]:
    """Write both generated files into ``mod_dir``.  Returns their paths."""
    # D1 自检兜底：compile_lex 会先过 validate_payload，但 write_lex 也可能被
    # 直接调用（工具/测试）——这里再拦一道，空规则集一律拒绝落盘。
    if not payload.rules:
        raise ValueError(
            "refusing to write lex files: empty ruleset (rule_count=0 silently no-ops, D1)"
        )
    vars_text, effects_text = render_lex_files(payload)
    vars_path = mod_dir / LEX_VARS_RELPATH
    effects_path = mod_dir / LEX_EFFECTS_RELPATH
    vars_path.parent.mkdir(parents=True, exist_ok=True)
    effects_path.parent.mkdir(parents=True, exist_ok=True)
    vars_path.write_text(vars_text, encoding="utf-8", newline="\n")
    effects_path.write_text(effects_text, encoding="utf-8", newline="\n")
    return vars_path, effects_path


# ---------------------------------------------------------------------------
# Sidecar record (audit trail)
# ---------------------------------------------------------------------------
def lex_record_path(mod_dir: Path) -> Path:
    return mod_dir / "overmind_lex_record.json"


def save_record(payload: LexPayload, mod_dir: Path, extra: Mapping[str, Any] | None = None) -> Path:
    """Persist what was compiled, so the next run can diff against it."""
    path = lex_record_path(mod_dir)
    data = payload.to_dict()
    if extra:
        data.update(dict(extra))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_record(mod_dir: Path) -> dict[str, Any] | None:
    path = lex_record_path(mod_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_lex_vars(text: str) -> int | None:
    """Read the revision out of a rendered vars file."""
    m = re.search(r"^@om_lex_rev\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def parse_lex_effects(text: str) -> dict[str, int]:
    """Read every ``set_variable`` out of a rendered effects file."""
    out: dict[str, int] = {}
    for m in re.finditer(
        r"set_variable\s*=\s*\{\s*which\s*=\s*(om_lex_\w+)\s*value\s*=\s*(-?\d+)\s*\}",
        text,
    ):
        out[m.group(1)] = int(m.group(2))
    return out


def validate_payload(payload: LexPayload) -> list[str]:
    """Return a list of problems; empty means the payload is sound."""
    problems: list[str] = []
    if len(payload.slots) != AGENDA_SLOTS:
        problems.append(
            f"agenda must have {AGENDA_SLOTS} slots, got {len(payload.slots)}"
        )
    for i, phase in enumerate(payload.slots, start=1):
        if phase not in PHASE_NAMES:
            problems.append(f"slot{i} has illegal phase code {phase}")
    if payload.threat not in (1, 2, 3, 4):
        problems.append(f"illegal threat code {payload.threat}")
    if payload.bottleneck not in (1, 2, 3):
        problems.append(f"illegal bottleneck code {payload.bottleneck}")
    if payload.stance not in STANCE_NAMES:
        problems.append(f"illegal stance code {payload.stance}")
    if payload.focus_zone not in ZONE_NAMES:
        problems.append(f"illegal focus zone code {payload.focus_zone}")
    if payload.tradition_mode not in TRADITION_MODE_NAMES:
        problems.append(f"illegal tradition mode code {payload.tradition_mode}")
    if payload.ap_mode not in PERK_MODE_NAMES:
        problems.append(f"illegal ascension perk mode code {payload.ap_mode}")
    if payload.bombardment not in BOMBARDMENT_NAMES:
        problems.append(f"illegal bombardment code {payload.bombardment}")
    if payload.rev <= 0:
        problems.append(f"revision must be positive, got {payload.rev}")
    # S-18 批次④b/A1 产出合规：规则集必须是规则形态（码/算子/相位全部受限），
    # 杜绝 LLM 输出时序计划或未实现码（D-26/D-31 型静默失败的编译期拦截）。
    implemented = set(range(1, 10)) | set(range(11, 21)) | {99}
    # D1 自检（2026-09-18，WDMSG-042/044）：C2 过渡期已随 D-31 闭环结束，
    # 空规则集不再"无意有用"——求值器 12 处守卫恒假，一条不执行也无日志，
    # 与 D-31 同族的静默失效。宁可拒绝写盘，也不要静默降级。
    if not payload.rules:
        problems.append(
            "ruleset is EMPTY (rule_count=0 will silently no-op) — 拒绝写出（D1 自检）"
        )
    if len(payload.rules) > 12:
        problems.append(f"rules count {len(payload.rules)} > 12")
    fallbacks = [i for i, r in enumerate(payload.rules) if r.get("cond") == 99]
    if fallbacks != [len(payload.rules) - 1]:
        problems.append(
            f"cond=99 fallback must be exactly one rule at the end, got indices {fallbacks}"
        )
    for i, r in enumerate(payload.rules, start=1):
        for fld in ("cond", "cond2"):
            code = r.get(fld, 0)
            if code != 0 and code not in implemented:
                problems.append(f"rule {i} {fld} uses unimplemented code {code}")
        if r.get("op", 0) not in (0, 1, 2, 3):
            problems.append(f"rule {i} has illegal op {r.get('op')}")
        act = r.get("act", 0)
        if act not in range(1, 9):
            problems.append(f"rule {i} act {act} out of 1..8")
    return problems
