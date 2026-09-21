"""Strategy implementation guards (S-series) — zcode line.

Ownership note (COORDINATION §7.2, 2026-09-16): ``tests/test_autonomy_contract.py``
is WorkBuddy territory.  The S-5..S-4 strategy guards therefore live here so the
two agents never edit the same test file.  Helpers are duplicated on purpose
(small, stable) to keep the file self-contained.

Order of business per task: guard first (red) → implement (green) → injection
reverse verification (re-inject the defect, guard must fail and name it).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"
AUTONOMY = MOD_DIR / "common/scripted_effects/overmind_autonomy.txt"
TRIGGERS = MOD_DIR / "common/scripted_triggers/overmind_autonomy_triggers.txt"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block_at(text: str, brace: int) -> str:
    """Return the balanced ``{...}`` block that opens at ``brace``."""
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    raise ValueError("unbalanced braces")


# ==========================================================================
# S-5 —— 动态威胁评估（2026-09-16，总裁席放行；规格书 §6，顺带解决 D-16）
#
# om_lex_threat 由法案装载时的静态值改为 annual 每年重算：
# 邻国军力比 / 战争（攻防区分）/ 宿敌清洗者邻接 / 危机年代，0..5 分级不变。
# ==========================================================================


def test_threat_is_recomputed_yearly_from_components() -> None:
    """S-5：om_lex_threat 必须由 annual 每年重算，且四个分量齐全。

    D-16：om_lex_threat 五年不更新 → WAR/MILITARY 的威胁分支全是死代码。
    规格书 §6 分量表：邻国军力比（>80% +1 / >120% +2）、处于战争
    （被宣战 +2 / 主动宣战 +1）、宿敌/清洗者邻接（+1）、危机年代（+1），
    分量和上限 2+2+1+1=6，必须夹紧回 5。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_annual = {")
    assert start != -1, "找不到 overmind_autonomy_annual"
    annual = _block_at(text, text.index("{", start))
    assert "overmind_evaluate_threat" in annual, (
        "annual 未调用威胁评估 —— om_lex_threat 将冻结为法案静态值（D-16 复发）"
    )
    ev = text.find("overmind_evaluate_threat = {")
    assert ev != -1, "找不到 overmind_evaluate_threat"
    body = _block_at(text, text.index("{", ev))
    assert re.search(
        r"set_variable\s*=\s*\{\s*which\s*=\s*om_lex_threat\s+value\s*=\s*0", body
    ), "威胁评估未先归零 —— 旧值会跨年累积叠加"
    # D-24：relative_power 只接受定性 token（数值 token 实机每月刷 error.log）。
    # token 带宽（wiki+defines 交叉确证）：equivalent ≈ 0.67–1.5x /
    # superior = 1.5x / overwhelming = 2.5x（00_defines.txt:710-711）。
    assert re.search(
        r"relative_power\s*=\s*\{[^}]*value\s*>\s*equivalent", body
    ), "军力比 +2 档缺少 value > equivalent（邻国高于我方）"
    assert re.search(
        r"relative_power\s*=\s*\{[^}]*value\s*>=\s*inferior", body
    ), "军力比 +1 档缺少 value >= inferior（邻国接近我方）"
    assert "any_war" in body and "any_defender" in body, (
        "威胁评估的战争分量未区分被宣战与主动宣战（规格书 §6 分量2）"
    )
    for name in ("is_rival", "civic_fanatic_purifiers"):
        assert name in body, f"威胁评估缺少宿敌/清洗者邻接分量（{name}，规格书 §6 分量3）"
    assert (
        "ai_invasion_happened" in body
        or "extradimensional_invasion_happened" in body
        or "prethoryn_invasion_happened" in body
    ), "威胁评估缺少危机年代分量（规格书 §6 分量4）"
    assert re.search(
        r"om_lex_threat\s+value\s*>=\s*5[\s\S]{0,200}?set_variable\s*=\s*\{\s*which\s*=\s*om_lex_threat\s+value\s*=\s*5",
        body,
    ), "威胁评估缺少 ≥5 夹紧 —— 分量和可达 6，会突破 0..5 分级"


def test_threat_evaluation_runs_after_the_era_ladder() -> None:
    """S-5：威胁评估必须排在年度效果里 om_era 阶梯之后。

    S-2 定下的约定：annual 里年代阶梯在最前，其后的一切年代相关逻辑
    （含威胁评估）才拿得到当年值；顺序回退会埋下难以归因的陈旧读数。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_annual = {")
    annual = _block_at(text, text.index("{", start))
    assert annual.index("om_era") < annual.index("overmind_evaluate_threat"), (
        "威胁评估必须放在 om_era 年度阶梯之后"
    )


# ==========================================================================
# S-6 —— 飞升天赋查表（2026-09-16，总裁席放行；执行手册 §飞升策略）
#
# ASCENSION 相位从「生成链按模式取用」升级为「按 om_era + 四道闸 + 决策树查表」：
#   1) 传统树按年代（era1 繁荣→扩张→发现；era2/era3+ 分支带年代门）；
#   2) 天赋按槽位表（1/2 纯增益过渡 → 3 路线选型 → 巨构/战争/防御按 era）；
#   3) 换线规则（era3 起路线关键科技不可见 → 剔除该路线候选）；
#   4) 危机天赋（become_the_crisis / cosmogenesis）绝不进自动查表。
# 引擎 add_ascension_perk 是资格真把关（D-6 同款哲学），本层只做
# 「什么时候值得尝试」的粗筛；生成文件 overmind_progression.txt 不动，
# 旧模式链留在原处仅不再被相位调用（test_autonomy_contract 的存在性契约不破坏）。
# ==========================================================================

ROUTE_PERKS = (
    "ap_engineered_evolution",
    "ap_mind_over_matter",
    "ap_synthetic_evolution",
    "ap_the_flesh_is_weak",
    "ap_organo_machine_interfacing",
)


def test_tradition_order_follows_the_era_table() -> None:
    """S-6：传统树按 om_era 查表，era1 只开手册开局三树。

    手册 §传统树优先级：era1 繁荣→扩张→发现；era2 分支（和谐/商贸/灵活）
    与 era3+ 分支（至霸/支配）必须带年代门，否则发展期会把凝聚力
    烧在军事树上 —— 那正是 P1「发展优先」要防的事。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_advance_tradition_by_era = {")
    assert start != -1, "找不到 overmind_advance_tradition_by_era"
    body = _block_at(text, text.index("{", start))
    # era1 开局三树，且顺序正确（繁荣 → 扩张 → 发现）
    i_prosperity = body.find("add_tradition = tr_prosperity_adopt")
    i_expansion = body.find("add_tradition = tr_expansion_adopt")
    i_discovery = body.find("add_tradition = tr_discovery_adopt")
    assert -1 < i_prosperity < i_expansion < i_discovery, (
        "era1 开局三树缺失或顺序错误（手册：繁荣→扩张→发现）"
    )
    # era2 / era3 分支必须带年代门（防发展期烧军事树）
    for tree, era in (("tr_harmony_adopt", ">= 2"), ("tr_supremacy_adopt", ">= 3")):
        m = re.search(
            rf"limit\s*=\s*\{{[^}}]*om_era\s+value\s*{era}[^}}]*NOT\s*=\s*\{{\s*has_tradition\s*=\s*{tree}",
            body,
        ) or re.search(
            rf"limit\s*=\s*\{{[^}}]*?NOT\s*=\s*\{{\s*has_tradition\s*=\s*{tree}\s*\}}[^}}]*?om_era\s+value\s*{era}",
            body,
        )
        assert m, f"{tree} 分支缺少 om_era {era} 年代门"
    # 相位接线：ASCENSION 相位调用新查表（替换旧模式链）
    p7 = text.find("overmind_autonomy_phase_ascension = {")
    phase = _block_at(text, text.index("{", p7))
    assert "overmind_advance_tradition_by_era" in phase, (
        "ASCENSION 相位未接 tradition_by_era —— 年代查表落空"
    )


def test_ascension_perk_picker_implements_gates_and_slots() -> None:
    """S-6：天赋查表必须先闸后树，槽 1/2 只给纯增益，路线须过四道闸。

    手册 §资格闸门：任何一道不满足即禁选（不是降权）。决策树要点：
    机械智能 → 机械专属（本池仅 assimilator 变体）；灵能须唯心/灵能理论；
    合成须过 blocks_ai_synthetic_evolution（政策闸，vanilla 同款）；
    生物为有机保底；无合格路线 → 只点普通天赋（步骤 0 兜底）。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_pick_ascension_perk = {")
    assert start != -1, "找不到 overmind_pick_ascension_perk"
    body = _block_at(text, text.index("{", start))
    p7 = text.find("overmind_autonomy_phase_ascension = {")
    phase = _block_at(text, text.index("{", p7))
    assert "overmind_pick_ascension_perk" in phase, (
        "ASCENSION 相位未接天赋查表器"
    )
    # 槽 1/2：纯增益过渡先行（索引在路线天赋之前 = 先闸后树之外的槽位序）
    i_first = body.find("add_ascension_perk = ap_technological_ascendancy")
    i_route = min(
        (body.find(f"add_ascension_perk = {p}") for p in ROUTE_PERKS if body.find(f"add_ascension_perk = {p}") != -1),
        default=-1,
    )
    assert i_first != -1, "槽 1 缺少纯增益天赋（科技飞升）"
    assert i_route != -1, "查表器缺少任何路线天赋"
    assert i_first < i_route, "路线天赋不得先于槽 1 纯增益（Wiki：路线要求先选 2 个普通天赋）"
    # 四道闸词汇：物种 / 起源 / 民议 / 政策（+自然设计全禁闸）
    for gate in (
        "is_machine_empire",
        "is_hive_empire",
        "has_origin",
        "has_valid_civic",
        "has_policy_flag",
        "is_natural_design_empire",
        "blocks_ai_synthetic_evolution",
    ):
        assert gate in body, f"天赋查表缺少资格闸门词汇：{gate}"
    # 槽位表：巨构门（星河奇迹要巨构工程学）
    assert re.search(
        r"ap_galactic_wonders[\s\S]{0,120}?has_technology\s*=\s*tech_mega_engineering"
        r"|has_technology\s*=\s*tech_mega_engineering[\s\S]{0,120}?ap_galactic_wonders",
        body,
    ), "星河奇迹分支缺少巨构工程学科技门"
    # 年代查表：era5 守护者 / era4 军力
    assert re.search(r"om_era value >= 5[\s\S]{0,200}?ap_defender_of_the_galaxy", body), (
        "era5 未查表到守护者天赋（Defender of the Galaxy）"
    )
    assert re.search(r"om_era value >= 4[\s\S]{0,200}?ap_galactic_force_projection", body), (
        "era4 未查表到军力天赋（Galactic Force Projection）"
    )
    # 换线规则：era3 起（years>=30）关键科技不可见 → 候选剔除
    assert re.search(
        r"years_passed\s*>=\s*30[\s\S]{0,200}?has_technology\s*=\s*tech_psionic_theory", body
    ) or re.search(
        r"has_technology\s*=\s*tech_psionic_theory[\s\S]{0,200}?years_passed\s*>=\s*30", body
    ), "换线规则缺失：era3 起灵能理论不可见应剔除灵能候选（手册 §换线规则）"


def test_crisis_perks_are_never_auto_granted() -> None:
    """S-6：危机天赋不进自动查表（手册：属用户级决策，只登记不做）。

    Cosmogenesis / Become the Crisis 会改写整局胜利目标，宣战六条件
    （S-3）管不住它们 —— 一旦出现在自治层脚本里即为违规。
    """
    text = _read(AUTONOMY)
    for banned in ("ap_become_the_crisis", "ap_cosmogenesis"):
        assert banned not in text, f"危机天赋 {banned} 不得进入自治层自动查表"


# ==========================================================================
# S-3 —— 宣战六条件评估器（2026-09-16，总裁席放行；规格书 §4.3）
#
# 评估与执行分离：S-3 交付六条件 scripted_trigger + WAR 相位评估日志；
# 实际 declare_war + 战争目标归 M3-4（依赖 M3-1 的 wg_* 调研）。
# ==========================================================================


def test_war_declaration_gate_implements_six_conditions() -> None:
    """S-3：宣战六条件逐条在触发器里，且评估器接进 WAR 相位。

    规格书 §4.3：①years>=30 ②目标军力<我方70% ③能源/合金收入为正+库存垫
    ④目标不在联邦（v1 取第一析支）⑤不打灭国战（触发器层近似：灭国型
    民议目标剔除）⑥威胁>=3 豁免库存条但能源门永不豁免（P6）。
    年代窗口 era3..era4：era1/2 无战争（手册），era5 仅防御反击。
    """
    text = _read(TRIGGERS)
    m = re.search(r"overmind_can_declare_war\s*=\s*\{", text)
    assert m, "触发器文件缺少 overmind_can_declare_war"
    body = _block_at(text, text.index("{", m.start()))
    assert re.search(r"years_passed\s*>=\s*30", body), "缺少条件1：years_passed >= 30"
    assert re.search(r"years_passed\s*<\s*150", body), "缺少 era5 封顶（手册：危机准备期仅防御反击）"
    assert re.search(
        r"relative_power\s*=\s*\{[^}]*value\s*<=\s*inferior", body
    ), "缺少条件2：目标军力低于我方（relative_power <= inferior；数值 token 非法，D-24）"
    assert "has_federation = no" in body, "缺少条件4：目标不在联邦"
    for civic in ("civic_fanatic_purifiers", "civic_hive_devouring_swarm", "civic_machine_terminator"):
        assert civic in body, f"缺少条件5近似：灭国型民议目标剔除（{civic}）"
    assert "overmind_can_afford" in body, "条件3 库存垫未走统一花销门（S-1 回退）"
    assert re.search(r"om_lex_threat value >= 3", body), "缺少条件6：威胁 >=3 豁免分支"
    # 能源门永不豁免：主分支与威胁豁免分支都必须各自查能源收入（P6）
    assert body.count("resource = energy value > 0") >= 2, (
        "威胁豁免分支缺能源收入门 —— 能源门任何姿态不豁免（P6 违反）"
    )
    # WAR 相位接线 + 可观测日志（D-7 哲学：评估必须留痕）
    text2 = _read(AUTONOMY)
    p8 = text2.find("overmind_autonomy_phase_war = {")
    phase = _block_at(text2, text2.index("{", p8))
    assert "overmind_can_declare_war" in phase, "WAR 相位未接宣战六条件评估器"
    assert "【宣战评估】" in phase, "宣战评估无日志标记 —— 评估结果不可观测（D-7 教训）"


def test_declaration_execution_stays_with_m3_4() -> None:
    """S-3：评估器不得真的宣战 —— declare_war 效果调用归 M3-4。

    战争目标（wg_*）还没调研（M3-1 未做），此时宣战只能打出无目标战争
    或触发引擎兜底行为；评估器只做条件判定与日志。
    注释里出现 declare_war 字样不违规（实现说明需要提到它），只拦调用行。
    """
    text = _read(AUTONOMY)
    assert not re.search(r"(?m)^\s*declare_war\s*=", text), (
        "自治层出现 declare_war 效果调用 —— 宣战执行越权（归 M3-4）"
    )


# ==========================================================================
# D-24 —— relative_power 数值 token 非法（2026-09-16 实机复现，WorkBuddy 登记）
#
# 引擎只接受定性等级 token：pathetic / inferior / equivalent / superior /
# overwhelming（数值比较写法实机每月刷 "Invalid relative power token"）。
# 等级带宽（Paradox Wiki Empire 页 + defines 00_defines.txt:710-711 交叉确证）：
#   equivalent ≈ 0.67x–1.5x；superior = 1.5x 起（RELATIVE_POWER_SUPERIOR=1.5）；
#   overwhelming = 2.5x 起（RELATIVE_POWER_OVERWHELMING=2.5）；inferior/pathetic
#   对称地低于 equivalent。算符 > >= = < <= 均有原版语料先例。
# ==========================================================================

_QUAL_TOKENS = ("pathetic", "inferior", "equivalent", "superior", "overwhelming")


# ==========================================================================
# S-12 —— 赤字局势管理（2026-09-16 交接自 WorkBuddy，规格见
# docs/玩法模型与策略基线.md §5；机制源 common/situations/02_deficit_situations.txt）
#
# 8 个赤字局势刻度一致：满 100 = 破产（deficit.110）。策略：进度 > 50（高危）
# 切削减型应对；进度 <= 50 且仍处于削减型 → 切回 do_nothing。
# 缺回退分支的后果：削减型（如 cut_science = -50% 研究员产出）永久生效。
# ==========================================================================

S12_SITUATIONS = {
    "situation_energy_deficit": "deficit_approach_cut_science_investment",
    "situation_mineral_deficit": "deficit_approach_cut_investment",
    "situation_food_deficit": "deficit_approach_invest_in_farmers",
    "situation_consumer_goods_deficit": "deficit_approach_cut_investment",
    "situation_alloys_deficit": "deficit_approach_cut_maintenance",
    "situation_rare_crystals_deficit": "deficit_approach_recycling",
    "situation_volatile_motes_deficit": "deficit_approach_recycling",
    "situation_exotic_gases_deficit": "deficit_approach_recycling",
}


def _situations_body() -> str:
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_situations = {")
    assert start != -1, "找不到 overmind_autonomy_situations"
    return _block_at(text, text.index("{", start))


def test_every_deficit_situation_has_an_escalation_branch() -> None:
    """S-12：8 个赤字局势逐块覆盖，且各按映射切换削减型应对。"""
    body = _situations_body()
    chunks = body.split("every_situation = {")[1:]
    assert len(chunks) >= 8, f"局势处理块只有 {len(chunks)} 个（应 ≥ 8）"
    for sit, approach in S12_SITUATIONS.items():
        chunk = next(
            (c for c in chunks if f"is_situation_type = {sit}" in c), None
        )
        assert chunk is not None, f"S-12：缺少局势 {sit} 的处理块"
        assert f"set_situation_approach = {approach}" in chunk, (
            f"S-12：局势 {sit} 未切换到对应应对 {approach}"
        )
        assert "situation_progress > 50" in chunk, (
            f"S-12：局势 {sit} 缺高危进度门（> 50/100）"
        )


def test_deficit_situations_fall_back_when_out_of_danger() -> None:
    """S-12：脱离高危必须切回默认应对 —— 缺回退会永久砍研究。

    cut_science = -50% 研究员产出，而科技力占实测分数 99.8%：只升级不回退
    的局势管理比没有局势管理更糟。
    """
    body = _situations_body()
    assert body.count("situation_progress <= 50") >= 8, (
        "回退门（<= 50）不足 8 处 —— 部分局势脱离高危后不会恢复"
    )
    assert body.count("set_situation_approach = deficit_approach_do_nothing") >= 8, (
        "恢复默认应对不足 8 处 —— 削减型将永久生效"
    )


def test_situation_handler_is_wired_into_the_tick() -> None:
    """S-12：局势处理器必须挂入月度 tick（防死代码）。"""
    text = _read(AUTONOMY)
    assert "overmind_autonomy_situations = yes" in text, (
        "overmind_autonomy_situations 未挂入月度 tick —— 死代码"
    )


# ==========================================================================
# D-26 —— 引用未定义的 mod 内 scripted_trigger/effect（2026-09-16 实机抓获）
#
# S-11 合金救火分支引用 `overmind_autonomy_deficit_alloys` 但从未定义 →
# 引擎静默判假，合金赤字救火从未生效（error.log: "Scripted Trigger ... is
# invalid"）。script_audit 只对游戏语料/exe 查未知键，mod 内部「引用 vs 定义」
# 的配对是审计盲区 —— 本守卫补位：每个 `overmind_xxx = yes` 引用必须能找到
# `overmind_xxx = {` 顶层定义（common 的 effects/triggers 全目录扫描）。
# ==========================================================================


def test_every_referenced_overmind_script_is_defined() -> None:
    """D-26：mod 内被引用的 overmind_* 脚本必须有顶层定义，缺失即红。"""
    effects_dir = MOD_DIR / "common/scripted_effects"
    triggers_dir = MOD_DIR / "common/scripted_triggers"
    events_dir = MOD_DIR / "events"
    defs: set[str] = set()
    refs: set[str] = set()
    for f in list(effects_dir.glob("*.txt")) + list(triggers_dir.glob("*.txt")):
        text = _read(f)
        defs.update(m.group(1) for m in re.finditer(r"(?m)^(overmind_\w+) = \{", text))
        refs.update(
            m.group(1)
            for m in re.finditer(r"(?m)^[ \t]+(overmind_\w+) = yes", text)
        )
    for f in events_dir.glob("*.txt"):
        refs.update(
            m.group(1)
            for m in re.finditer(r"(?m)^[ \t]+(overmind_\w+) = yes", _read(f))
        )
    missing = sorted(r for r in refs if r not in defs)
    assert not missing, (
        f"以下 overmind_* 被引用但从未定义（引擎静默判假，D-26 类缺陷）：{missing}"
    )


# ==========================================================================
# S-13 —— 内阁议程（2026-09-16；规格 docs/玩法模型与策略基线.md §2.1）
#
# 社区序：agenda_infinite_opportunities（全民幸福 +4%）→ agenda_unlock_slot
# （+1 内阁席位，原版 ai_weight 9999）。⚠️ 名字确证（zcode 复核）：两个效果名
# 仅存在于 exe 标识符表（common/ 零原版用法，参数形态属推断，待实机复验）；
# add_council_agenda_progress_percent exe/语料双零 → 弃用。「启动已就绪议程」
# 无脚本效果（UI 硬边界）→ 只做选择；若引擎准备完毕即自动结算则链自然推进，
# 否则停在第一档（下局日志观察项）。
# ==========================================================================


def test_agenda_selection_follows_community_order() -> None:
    """S-13：议程按社区序选择，且只在空闲时选（不重设推进中的议程）。"""
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_agenda = {")
    assert start != -1, "找不到 overmind_autonomy_agenda"
    body = _block_at(text, text.index("{", start))
    i1 = body.find("set_council_agenda = agenda_infinite_opportunities")
    i2 = body.find("set_council_agenda = agenda_unlock_slot")
    assert i1 != -1 and i2 != -1, "缺社区默认议程两档（infinite_opportunities / unlock_slot）"
    assert i1 < i2, "议程选择顺序错误（社区序：先 infinite_opportunities 后 unlock_slot）"
    assert "has_agenda_selected = no" in body, (
        "议程选择缺空闲门 —— 议程推进中重设会浪费内阁进度"
    )
    assert "om_agenda_1_done" in body and "om_agenda_2_done" in body, (
        "缺议程进度旗 —— 无法按序推进两档"
    )


def test_agenda_handler_is_wired_into_the_tick() -> None:
    """S-13：议程处理器必须挂入月度 tick（防死代码）。"""
    text = _read(AUTONOMY)
    assert "overmind_autonomy_agenda = yes" in text, (
        "overmind_autonomy_agenda 未挂入月度 tick —— 死代码"
    )


# ==========================================================================
# S-18 批次① —— LLM 决策规则求值器骨架（2026-09-16；规格 v4 §3.1-3.4/§5）
#
# 规则形态取代 8 槽轮转：LLM 产出「条件码 → 相位」的规则集（顺序=优先级），
# 游戏内每月用真实局势全量求值。本批次：单条件求值器 + 兜底 + 降级回退
# （验收 #1 顺序敏感 / #3 降级可用 / #4 日志可解释；复合条件=批次②、
# 并行去重=批次③、收益门=批次④）。嵌套预算：求值器挂 tick（深 3），
# cond_check 参数化效果（深 4）；执行器 run_slot 峰值 5 层顶格。
# ==========================================================================


def test_s18_rule_ladder_is_order_sensitive() -> None:
    """S-18 验收#1：规则按行号升序求值 —— 顺序即优先级，调换规则结果必须变。"""
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_evaluate = {")
    assert start != -1, "找不到 overmind_autonomy_evaluate"
    body = _block_at(text, text.index("{", start))
    positions = [
        body.find(f"overmind_cond_check = {{ VNAME = om_rule_{n}_cond ")
        for n in range(1, 13)
    ]
    assert all(p != -1 for p in positions), "求值器未覆盖规则 1..12"
    assert positions == sorted(positions), "规则求值未按行号升序 —— 优先级语义被破坏"


def test_s18_condition_code_dispatch_is_wired() -> None:
    """S-18：条件码 → 触发器的派发必须齐全且映射正确（规格 §3.2）。"""
    for code, marker in (
        (1, "overmind_autonomy_deficit_energy"),
        (2, "overmind_autonomy_deficit_food"),
        (3, "overmind_autonomy_deficit_consumer_goods"),
        (4, "overmind_autonomy_deficit_alloys"),
        (5, "is_at_war"),
        (6, "om_lex_threat"),
        (7, "any_situation"),
        (8, "overmind_autonomy_needs_housing"),
        (9, "overmind_autonomy_needs_amenities"),
    ):
        m = re.search(
            rf"overmind_cond_{code}\s*=\s*\{{([^}}]*)}}", _read(TRIGGERS)
        )
        assert m, f"缺少条件码触发器 overmind_cond_{code}"
        assert marker in m.group(1), f"overmind_cond_{code} 映射错误（应含 {marker}）"
    assert re.search(r"overmind_cond_99\s*=\s*\{[^}]*always\s*=\s*yes", _read(TRIGGERS)), (
        "缺少兜底条件码 overmind_cond_99"
    )


def test_s18_falls_back_to_rotation_when_rules_missing() -> None:
    """S-18 验收#3：规则集缺失 → 回退 8 槽轮转，绝不空转。"""
    text = _read(AUTONOMY)
    assert "overmind_autonomy_evaluate = yes" in text, "求值器未挂入 tick"
    # 执行分发内联在 tick（不包执行器效果——否则相位扣费顶到第 6 层，D-26 前车之鉴）
    for n in (1, 2, 3):
        assert f"SLOT = om_exec_{n}" in text, f"执行分发缺少 om_exec_{n}"
    # 回退分支必须仍然保留 8 槽轮转（分发行恰好 8 条——初始化行不算）
    assert text.count("SLOT = om_lex_slot") == 8, (
        f"轮转分发行只剩 {text.count('SLOT = om_lex_slot')} 条（应恰 8 条）—— 回退链路残缺"
    )
    # 门：有规则才求值（rule_count >= 1），否则走轮转
    assert "om_lex_rule_count value >= 1" in text, "求值门（rule_count >= 1）缺失"


def test_s18_composite_conditions_combine_all_three_ops() -> None:
    """S-18 验收#6（v2）：复合条件 AND/OR/NOT 三算子齐备，且组合必须以
    「存在副条件」为前提 —— cond2=0（单条件退化）时主条件真值即结果，
    不得被 AND 的假副条件清零（缺省退化安全性，MSG-007 裁定 ④）。"""
    text = _read(AUTONOMY)
    start = text.find("overmind_cond_check = {")
    assert start != -1, "找不到 overmind_cond_check"
    body = _block_at(text, text.index("{", start))
    # 副条件派发必须存在（10 码 + 兜底 → om_rule_hit2）
    assert "which = $VNAME2$" in body and "om_rule_hit2" in body, (
        "复合条件缺副条件派发（$VNAME2$ → om_rule_hit2）"
    )
    assert body.count("which = $VNAME2$") >= 11, "副条件派发分支不足（应 10 码 + 兜底）"
    # 组合必须以「存在副条件」为前提（cond2=0 退化保护门）
    assert "which = $VNAME2$ value >= 1" in body, "组合缺 cond2 >= 1 退化保护门"
    # 三算子各用**唯一特征签名**断言（子串 "which = $OP$ value = 2/3" 在否定门里
    # 也会出现，不能作为分支存在性证据 —— 注入 B 的教训）：
    # OR（op=2）唯一动作 = 把 hit2 拷入 hit；
    assert 'set_variable = { which = om_rule_hit value = om_rule_hit2 }' in body, (
        "缺少 OR（op=2）组合分支（特征：hit2 拷入 hit）"
    )
    # NOT（op=3）唯一动作 = hit2 为真时清零 hit；
    assert (
        "which = $OP$ value = 3" in body
        and "check_variable = { which = om_rule_hit2 value = 1 }" in body
    ), "缺少 NOT（op=3）组合分支（特征：hit2=1 清零 hit）"
    # AND（缺省 1，且排除 2/3 后兜底应用）= hit2 为假时清零 hit；
    and_gate = (
        "NOT = { check_variable = { which = $OP$ value = 2 } }" in body
        and "NOT = { check_variable = { which = $OP$ value = 3 } }" in body
    )
    assert and_gate and "which = om_rule_hit2 value = 0" in body, (
        "缺少 AND（op=1 缺省）组合分支 —— 副条件为假时主条件命中必须清零"
    )


def test_s18_same_act_dedup_keeps_first() -> None:
    """S-18 验收#9：同相位去重 —— 多规则命中同一相位只执行行号最小者。"""
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_evaluate = {")
    body = _block_at(text, text.index("{", start))
    # 去重判据：命中后逐一比对 om_exec_1..3 是否已含本规则 act，命中即打回
    for slot in (1, 2, 3):
        assert (
            f"check_variable = {{ which = om_exec_{slot} value = om_rule_" in body
        ), f"去重判据缺少 om_exec_{slot} 比对"
    # 逐规则配对：每条规则的去重判据必须比对**它自己的 act**（防单点删除漏网）
    for n in range(1, 13):
        for slot in (1, 2, 3):
            assert (
                f"check_variable = {{ which = om_exec_{slot} value = om_rule_{n}_act }}" in body
            ), f"规则 {n} 的去重判据缺少 om_exec_{slot} = om_rule_{n}_act 比对"
    assert "去重跳过" in body, "去重跳过无日志（不可解释）"


def test_s18_parallel_cap_truncates() -> None:
    """S-18 验收#8（A2 配套）：并行上限 om_parallel_max 生效，超限截断。"""
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_evaluate = {")
    body = _block_at(text, text.index("{", start))
    # 截断三件套缺一不可：槽位上限 / 两档 parallel_max 逐档门 / 截断日志
    assert "check_variable = { which = om_exec_count value >= 3 }" in body, "截断缺槽位上限（3）判据"
    assert "om_parallel_max value = 2" in body and "om_parallel_max value = 1" in body, (
        "截断缺 parallel_max 逐档门（2/1）"
    )
    assert "执行集已满" in body, "截断无日志（不可解释）—— 文案须与「同相位去重」可区分（WDMSG-068 缺陷B）"
    # 初始化：rules_apply 必须把上限显式置 3（D-6：未设置读数即假，会把执行集清空）
    rules = _read(MOD_DIR / "common/scripted_effects/overmind_lex_rules.txt")
    assert "om_parallel_max value = 3" in rules, "rules_apply 未初始化 parallel_max = 3"


def test_s18_fallback_runs_only_on_empty_set() -> None:
    """S-18 验收#10（v3 语义修正）：兜底只在执行集为空时执行。

    cond=99 在批次②的 cond_check 里是恒真条件 —— 若求值器不特判，
    兜底会每月白做一次。结构约束：兜底捕获以 fallback_act=0 为门，
    兜底执行以 om_exec_count=0 为门（有命中时兜底不得执行）。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_evaluate = {")
    body = _block_at(text, text.index("{", start))
    assert "om_fallback_act" in body, "求值器未捕获兜底规则动作"
    assert "om_rule_1_cond value = 99" in body, "兜底捕获缺 cond=99 判据"
    assert "om_fallback_act value = 0" in body, "兜底捕获缺「首个」门（防多兜底覆盖）"
    tick_i = text.find("overmind_autonomy_evaluate = yes")
    tick_tail = text[tick_i:tick_i + 40000]
    assert "om_exec_count value = 0" in tick_tail, (
        "兜底执行缺「执行集为空」门 —— 有命中时兜底不得执行（每月白做一次）"
    )


def test_s18_evaluation_never_stops_early() -> None:
    """S-18 验收#8（A2 核心）：求值不早停 —— 规则块不得以执行集状态为前置门。

    早停实现会在首个命中后跳过其余规则求值；结构约束：evaluate 内禁止
    「om_exec_count = 0」形态的规则门（A2：全量求值，命中的都进执行集）。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_evaluate = {")
    body = _block_at(text, text.index("{", start))
    # 只禁「门形态」（limit 里的 check_variable），set_variable 的合法重置不算
    assert "check_variable = { which = om_exec_count value = 0" not in body, (
        "求值器内出现执行集空判 —— 疑似早停实现（A2 违规：应全量求值）"
    )


def test_s18_gain_circuit_breaker_exists() -> None:
    """S-18 验收#11（A3 核心）：收益熔断 —— 连续 3 月无正收益的相位被跳过。

    结构四件套：①执行分发按相位查 streak >= 3（熔断门，不占配额）——熔断门
    在 tick 分发块（不在 evaluate）；②相位末尾自报收益（om_did_* 直拷）；
    ③slot 执行后清旗标（防串位误判）；④streak 记账（正收益清零 / 否则累加）。
    阈值用字面量 3：check_variable 的变量比较语法无原版先例（确证优先），
    spec 的 neg_max 可变阈值待实机确证后升级。
    """
    text = _read(AUTONOMY)
    # D-41（2026-09-19）：直拷形态 `value = om_did_*` 把旗标当数值读恒 0（熔断单向锁死根因）
    assert "value = om_did_" not in text, (
        "自报直拷形态回流 —— 旗标当数值 = gain 恒 0 = 熔断单向锁死"
    )
    for n in range(1, 9):
        assert f"set_variable = {{ which = om_lex_gain_p{n} value = 1 }}" in text, (
            f"相位 {n} 缺收益自报（has_country_flag 条件赋值形态）"
        )
    for n in range(1, 9):
        assert f"om_lex_gain_p{n}_streak value >= 3" in text, (
            f"熔断门缺少相位 {n} 的 streak >= 3 判据"
        )
    assert text.count("om_lex_gain_p1_streak value >= 3") >= 3, (
        "熔断门覆盖不足（应每个执行 slot 一道）"
    )
    # slot 执行后清旗标（防多相位共享 om_did_build 串位误判）
    assert text.count("remove_country_flag = om_did_build") >= 3, (
        "执行分发缺旗标清理 —— 多相位共享 om_did_build 会串位误判收益"
    )


def test_s18_circuit_breaker_resets_on_rev() -> None:
    """S-18 验收#12：熔断可解除 —— 换版（rev 递增）通道清零全部 streak。

    rules_apply 由 lex_apply 在换版时调用：streak 清零写在这里即覆盖解除条件③
    （rev 递增）；兜底（条件码 99）天然不走熔断门 = 解除条件④。
    """
    rules = _read(MOD_DIR / "common/scripted_effects/overmind_lex_rules.txt")
    for n in range(1, 9):
        assert f"om_lex_gain_p{n}_streak value = 0" in rules, (
            f"rules_apply 未清零相位 {n} 的 streak —— 熔断将永久化（验收#12）"
        )


def test_s18_fallback_bypasses_the_breaker() -> None:
    """S-18 验收#13：兜底不熔断 —— 零命中时兜底执行不得被熔断门拦截。"""
    text = _read(AUTONOMY)
    i = text.find("SLOT = om_fallback_act")
    assert i != -1, "兜底分发缺失"
    blk_start = text.rfind("if = {", 0, i)
    blk = text[blk_start:i]
    assert "om_lex_gain" not in blk and "streak" not in blk and "熔断" not in blk, (
        "兜底分发自身的 limit 带了熔断判据 —— 零命中时会被拦（验收#13 违规）"
    )


def test_low_stockpile_family_refactored_and_consumed() -> None:
    """S-19 必改1（裁定 §2）：库存触发器重构为单资源三支且**真实被消费**。

    D-28 真身：low_stockpile/rich_enough_to_build 写好了却零引用（定义了没人用，
    D-26 的反向同族病）。守卫断言：①三支单资源触发器存在；②low_stockpile =
    三者 OR（语义等价）；③low_minerals 至少 1 个消费点（JOBS 救火）。
    """
    t = _read(TRIGGERS)
    for name, res, thr in (
        ("overmind_autonomy_low_minerals", "minerals", "200"),
        ("overmind_autonomy_low_energy", "energy", "200"),
        ("overmind_autonomy_low_food", "food", "100"),
    ):
        m = re.search(rf"{name}\s*=\s*\{{([^}}]*)}}", t)
        assert m, f"缺少触发器 {name}"
        assert f"resource = {res}" in m.group(1) and f"value < {thr}" in m.group(1), (
            f"{name} 判据错误（应 {res} < {thr}）"
        )
    ls = re.search(r"overmind_autonomy_low_stockpile\s*=\s*\{([^}]*)\}", t)
    assert ls and "overmind_autonomy_low_minerals = yes" in ls.group(1), (
        "low_stockpile 未重构为三支 OR（语义等价改造缺失）"
    )


def test_s19_jobs_relief_branch_builds_mining_district() -> None:
    """S-19 必改1/2（裁定 §2/§3）：JOBS 救火消费 low_minerals → 建采矿区划，
    专用垫 100 且走统一花销门（能源收入门不豁免）。"""
    t = _read(AUTONOMY)
    assert "overmind_autonomy_low_minerals = yes" in t, "JOBS 未消费 low_minerals（D-28 修①落空）"
    assert "overmind_afford_district_district_mining_relief" in t, "JOBS 救火未走专用垫"
    trig = _read(TRIGGERS)
    m = re.search(r"overmind_afford_district_district_mining_relief\s*=\s*\{", trig)
    assert m, "缺少救火专用 afford 触发器"
    block = _block_at(trig, trig.index("{", m.start()))
    assert "om_lex_rule_" not in block
    # 救火垫 100：显式库存判据 + 统一花销门（裁定 §2 建议形态，双保险）
    assert "resource_stockpile_compare = { resource = minerals value >= 100 }" in block, (
        "救火垫不是 100（裁定分档表：救火态 ≥100）"
    )
    assert "overmind_can_afford" in block, "救火 afford 未走统一花销门（能源门必须在场）"


def test_s18_condition_codes_16_to_20_dispatched() -> None:
    """S-19 条件码 v2（裁定 §4）：码 16-20 主/副两段派发都必须接通。"""
    text = _read(AUTONOMY)
    expectations = {
        16: "resource_stockpile_compare = { resource = minerals value < 200 }",
        17: "resource_stockpile_percent = { resource = energy value >= 0.9 }",
        18: "resource_stockpile_percent = { resource = food value >= 0.9 }",
        19: "resource_stockpile_compare = { resource = consumer_goods value < 200 }",
        20: "resource_stockpile_compare = { resource = alloys value < 200 }",
    }
    trig = _read(TRIGGERS)
    for code, judgement in expectations.items():
        m = re.search(rf"overmind_cond_{code}\s*=\s*\{{", trig)
        assert m, f"缺少条件码触发器 overmind_cond_{code}"
        blk = _block_at(trig, trig.index("{", m.start()))
        assert re.sub(r"\s+", "", judgement) in re.sub(r"\s+", "", blk), (
            f"overmind_cond_{code} 判据与裁定不符（应 {judgement}）"
        )
    body_start = text.find("overmind_cond_check = {")
    body = _block_at(text, text.index("{", body_start))
    for code in range(16, 21):
        assert f"which = $VNAME$ value = {code}" in body, (
            f"主条件派发缺码 {code}"
        )
        assert f"which = $VNAME2$ value = {code}" in body, (
            f"副条件派发缺码 {code}"
        )


def test_s18_era_codes_11_to_15_dispatched() -> None:
    """S-19/D-31（裁定 §4.2-4.3）：年代码 11-15 两段派发补齐（原为潜伏恒假）。"""
    text = _read(AUTONOMY)
    body_start = text.find("overmind_cond_check = {")
    body = _block_at(text, text.index("{", body_start))
    for era in range(1, 6):
        code = 10 + era
        assert f"which = $VNAME$ value = {code}" in body, f"主条件派发缺年代码 {code}"
        assert f"which = $VNAME2$ value = {code}" in body, f"副条件派发缺年代码 {code}"
    trig = _read(TRIGGERS)
    for era in range(1, 6):
        m = re.search(rf"overmind_cond_{10 + era}\s*=\s*\{{", trig)
        assert m, f"缺少年代码触发器 overmind_cond_{10 + era}"
        blk = _block_at(trig, trig.index("{", m.start()))
        assert f"om_era value = {era}" in blk, f"码 {10+era} 未映射 om_era = {era}"


def test_s19_full_cap_inhibits_district_builds() -> None:
    """S-19 裁定 §3.2/d：能源或食物 ≥90% 存储上限时，停建对应区划（resource_stockpile_percent）。"""
    t = _read(AUTONOMY)
    assert (
        "resource_stockpile_percent = { resource = energy value >= 0.9 }" in t
        or "resource_stockpile_percent = { resource = energy value >= 0.9 }" in _read(TRIGGERS)
    ), "缺能源满仓判据（resource_stockpile_percent）"
    assert "resource_stockpile_percent = { resource = food value >= 0.9 }" in _read(TRIGGERS) or (
        "resource_stockpile_percent = { resource = food value >= 0.9 }" in t
    ), "缺食物满仓判据"


# ==========================================================================
# S-18 批次④b —— 默认决策规则集（规格 docs/默认决策规则集_规格.md §3 档 B + §8）
# ==========================================================================

IMPLEMENTED_CODES = set(range(1, 10)) | set(range(11, 21)) | {99}  # 10 = 恒假占位


def _default_ruleset() -> list:
    from engine import strategy_lex as lex
    return lex.DEFAULT_RULES


def test_default_ruleset_uses_only_implemented_condition_codes() -> None:
    """S-18 验收 C3（本轮最重要）：默认规则集条件码 ⊆ 已实现码集合。

    D-26（触发器未定义）/ D-31（码未实现）两次实机静默失败之后，这是唯一能在
    编译期拦住同类复发的机制。码 10 = 恒假占位，严禁入集。
    """
    rules = _default_ruleset()
    assert rules, "默认规则集为空"
    for i, r in enumerate(rules, start=1):
        for field in ("cond", "cond2"):
            code = r[field]
            if code == 0:
                continue
            assert code in IMPLEMENTED_CODES, (
                f"规则 {i} 的 {field} 用了未实现条件码 {code}（D-26/D-31 型静默失败）"
            )


def test_default_ruleset_structure_matches_spec() -> None:
    """S-18 验收 A3/A4/A5：条数 ≤12、act 全覆盖 1..8、有且仅一条兜底且收尾。"""
    rules = _default_ruleset()
    assert 1 <= len(rules) <= 12, f"规则条数 {len(rules)} 越界"
    acts = {r["act"] for r in rules}
    assert acts == set(range(1, 9)), f"8 相位覆盖不全：缺 {set(range(1,9)) - acts}"
    fallbacks = [i for i, r in enumerate(rules, start=1) if r["cond"] == 99]
    assert fallbacks == [len(rules)], (
        f"兜底 cond=99 必须有且仅有一条且收尾（实际：{fallbacks}）"
    )


def test_s18_renderer_emits_rule_variables() -> None:
    """S-18 验收 A1：渲染器输出 om_rule_N_* 四字段 + rule_count = 条数。"""
    from engine import strategy_lex as lex
    payload = lex.LexPayload(rev=1)
    _, effects_text = lex.render_lex_files(payload)
    count = len(payload.rules)
    for n in range(1, count + 1):
        for field in ("cond", "cond2", "op", "act"):
            assert f"which = om_rule_{n}_{field} value =" in effects_text, (
                f"渲染缺 om_rule_{n}_{field}"
            )
    assert f"om_lex_rule_count value = {count}" in effects_text, "rule_count 与条数不一致"


def test_s8_anchorage_branch_protects_shipyard() -> None:
    """S-8/V-6（裁定 A+）：锚地分支必须跳过含船坞模块的星堡（硬底线）。

    裁定 MSG-033：shipyard 是硬底线，任何情况下不得覆盖；且不得写入
    已有锚地的星堡（幂等）。原版 paragon_effects:1264 同款条件式放置先例。
    """
    t = _read(AUTONOMY)
    assert "module = anchorage" in t, "锚地分支缺失"
    # 全文件断言（该分支的三个门是全文件唯一形态，窗口切片对并发写脆弱）
    assert "NOT = { has_starbase_building = building_shipyard }" not in t, (
        "出现了无效的 building_shipyard 判据（D-8：该 id 不存在）"
    )
    assert "NOT = { has_starbase_module = shipyard }" in t, (
        "锚地分支缺船坞保护门（shipyard 模块不得覆盖，裁定 A+ 硬底线）"
    )
    assert "NOT = { has_starbase_module = anchorage }" in t, (
        "锚地分支缺幂等门（已有锚地不得重写）"
    )


def test_d25_colony_call_precedes_outposts() -> None:
    """D-25 验收 AC-1：殖民调用行号必须早于两处拓土调用（顺序即优先级）。"""
    t = _read(AUTONOMY)
    i_colony = t.find("overmind_expand_try_colony = yes")
    i_out1 = t.find("overmind_expand_try_outpost = yes")
    i_out2 = t.find("overmind_expand_try_outpost = yes", i_out1 + 1)
    assert -1 < i_colony < i_out1 < i_out2, (
        "调用序错误：殖民必须在两处前哨拓土之前（D-25 M1）"
    )


def test_d25_outpost_influence_reserve_gate() -> None:
    """D-25 验收 AC-2（规格 §3.2 M2）：拓土影响力门双分支 ——
    无可殖民星球 ⇒ >=75；有可殖民星球 ⇒ >=175（预留殖民成本 100）。
    断言用两分支**各自唯一特征签名**（否定门会掩盖，批次②教训）。"""
    t = _read(AUTONOMY)
    # 分支一唯一特征：NOT 可殖民 + 75
    assert (
        "NOT = { any_planet_within_border = { is_colonizable = yes } }" in t
        and "resource_stockpile_compare = { resource = influence value >= 75 }" in t
    ), "缺无殖民分支（>= 75）"
    # 分支二唯一特征：可殖民 + 175
    assert (
        "any_planet_within_border = { is_colonizable = yes }" in t
        and "resource_stockpile_compare = { resource = influence value >= 175 }" in t
    ), "缺有殖民分支（>= 175）"
    # 两判据同源（is_colonizable 同一写法，防漂移 —— 裁定 §3.2 要点）


def test_d25_influence_gate_dual_branch() -> None:
    """D-25 验收 AC-2 补充：拓土 limit 内同时存在两 AND 分支（唯一签名）。"""
    t = _read(AUTONOMY)
    assert (
        "NOT = { any_planet_within_border = { is_colonizable = yes } }" in t
    ), "缺分支一（NOT colonizable）"
    assert (
        "resource_stockpile_compare = { resource = influence value >= 75 }" in t
    ), "缺分支一阈值（75）"
    assert (
        "resource_stockpile_compare = { resource = influence value >= 175 }" in t
    ), "缺分支二阈值（175，= 75 + 100 殖民预留）"


# ==========================================================================
# S-20 —— LLM 复盘迭代闭环（规格 docs/LLM复盘迭代闭环规格.md §4 六条）




def test_s19_capacity_buildings_require_zone_precondition() -> None:
    """S-19 裁定 §5：产能建筑必须有专业区前置门（否则 add_building 静默失败）。"""
    t = _read(AUTONOMY)
    for building, zone in (
        ("building_factory_1", "zone_factory"),
        ("building_foundry_1", "zone_industrial"),
    ):
        i = t.find(f"overmind_charge_building_{building} = yes")
        assert i != -1, f"缺少产能建筑分支：{building}"
        seg_start = t.rfind("random_owned_planet = {", 0, i)
        seg = t[seg_start:i]
        assert f"num_zones = {{ type = {zone} value >= 1 }}" in seg, (
            f"{building} 分支缺专业区前置门（同星球 {zone} >= 1，否则静默失败）"
        )


def test_s19_cg_starved_switches_to_civilian_economy() -> None:
    """S-19/D-28：消费品短缺 → 民用经济（+25% 工匠产出，00_policies:3579）。

    社区共识（Reddit/Paradox 论坛）：CG 赤字期「暂停合金扩军、保民生产出」。
    经济链头插 CG 分支，能源赤字退为次席。
    """
    t = _read(AUTONOMY)
    i = t.find("overmind_autonomy_deficit_consumer_goods = yes")
    assert i != -1, "经济姿态缺 CG 分支"
    seg = t[max(0, i - 400):i]
    assert "economic_policy_civilian" in t[i:i + 600] or "economic_policy_civilian" in seg, (
        "CG 分支未切换民用经济"
    )


def test_s19_mercantile_trade_policy_wired() -> None:
    """S-19/D-29：Mercantile 传统 → 消费品福利贸易政策（贸易值转 CG）。

    社区共识（Reddit 1ed6csv）：Consumer Benefits 贸易政策直接以贸易值抵 CG。
    前置门与原版 potential 同门（tr_mercantile_adaptive_economic_policies），
    幂等设置（NOT has_policy_flag = trade_conversion_consumer_goods）。
    """
    t = _read(AUTONOMY)
    i = t.find("trade_policy_consumer_goods")
    assert i != -1, "贸易政策未接线"
    seg = t[max(0, i - 500):i]
    assert "tr_mercantile_adaptive_economic_policies" in seg, (
        "贸易政策切换缺 Mercantile 传统前置门"
    )
    assert "NOT = { has_policy_flag = trade_conversion_consumer_goods }" in seg, (
        "贸易政策缺幂等门（已设时不得重复 set_policy）"
    )


def test_s8_peace_seeking_wired_and_gated() -> None:
    """S-8：止战止损 —— 消耗度 75%+ 即 end_war_effect（双侧），挂月度 tick。

    接口（裁定前已确证）：every_war 效果（语料 27 处，原版任务系统 war scope
    内配 end_war_effect）；attacker/defender_war_exhaustion（0-1 刻度，
    00_scripted_triggers 联邦 AI 用 0.6/0.8 阈值）；end_war_effect
    （00_scripted_effects:3871 "Use in war scope. War is deleted."）。
    """
    t = _read(AUTONOMY)
    start = t.find("overmind_autonomy_war_management = {")
    assert start != -1, "找不到 overmind_autonomy_war_management"
    body = _block_at(text := _read(AUTONOMY), text.index("{", start))
    # 双侧止战：防守方（defender WE）与进攻方（attacker WE）都要有
    assert "defender_war_exhaustion >= 0.75" in body, "缺防守侧止战判据（WE >= 0.75）"
    assert "attacker_war_exhaustion >= 0.75" in body, "缺进攻侧止战判据（WE >= 0.75）"
    assert body.count("end_war_effect = yes") >= 2, "止战动作不足两处"
    assert "【止战】" in body, "止战无日志（D-7 教训）"
    # 接线
    assert "overmind_autonomy_war_management = yes" in t, "战时管理未挂入 tick"


def test_s8_wartime_shipbuilding_exemption() -> None:
    """S-8：战时建舰 —— is_at_war 加入发展优先门的豁免组（战时军备优先）。"""
    t = _read(AUTONOMY)
    start = t.find("overmind_autonomy_build_ship = {")
    assert start != -1
    end = t.find("overmind_autonomy_phase_military = {", start)
    ship_body = t[start:end if end != -1 else len(t)]
    assert "is_at_war = yes" in ship_body, "建舰门缺战时豁免（S-8）"


def test_s18_evaluation_logs_are_interpretable() -> None:
    """S-18 验收#4：求值必须留痕 —— 命中、零命中都要能从日志看出为什么。"""
    text = _read(AUTONOMY)
    assert "【求值】" in text, "求值器无【求值】日志 —— 决策不可解释（D-7 教训）"
    assert text.count("命中 → 入执行集") == 12, (
        f"逐规则命中日志只有 {text.count('【求值】规则 #')} 条（应 12 条）—— 每条规则都必须留痕"
    )
    assert "零命中" in text, "零命中路径无日志"


def _iter_relative_power_blocks(text: str):
    for m in re.finditer(r"relative_power\s*=\s*\{[^}]*\}", text):
        yield m.group(0)


def test_relative_power_uses_only_qualitative_tokens() -> None:
    """D-24：全 mod 的 relative_power 一律用定性 token，数值比较零容忍。

    `value < 0.7` 这类写法引擎静默判假并刷 error.log（22 条/月，实机取证），
    守卫断言与运行时行为同时失效 —— 必须在测试期拦下。
    """
    for path in (AUTONOMY, TRIGGERS):
        text = _read(path)
        for block in _iter_relative_power_blocks(text):
            if "value" not in block:
                continue
            m = re.search(r"value\s*(?:>=|<=|>|<|=)\s*([A-Za-z0-9_.]+)", block)
            assert m, f"{path.name}: relative_power 块缺 value 比较：{block!r}"
            token = m.group(1)
            assert token in _QUAL_TOKENS, (
                f"{path.name}: relative_power 用了非法 token {token!r}"
                f"（只允许 {'/'.join(_QUAL_TOKENS)}）—— D-24 回归"
            )


# ==========================================================================
# S-20 —— LLM 复盘迭代闭环（规格 docs/LLM复盘迭代闭环规格.md §4 六条）
# ==========================================================================


# ==========================================================================
# S-15 —— 舰队无领袖判据形态（2026-09-18 实机回归）
# ==========================================================================

def test_s15_fleet_leaderless_filter_uses_exists_form() -> None:
    """leader = {} 从 fleet scope 直接切换是 Invalid context switch（实机
    error.log 五支舰队各 1 条，整个补领袖 if 恒假静默失效）。
    舰队无领袖判定必须用 NOT = { exists = leader } —— 舰队 scope 原版先例
    nomads_effects.txt:792（champions_forge_contestant_fleet）、
    caravaneer_scripted_effects.txt:212；论坛同口径（exists 不做 context 切换）。
    """
    t = _read(AUTONOMY)
    start = t.index("overmind_autonomy_manage_leaders = {")
    block = t[start:t.index("\n}", start)]
    assert "leader = { always" not in block, (
        "S-15 死形态回流：fleet scope 直接切 leader 会 Invalid context switch 静默失效"
    )
    assert block.count("NOT = { exists = leader }") == 4, (
        "S-15 两类舰队 × any/random 共四处判定，都必须用 exists 形态"
    )


def test_s15_leader_assign_uses_event_target_form() -> None:
    """S-15b 实机回归（2026-09-18 新局）：assign_leader = last_created_leader
    跨 country→fleet 不贴上 —— 连续 13 个月重复创建指挥官（舰队始终无领袖）。
    必须用原版标准形态：create_leader 内嵌 effect { save_event_target_as } +
    舰队里 assign_leader = event_target:（00_scripted_effects.txt L1003-1018 先例）。
    """
    t = _read(AUTONOMY)
    start = t.index("overmind_autonomy_create_commander = {")
    region = t[start:t.index("overmind_autonomy_disengage", start)]
    assert "save_event_target_as = om_new_commander" in region, (
        "S-15b 缺 save_event_target_as —— last_created_leader 跨 scope 实机不贴上"
    )
    assert "assign_leader = event_target:om_new_commander" in region, (
        "S-15b 赋任必须用 event_target 形态"
    )
    # 只查代码形态（制表符缩进行），注释里引用死形态做讲解是允许的
    assert "\n\t\tassign_leader = last_created_leader" not in region, (
        "S-15b 死形态回流：last_created_leader 跨 scope 赋任实机无效（13×重复创建教训）"
    )


def test_s15c_leaderless_check_restricted_to_military_fleets() -> None:
    """S-15c（2026-09-18 第二次重启后 27×循环仍续）：any_owned_fleet 包含科研船/
    工程船舰队 —— 它们永久无领袖（科学家另有任命机制），会驱动无限补指挥官。
    判定必须限定军用舰队：any_owned_ship = { is_ship_class = shipclass_military }
    （is_military_fleet 触发器在 4.4.6 不存在 —— 引擎触发器文档零命中；
    shipclass_military 语料先例 00_scripted_effects.txt:6109）。两处判定都要有。
    """
    t = _read(AUTONOMY)
    start = t.index("overmind_autonomy_manage_leaders = {")
    block = t[start:t.index("\n}", start)]
    assert block.count("any_owned_ship = { is_ship_class = shipclass_military }") == 2, (
        "S-15c 两处舰队判定（any/random）都必须限定军用舰船"
    )


def test_s17_outpost_survey_on_claim() -> None:
    """S-17 增强（2026-09-18 落地）：前哨拓土即顺手勘测新星系。

    脚本 create_starbase 不受勘测限制（两局实证，S-17 降级结论），
    但未勘测星系并入疆域让研究站/殖民语义不完整。原版形态 = 块式
    `set_surveyed = { surveyed = yes surveyor = <scope> }`
    （infernals_effects.txt:17 先例），必须在 system scope 内、跟
    create_starbase 同层（不加深嵌套）。
    """
    t = _read(AUTONOMY)
    start = t.index("overmind_expand_try_outpost = {")
    end = t.index("overmind_expand_try_colony = {", start)
    block = t[start:end]
    assert "set_surveyed = {" in block, "S-17 缺拓土勘测增强"
    assert "surveyed = yes" in block
    assert "surveyor = root" in block, "勘测者必须标注（root = 国家）"
    # 勘测必须写在前哨分支内（放殖民分支=作用域错，D-26 教训）
    assert block.index("set_surveyed") > block.index("random_neighbor_system"), (
        "S-17 勘测必须落在 neighbor_system scope 内"
    )


def test_s15d_prune_uses_idle_gate_and_triple_exemption() -> None:
    """S-15d 超编出清（2026-09-18 用户实测「招一堆指挥官把资源吃干了」）：
    只许解雇 is_idle（无岗位）的指挥官 —— 带舰队者非 idle，绝不误杀；
    内阁（is_councilor）与元首（is_ruler）双豁免；必须有定编门槛
    （count_owned_leader count > 2）防清空；kill_leader 须 show_notification = no。
    """
    t = _read(AUTONOMY)
    start = t.index("overmind_autonomy_prune_leaders = {")
    block = t[start:t.index("\n}", start)]
    assert "is_idle = yes" in block, "缺 is_idle 岗位门 —— 会误杀带舰队的指挥官"
    assert block.count("is_councilor = no") >= 2
    assert block.count("is_ruler = no") >= 2
    assert "count > 2" in block, "缺定编门槛"
    assert "show_notification = no" in block, "解雇须静默（不弹窗）"
    assert "kill_leader" in block


def test_s15_v3_quota_gate_and_class_policy() -> None:
    """S-15 v3（用户口径 2026-09-18：三职业都招、空缺即补、**别超上限**）：
    ① 招募共享一道定额门 count_owned_leader count < 6（引擎未暴露容量触发器，
    6 ≤ 基础容量下限，宁少勿超）；② 职业策略 = 指挥官→军用舰队、科学家→科研舰队
    （shipclass_science_ship，语料 00:7605）；③ event_target 按职业分名。
    """
    t = _read(AUTONOMY)
    start = t.index("overmind_autonomy_manage_leaders = {")
    block = t[start:t.index("\n}\n\novermind_autonomy_disengage", start)]
    assert "count_owned_leader" in block and "count < 6" in block, "缺定额门（用户红线：别超上限）"
    assert "shipclass_military" in block and "shipclass_science_ship" in block
    assert "om_new_commander" in block and "om_new_scientist" in block, "event_target 须按职业分名"
    # 全库只许招募这三类（用户口径 2026-09-19 扩大：所有岗位都要有人，含内阁；
    # S-15e 实验创建 official 验证自动补位）。只认 create_leader 的参数行
    for m in re.finditer(r"(?m)^\t+class = (\w+)$", t):
        assert m.group(1) in ("commander", "scientist", "official"), f"出现未授权职业：{m.group(1)}"
