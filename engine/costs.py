"""Cost & progression tables — generate the mod's price tags from game data.

Two jobs, both of them "read the game, write the mod":

**1. Prices.**  ``add_district`` / ``add_zone`` / ``add_building`` /
``create_army`` are instant effects: they bypass the build queue, so the game
never charges for them.  The autonomy executor debits the treasury itself, and
this module makes sure the debit is *the game's own price* — see
``engine/cost_table.py`` for how that price is resolved.  The prices are baked
in as **literals**, not as ``@`` scripted variables, because a pre-processed
macro inside a ``$parameter$`` is not something we can verify statically, and a
mistake there would be charged silently at runtime.

**2. Progression.**  Traditions and ascension perks are granted with
``add_tradition`` / ``add_ascension_perk``.  Which ones to take is the LLM's
call, but it may only speak in *closed codes* (FR-03), so each code selects one
of a handful of pre-generated chains.  The chains themselves are read out of
``common/traditions`` and ``common/ascension_perks`` at generation time, so a
patch that adds or renames a node is picked up automatically instead of
silently doing nothing.

Everything written here is *generated*: hand edits are lost on the next run.
The hand-written executor (``overmind_autonomy.txt``) calls into these effects
by name, and ``tests/test_costs.py`` pins the contract between the two.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from engine.cost_table import (
    collect_costs,
    extract_block,
    iter_txt,
    strip_comments,
)

# ---------------------------------------------------------------------------
# Generated artefacts
# ---------------------------------------------------------------------------
# ⚠️ Triggers and effects live in **separate directories** and Clausewitz keeps
# two separate registries: ``common/scripted_triggers/`` is the only place a
# top-level ``name = { <trigger> }`` block is registered as a scripted trigger;
# ``common/scripted_effects/`` is the only place a ``name = { <effect> }``
# block is registered as a scripted effect.
#
# An earlier revision put both into one file under ``scripted_effects/``.  The
# charge *effects* registered fine (which is why they never showed up in
# ``error.log``), but every ``overmind_afford_*`` trigger silently failed to
# register, and the runtime then reported:
#
#     Error in scripted trigger, cannot find: overmind_afford_district
#
# for all 15 of them.  Real hardware, real log, 2026-09-15.  Do not merge these
# two files back together — the split is load-bearing.
CHARGES_RELPATH = Path("common/scripted_effects/overmind_charges.txt")
AFFORD_RELPATH = Path("common/scripted_triggers/overmind_afford.txt")
PROGRESSION_RELPATH = Path("common/scripted_effects/overmind_progression.txt")
COST_VARS_RELPATH = Path("common/scripted_variables/overmind_costs.txt")
TABLE_RELPATH = Path("overmind_cost_table.json")

GENERATED_RELPATHS = (
    CHARGES_RELPATH,
    AFFORD_RELPATH,
    PROGRESSION_RELPATH,
    COST_VARS_RELPATH,
    TABLE_RELPATH,
)

# ---------------------------------------------------------------------------
# Which items the executor is allowed to build
# ---------------------------------------------------------------------------
#: ``kind -> [game id]``.  Every id here is checked against the game's own cost
#: table at generation time; a typo raises instead of shipping.
CHARGED_ITEMS: dict[str, tuple[str, ...]] = {
    "district": (
        "district_generator",
        "district_mining",
        "district_farming",
        "district_city",
    ),
    "zone": (
        "zone_industrial",
        "zone_foundry",
        # 2026-09-16 用户要求「所有资源产能都要有 + 稀有资源也要列」：
        # 消费品（zone_factory）、基础三资源（energy/minerals/food）、
        # 稀有资源（rare_crystals/volatile_motes/exotic_gases）补齐。
        # 缺消费品产能曾导致研究员（消费品大户）越建越多却无产出 → 长期赤字。
        "zone_factory",
        "zone_research",
        "zone_trade",
        "zone_unity",
        "zone_energy",
        "zone_minerals",
        "zone_food",
        "zone_rare_crystals",
        "zone_volatile_motes",
        "zone_exotic_gases",
    ),
    "building": (
        "building_holo_theatres",
        "building_luxury_residence",
        "building_research_lab_1",
        # S-19（裁定 §5）：产能建筑双将 —— 消费品（factory）与合金（foundry）。
        # 专业区前置门（has_any_*_zone）写在执行分支，不进生成 afford（见
        # EXTRA_AFFORD_LINES 与 invest 通道）。
        "building_factory_1",
        "building_foundry_1",
    ),
    "army": ("assault_army",),
    "starbase": ("starbase_outpost",),
}

#: Charge effects are only ever debited in one resource per item, because an
#: instant ``add_resource`` cannot split its payment sensibly.  The resource is
#: whatever the game asks for first; alloys-only items are converted to their
#: mineral equivalent is *not* done — the real resource is used.
DEFAULT_CHARGE_RESOURCE = "minerals"

#: 除「库存 ≥ 造价」之外**还要满足**的额外条件，按 game_id 挂。
#:
#: 为什么需要它：生成的 afford 只有一条库存门，而某些动作还要过**收入门**。
#: 哨站重建就是典型 —— 总裁席整改（2026-09-15）指出：只查合金 ≥100 时，
#: 能源已经赤字还会继续建站放血（每站一份永久维护）。
#: 原先这行是手改在生成物里的，结果一次 regenerate 就被抹掉（2026-09-16 实测），
#: 所以改为写在生成器里 —— **生成物永远不该手改**。
EXTRA_AFFORD_LINES: dict[str, tuple[str, ...]] = {
    "starbase_outpost": (
        "\t# 总裁席整改（2026-09-15）：必须同时过统一花销门（P6：能源门不豁免）",
        "\tovermind_can_afford = { RESOURCE = alloys PAD = 100 }",
    ),
}

# ---------------------------------------------------------------------------
# Progression chains
# ---------------------------------------------------------------------------
#: ``mode code -> (name, [tradition tree, ...])``.  The tree that is adopted
#: first is the first one in the list that the empire does not already have.
TRADITION_MODES: dict[int, tuple[str, tuple[str, ...]]] = {
    1: ("tech", ("discovery", "prosperity", "supremacy", "expansion", "mercantile", "harmony", "adaptability", "diplomacy")),
    2: ("wide", ("expansion", "prosperity", "adaptability", "discovery", "mercantile", "supremacy", "harmony", "diplomacy")),
    3: ("war", ("supremacy", "prosperity", "domination", "discovery", "mercantile", "harmony", "adaptability", "diplomacy")),
    4: ("diplo", ("diplomacy", "mercantile", "prosperity", "discovery", "harmony", "adaptability", "expansion", "supremacy")),
}

#: ``mode code -> (name, [ascension perk])``, taken in order.
PERK_MODES: dict[int, tuple[str, tuple[str, ...]]] = {
    1: ("tech", ("ap_technological_ascendancy", "ap_transcendent_learning", "ap_one_vision", "ap_galactic_wonders")),
    2: ("wide", ("ap_mastery_of_nature", "ap_voidborn", "ap_world_shaper", "ap_arcology_project")),
    3: ("war", ("ap_galactic_force_projection", "ap_lord_of_war", "ap_colossus", "ap_master_builders")),
    4: ("trade", ("ap_universal_transactions", "ap_executive_vigor", "ap_imperial_prerogative", "ap_galactic_contender")),
}

#: Trees that are always present in the base game and safe to name.  Used as a
#: sanity check on the chains above.
REQUIRED_TREES: tuple[str, ...] = (
    "discovery",
    "prosperity",
    "supremacy",
    "expansion",
    "mercantile",
    "harmony",
    "adaptability",
    "diplomacy",
    "domination",
)


# ---------------------------------------------------------------------------
# Game-data reading
# ---------------------------------------------------------------------------
def collect_tradition_trees(game_dir: Path) -> dict[str, dict[str, list[str]]]:
    """``{tree: {"adopt": id, "nodes": [id, ...], "finish": id}}``.

    Nodes come out in the order the data file lists them, which is the order
    the game's own tree UI walks — so the executor can advance a tree by
    picking the first node it does not have yet.
    """
    trees: dict[str, dict[str, list[str]]] = {}
    root = game_dir / "common/traditions"
    for path in iter_txt(root):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in re.finditer(r"^(tr_[a-z_0-9]+)\s*=\s*\{", text, re.M):
            key = m.group(1)
            body = key[len("tr_") :]
            if body.endswith("_adopt"):
                tree = body[: -len("_adopt")]
                trees.setdefault(tree, {"adopt": [], "nodes": [], "finish": []})["adopt"].append(key)
            elif body.endswith("_finish"):
                tree = body[: -len("_finish")]
                trees.setdefault(tree, {"adopt": [], "nodes": [], "finish": []})["finish"].append(key)
            else:
                # ``tr_<tree>_<node>``; split on the first underscore.
                tree, _, _node = body.partition("_")
                if tree:
                    trees.setdefault(tree, {"adopt": [], "nodes": [], "finish": []})["nodes"].append(key)
            _ = extract_block(text, m.end() - 1)
    return trees


def collect_ascension_perks(game_dir: Path) -> set[str]:
    perks: set[str] = set()
    for path in iter_txt(game_dir / "common/ascension_perks"):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        perks.update(m.group(1) for m in re.finditer(r"^(ap_[a-z_0-9]+)\s*=\s*\{", text, re.M))
    return perks


#: Defines the tradition price is derived from.  Reading them means a rebalance
#: flows through automatically instead of needing an edit here.
TRADITION_DEFINES = {
    "base": ("TRADITION_COST_AMOUNTS", 300),
    "linear": ("TRADITION_COST_TRADITION", 8),
    "exponent": ("TRADITION_COST_TRADITION_EXP", 1.8),
    "categories_max": ("TRADITION_CATEGORIES_MAX", 7),
}

_DEFINE_LINE = re.compile(r"^\s*([A-Z_0-9]{2,})\s*=\s*([^\n#]*)", re.M)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def read_defines(game_dir: Path) -> dict[str, float]:
    """Pull the handful of numeric defines the cost model needs.

    Values come in three shapes and all of them must work, or the model silently
    falls back to a hardcoded default and stops tracking the game::

        TRADITION_COST_TRADITION       = 8
        TRADITION_COST_TRADITION_EXP   = 1.800
        TRADITION_COST_AMOUNTS         = { 300 }
    """
    out: dict[str, float] = {}
    defines = game_dir / "common/defines/00_defines.txt"
    if not defines.exists():
        return out
    text = strip_comments(defines.read_text(encoding="utf-8", errors="replace"))
    for m in _DEFINE_LINE.finditer(text):
        number = _NUMBER.search(m.group(2))
        if number:
            out[m.group(1)] = float(number.group())
    return out


@dataclass
class TraditionCostModel:
    """The game's own tradition price curve.

    ``cost(n) = base + (linear * n) ** exponent`` — the formula the game's own
    defines describe, where ``n`` is how many traditions the empire has taken.

    The catch: **there is no ``num_traditions`` trigger.**  The first draft of
    this module used one, and the auditor caught it before it shipped (a
    non-existent trigger fails silently in Clausewitz).  The only tradition
    counter the game actually exposes to script is
    ``num_tradition_categories`` — the number of *trees* adopted.  So the price
    is bracketed by tree count, with each tree assumed to hold
    ``traditions_per_tree`` traditions.  That is an approximation, and it is
    deliberately a *conservative* one: the charge can be a bracket low, never
    free.
    """

    base: int = 300
    linear: float = 8.0
    exponent: float = 1.8
    categories_max: int = 7
    traditions_per_tree: int = 7

    def cost(self, n: int) -> int:
        """Price when the empire has taken ``n`` traditions."""
        return int(round(self.base + (self.linear * n) ** self.exponent))

    def cost_for_categories(self, categories: int) -> int:
        return self.cost(categories * self.traditions_per_tree)

    def brackets(self) -> list[tuple[int, int]]:
        """``[(upper_exclusive_categories, price), ...]``, last one open-ended."""
        out: list[tuple[int, int]] = []
        for categories in range(1, self.categories_max):
            out.append((categories, self.cost_for_categories(categories - 1)))
        out.append((self.categories_max, self.cost_for_categories(self.categories_max - 1)))
        return out


def build_tradition_cost_model(game_dir: Path) -> TraditionCostModel:
    defines = read_defines(game_dir)
    model = TraditionCostModel()
    for attr, (name, fallback) in TRADITION_DEFINES.items():
        value = defines.get(name, fallback)
        setattr(model, attr, float(value) if attr in ("linear", "exponent") else int(value))
    return model


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def charge_for(entry_costs: dict[str, int]) -> tuple[str, int] | None:
    """The resource and amount a build should be debited in."""
    if not entry_costs:
        return None
    if DEFAULT_CHARGE_RESOURCE in entry_costs:
        return DEFAULT_CHARGE_RESOURCE, entry_costs[DEFAULT_CHARGE_RESOURCE]
    resource, amount = sorted(entry_costs.items())[0]
    return resource, amount


def charge_effect_name(kind: str, game_id: str) -> str:
    """Naming convention for the generated spend effect."""
    return f"overmind_charge_{kind}_{game_id}"


def afford_trigger_name(kind: str, game_id: str) -> str:
    """Naming convention for the generated "can I pay for this" trigger."""
    return f"overmind_afford_{kind}_{game_id}"


def build_charge_effects(
    game_dir: Path,
) -> tuple[str, str, dict[str, dict[str, int]]]:
    """Render the charge effects and their matching affordability triggers.

    Returns ``(afford_text, charge_text, table)`` — the two blocks go to
    different directories (see ``AFFORD_RELPATH``), because a trigger defined
    inside ``scripted_effects/`` is never registered.

    The pair matters.  Every build is gated on ``overmind_afford_*`` *before*
    the effect runs, and only then is ``overmind_charge_*`` debited alongside
    the build.  Without the gate a script could conjure a district out of an
    empty treasury, because ``add_resource`` clamps at zero rather than
    refusing.
    """
    costs = collect_costs(game_dir)
    header = [
        "# ============================================================",
        "# Stellaris Overmind — Charge & Affordability（生成文件）",
        "#",
        "# 本文件由 engine/costs.py 从游戏自带数据生成，请勿手改。",
        "#",
        "# 每个物件配一对：",
        "#   overmind_afford_<kind>_<id>  触发器：国库够不够付",
        "#   overmind_charge_<kind>_<id>  效果：按游戏造价扣一次钱",
        "# 执行层先问 afford、再调 charge，然后才建造，",
        "# 这样帝国永远不可能在建不起的时候凭空长出东西。",
        "#",
        "# ⚠️ 这一对**分居两个目录**，不要把它们合并回去：",
        "#   trigger 只有放在 common/scripted_triggers/ 才会被注册；",
        "#   放进 scripted_effects/ 的顶层 trigger 会被静默丢弃，",
        "#   运行时表现为 `Error in scripted trigger, cannot find: ...`。",
        "#",
        "# 写成字面数字而不是 @ 脚本变量：@ 在 $参数$ 展开之后的解析",
        "# 顺序无法静态验证，而扣费出错是静默的。",
        "# ============================================================",
        "",
    ]
    afford_lines: list[str] = list(header)
    charge_lines: list[str] = list(header)
    table: dict[str, dict[str, int]] = {}

    for kind, ids in CHARGED_ITEMS.items():
        for game_id in ids:
            entry = costs.get(game_id)
            if entry is None:
                raise KeyError(f"{game_id} 不在游戏造价表中（engine/cost_table.py）")
            charge = charge_for(entry.costs)
            if charge is None:
                # Nothing to charge (e.g. defensive armies): record it and emit
                # a deliberate no-op pair, so the call site stays valid and the
                # omission is visible in the table.
                table[game_id] = {}
                afford_lines.append(f"# {game_id}: 游戏数据未定义造价，扣费为空操作")
                afford_lines.append(
                    f"{afford_trigger_name(kind, game_id)} = {{ always = yes }}"
                )
                afford_lines.append("")
                charge_lines.append(f"# {game_id}: 游戏数据未定义造价，扣费为空操作")
                charge_lines.append(f"{charge_effect_name(kind, game_id)} = {{")
                charge_lines.append("\t# no cost defined in game data")
                charge_lines.append("}")
                charge_lines.append("")
                continue
            resource, amount = charge
            table[game_id] = {resource: amount}
            afford_lines.append(f"# {game_id} <- {entry.source}")
            afford_lines.append(f"{afford_trigger_name(kind, game_id)} = {{")
            afford_lines.append(
                f"\tresource_stockpile_compare = {{ resource = {resource} value >= {amount} }}"
            )
            afford_lines.extend(EXTRA_AFFORD_LINES.get(game_id, ()))
            afford_lines.append("}")
            afford_lines.append("")
            charge_lines.append(f"# {game_id} <- {entry.source}")
            charge_lines.append(f"{charge_effect_name(kind, game_id)} = {{")
            charge_lines.append(f"\tadd_resource = {{ {resource} = -{amount} }}")
            charge_lines.append("}")
            charge_lines.append("")

    return "\n".join(afford_lines), "\n".join(charge_lines), table


def build_cost_vars(game_dir: Path, table: dict[str, dict[str, int]]) -> str:
    """The same numbers again, as ``@`` variables, for readability.

    ⚠️ 本文件是**纯文档**，不参与扣费。这一点必须名副其实 ——
    2026-09-15 之前它并不名副其实：这里发出的 ``@om_neg_*`` /
    ``@om_max_tradition_trees`` 是**本 mod 自己造的新名字**，而执行层用的是
    ``overmind_charges.txt`` 里的字面数字，两边没有任何引用关系。
    那些没人读的变量却会被引擎全量注册，于是撞上命名冲突：

        [20:31:26][reader.cpp:209]: Variable name max_tradition_trees is
        already taken. file: common/scripted_variables/07_scripted_variables_machine_age.txt line: 20

    这不是把名字加 ``om_`` 前缀就能了事的 bug —— 那是把 mod 的私有名字
    去和游戏上游定义抢注册表。正确做法是**根本不注册**：
    只保留 ``#`` 注释行，下面这些 ``@`` 变量一律不再发出。

    （需要机器可读的数字请用 ``overmind_cost_table.json``，那才是给程序看的。）
    """
    lines = [
        "# ============================================================",
        "# Stellaris Overmind — Cost Variables（生成文件，**纯文档**）",
        "#",
        "# 本文件由 engine/costs.py 生成，不含任何生效语句 —— 全部是注释。",
        "# 执行层扣费使用的是 common/scripted_effects/overmind_charges.txt",
        "# 里的字面数字；机器可读的造价表是 overmind_cost_table.json。",
        "#",
        "# 为什么故意不定义 @ 变量：scripted_variables 会被引擎**全量注册**，",
        "# 于是本 mod 自造的名字会和游戏及其它 mod 抢同一个注册表。",
        "# 实机曾因此报：",
        "#   Variable name max_tradition_trees is already taken.",
        "#   file: common/scripted_variables/07_scripted_variables_machine_age.txt line: 20",
        "# 文档文件不该有这种副作用，所以这里一个活跃定义都不留。",
        "#",
        "# 传统树上限的真身是游戏定义的 @max_tradition_trees（见下行说明），",
        "# 执行层直接引用它，本文件只做备忘。",
        "# ============================================================",
        "",
    ]
    model = build_tradition_cost_model(game_dir)
    lines.append(
        f"# --- 传统树上限：游戏定义 @max_tradition_trees = {model.categories_max} ---"
    )
    lines.append(
        "#     （来源 common/scripted_variables/07_scripted_variables_machine_age.txt；"
        "执行层引用该游戏变量，不引用本文件）"
    )
    lines.append("")
    for kind, ids in CHARGED_ITEMS.items():
        lines.append(f"# --- {kind} ---")
        for game_id in ids:
            charge = table.get(game_id) or {}
            if not charge:
                lines.append(f"# {game_id}: 无造价")
                continue
            resource, amount = sorted(charge.items())[0]
            lines.append(f"# {game_id}: {resource} -{amount}")
        lines.append("")
    return "\n".join(lines)


def build_progression(game_dir: Path) -> tuple[str, dict[str, object]]:
    """Render the tradition / ascension-perk chains."""
    trees = collect_tradition_trees(game_dir)
    perks = collect_ascension_perks(game_dir)

    missing = [t for t in REQUIRED_TREES if t not in trees or not trees[t]["adopt"]]
    if missing:
        raise KeyError(f"游戏数据缺少传统树: {missing}")

    for mode, (name, chain) in {**TRADITION_MODES}.items():
        bad = [t for t in chain if t not in trees]
        if bad:
            raise KeyError(f"传统树模式 {mode}({name}) 引用了不存在的树: {bad}")

    bad_perks = sorted({p for _n, chain in PERK_MODES.values() for p in chain} - perks)
    if bad_perks:
        raise KeyError(f"飞升天赋模式引用了不存在的天赋: {bad_perks}")

    lines = [
        "# ============================================================",
        "# Stellaris Overmind — Progression（传统与飞升，生成文件）",
        "#",
        "# 本文件由 engine/costs.py 从游戏自带数据生成，请勿手改。",
        "# 链上的每个 ID 都取自 common/traditions 与 common/ascension_perks，",
        "# 因此游戏改名 / 加节点会自动同步，而不是静默失效。",
        "#",
        "# 大模型只说整数编码（om_lex_tradition_mode / om_lex_ap_mode），",
        "# 具体走哪条链由本文件决定——这就是「封闭取值域」的落地方式。",
        "# ============================================================",
        "",
    ]

    # --- adopt the next tree in the mode's chain ---
    #
    # ⚠️ The per-mode bodies are **inlined** into this one effect rather than
    # kept in separate ``overmind_adopt_tree_<name>`` effects.  Reason: the
    # engine caps scripted-effect nesting at 5
    # (``CRITICAL: Max effects post init recursive depth of 5 reached``, seen
    # on real hardware 2026-09-15).  The old shape was
    #
    #     tick -> run_slot -> phase_ascension -> advance_tradition_tree
    #          -> adopt_tree_tech -> add_tradition                       = 6
    #
    # which made the engine abandon the rest of that branch's initialisation,
    # so ``add_tradition`` reported "could not find tradition with key:
    # tr_discovery_adopt" for keys that *do* exist in the game files.
    # Flattening one dispatcher layer brings it back to 5 and the errors stop.
    # Do not split it back out.
    lines.append("# 采纳下一棵传统树（按模式链，取第一棵尚未采纳的）")
    lines.append("# 注意：各模式的分支是**内联**在这里的，不是各自一个 effect。")
    lines.append("# 原因：引擎对 scripted effect 的嵌套上限是 5 层，拆开会超限。")
    lines.append("overmind_advance_tradition_tree = {")
    for index, mode in enumerate(sorted(TRADITION_MODES)):
        name, chain = TRADITION_MODES[mode]
        kw = "if" if index == 0 else "else_if"
        lines.append(f"\t{kw} = {{")
        lines.append(f"\t\tlimit = {{ check_variable = {{ which = om_lex_tradition_mode value = {mode} }} }}")
        # Inline the chain body for this mode.
        for cindex, tree in enumerate(chain):
            adopt = trees[tree]["adopt"][0]
            ckw = "if" if cindex == 0 else "else_if"
            lines.append(f"\t\t{ckw} = {{")
            lines.append(f"\t\t\tlimit = {{ NOT = {{ has_tradition = {adopt} }} }}")
            lines.append(f"\t\t\tadd_tradition = {adopt}")
            lines.append(
                f'\t\t\tlog = "OVERMIND: 采纳传统树 {tree}'
                f'（{adopt}）"'
            )
            lines.append("\t\t}")
        lines.append("\t}")
    # Default branch: fall back to the tech chain, also inlined.
    default_chain = TRADITION_MODES[1][1] if 1 in TRADITION_MODES else []
    lines.append("\telse = {")
    for cindex, tree in enumerate(default_chain):
        adopt = trees[tree]["adopt"][0]
        ckw = "if" if cindex == 0 else "else_if"
        lines.append(f"\t\t{ckw} = {{")
        lines.append(f"\t\t\tlimit = {{ NOT = {{ has_tradition = {adopt} }} }}")
        lines.append(f"\t\t\tadd_tradition = {adopt}")
        lines.append(
            f'\t\t\tlog = "OVERMIND: 采纳传统树 {tree}（{adopt}）"'
        )
        lines.append("\t\t}")
    lines.append("\t}")
    lines.append("}")
    lines.append("")

    # The standalone per-mode effects are no longer emitted.  They were only
    # ever called from ``overmind_advance_tradition_tree``; emitting them again
    # would recreate the extra nesting level the flattening removed.
    for mode, (name, chain) in TRADITION_MODES.items():
        lines.append(f"# 模式 {mode} = {name}（已内联到 overmind_advance_tradition_tree）")
        lines.append(f"# 链: {' -> '.join(chain)}")
        lines.append("")

    # --- advance a node inside an already-adopted tree ---
    #
    # Same flattening as above: ``overmind_node_<tree>`` bodies are inlined so
    # the chain stays at 5 levels (tick -> run_slot -> phase_ascension ->
    # advance_tradition_node -> add_tradition).
    lines.append("# 在已采纳的树里推进一个节点（取第一个尚未拥有的）")
    lines.append("# 同样内联各树分支，避免多出一层嵌套（见 advance_tradition_tree 的说明）。")
    lines.append("overmind_advance_tradition_node = {")
    emitted = 0
    for tree in REQUIRED_TREES:
        adopt = trees[tree]["adopt"][0]
        nodes = trees[tree]["nodes"]
        kw = "if" if emitted == 0 else "else_if"
        lines.append(f"\t{kw} = {{")
        lines.append(f"\t\tlimit = {{ has_tradition = {adopt} }}")
        for index, node in enumerate(nodes):
            nkw = "if" if index == 0 else "else_if"
            lines.append(f"\t\t{nkw} = {{")
            lines.append(f"\t\t\tlimit = {{ NOT = {{ has_tradition = {node} }} }}")
            lines.append(f"\t\t\tadd_tradition = {node}")
            lines.append(f'\t\t\tlog = "OVERMIND: 传统推进节点 {node}"')
            lines.append("\t\t}")
        lines.append("\t}")
        emitted += 1
    lines.append("}")
    lines.append("")

    for tree in REQUIRED_TREES:
        nodes = trees[tree]["nodes"]
        lines.append(f"# overmind_node_{tree}（已内联）— {len(nodes)} 个节点")
        lines.append("")

    # --- ascension perks ---
    # Flattened for the same nesting-limit reason as the tradition chains.
    lines.append("# 授予一个飞升天赋（按模式链，取第一个尚未拥有的）")
    lines.append("# 各模式分支同样内联，避免多出一层嵌套。")
    lines.append("overmind_grant_ascension_perk = {")
    for index, mode in enumerate(sorted(PERK_MODES)):
        name, chain = PERK_MODES[mode]
        kw = "if" if index == 0 else "else_if"
        lines.append(f"\t{kw} = {{")
        lines.append(f"\t\tlimit = {{ check_variable = {{ which = om_lex_ap_mode value = {mode} }} }}")
        for cindex, perk in enumerate(chain):
            ckw = "if" if cindex == 0 else "else_if"
            lines.append(f"\t\t{ckw} = {{")
            lines.append(f"\t\t\tlimit = {{ NOT = {{ has_ascension_perk = {perk} }} }}")
            lines.append(f"\t\t\tadd_ascension_perk = {perk}")
            lines.append(f'\t\t\tlog = "OVERMIND: 授予飞升天赋 {perk}"')
            lines.append("\t\t}")
        lines.append("\t}")
    # Default branch: tech perks, also inlined.
    default_perks = PERK_MODES[1][1] if 1 in PERK_MODES else []
    lines.append("\telse = {")
    for cindex, perk in enumerate(default_perks):
        ckw = "if" if cindex == 0 else "else_if"
        lines.append(f"\t\t{ckw} = {{")
        lines.append(f"\t\t\tlimit = {{ NOT = {{ has_ascension_perk = {perk} }} }}")
        lines.append(f"\t\t\tadd_ascension_perk = {perk}")
        lines.append(f'\t\t\tlog = "OVERMIND: 授予飞升天赋 {perk}"')
        lines.append("\t\t}")
    lines.append("\t}")
    lines.append("}")
    lines.append("")

    for mode, (name, chain) in PERK_MODES.items():
        lines.append(f"# 模式 {mode} = {name}（已内联）— {len(chain)} 个")
    lines.append("")

    meta: dict[str, object] = {
        "tradition_modes": {
            str(mode): {"name": name, "chain": list(chain)}
            for mode, (name, chain) in TRADITION_MODES.items()
        },
        "perk_modes": {
            str(mode): {"name": name, "chain": list(chain)}
            for mode, (name, chain) in PERK_MODES.items()
        },
        "tree_nodes": {t: trees[t]["nodes"] for t in REQUIRED_TREES},
    }
    return "\n".join(lines), meta


def build_tradition_charge(game_dir: Path) -> tuple[str, str, list[tuple[int, int]]]:
    """The bracket table used to pay for one tradition.

    Returns ``(afford_text, charge_text, brackets)`` for the same
    registry-separation reason as ``build_charge_effects``: the affordability
    side is a trigger and therefore belongs in ``scripted_triggers/``.
    """
    model = build_tradition_cost_model(game_dir)
    brackets = model.brackets()

    header = [
        "# ============================================================",
        "# 传统的真实价格（由 defines 公式推导，按已采纳传统树分档）",
        "#   cost(n) = base + (linear * n) ** exponent",
        "#   n 取 num_tradition_categories * {per_tree}（游戏未暴露传统计数）",
        "#   base={base} linear={linear} exponent={exponent}".format(
            base=model.base,
            linear=model.linear,
            exponent=model.exponent,
            per_tree=model.traditions_per_tree,
        ),
        "# ============================================================",
    ]

    afford_lines: list[str] = list(header)
    afford_lines.append("overmind_afford_tradition = {")
    for index, (limit, price) in enumerate(brackets):
        kw = "if" if index == 0 else "else_if"
        afford_lines.append(f"\t{kw} = {{")
        afford_lines.append(f"\t\tlimit = {{ num_tradition_categories < {limit} }}")
        afford_lines.append(
            f"\t\tresource_stockpile_compare = {{ resource = unity value >= {price} }}"
        )
        afford_lines.append("\t}")
    afford_lines.append("\telse = {")
    afford_lines.append(
        f"\t\tresource_stockpile_compare = {{ resource = unity value >= {brackets[-1][1]} }}"
    )
    afford_lines.append("\t}")
    afford_lines.append("}")
    afford_lines.append("")

    charge_lines: list[str] = list(header)
    charge_lines.append("overmind_charge_tradition = {")
    for index, (limit, price) in enumerate(brackets):
        kw = "if" if index == 0 else "else_if"
        charge_lines.append(f"\t{kw} = {{")
        charge_lines.append(f"\t\tlimit = {{ num_tradition_categories < {limit} }}")
        charge_lines.append(f"\t\tadd_resource = {{ unity = -{price} }}")
        charge_lines.append("\t}")
    charge_lines.append("\telse = {")
    charge_lines.append(f"\t\tadd_resource = {{ unity = -{brackets[-1][1]} }}")
    charge_lines.append("\t}")
    # ⚠️ 这行 } 曾经漏掉（2026-09-15 发现）：afford 侧收了尾，charge 侧没有，
    # 于是 overmind_charge_tradition 一直没闭合、整个文件在块中间结束。
    # 后果是静默的 —— Clausewitz 对文件末尾少一个 } 不一定报错，但
    # 它前面的内容与后文边界就错了。由 tests/test_generated_braces_balance.py 兜底。
    charge_lines.append("}")
    charge_lines.append("")

    return "\n".join(afford_lines), "\n".join(charge_lines), brackets


def generate(game_dir: Path, mod_dir: Path) -> dict[str, Path]:
    """Write every generated artefact into ``mod_dir``."""
    afford, charges, table = build_charge_effects(game_dir)
    trad_afford, trad_charge, brackets = build_tradition_charge(game_dir)
    progression, meta = build_progression(game_dir)
    cost_vars = build_cost_vars(game_dir, table)

    # Triggers and effects are concatenated *within their own kind*, then
    # written to two different directories.  Never merge across the boundary.
    afford_text = afford + "\n\n" + trad_afford
    charges_text = charges + "\n\n" + trad_charge
    # The names are exported so the hand-written executor and this generated
    # file can be checked against each other by a test, instead of drifting.
    payload = {
        "charges": table,
        "charge_effect_names": sorted(
            charge_effect_name(kind, game_id)
            for kind, ids in CHARGED_ITEMS.items()
            for game_id in ids
        )
        + ["overmind_charge_tradition"],
        "afford_trigger_names": sorted(
            afford_trigger_name(kind, game_id)
            for kind, ids in CHARGED_ITEMS.items()
            for game_id in ids
        )
        + ["overmind_afford_tradition"],
        "tradition_cost_brackets": brackets,
        "generated_from": str(game_dir),
        **meta,
    }

    written: dict[str, Path] = {}
    for rel, text in (
        (AFFORD_RELPATH, afford_text),
        (CHARGES_RELPATH, charges_text),
        (PROGRESSION_RELPATH, progression),
        (COST_VARS_RELPATH, cost_vars),
    ):
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written[rel.name] = path

    table_path = mod_dir / TABLE_RELPATH
    table_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    written[TABLE_RELPATH.name] = table_path
    return written


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse

    from engine.script_audit import game_dir_from_config, mod_dir_from_config

    ap = argparse.ArgumentParser(description="从游戏数据生成造价与进程表")
    ap.add_argument("--game", type=Path, default=None)
    ap.add_argument("--mod", type=Path, default=None)
    args = ap.parse_args(argv)

    game_dir = args.game or game_dir_from_config()
    mod_dir = args.mod or mod_dir_from_config()
    written = generate(game_dir, mod_dir)
    for name, path in written.items():
        print(f"已写入 {name}: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
