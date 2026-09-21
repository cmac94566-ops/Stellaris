"""M4 capability checklist guards (C-01..C-15) + D1 empty-ruleset self-check.

依据 docs/M4能力清单与覆盖判据_2026-09-18.md §3（WDMSG-051 派工）：
每项能力一条守卫，判据 = 「代码路径存在」。分母锁定 15 项，分母变更须改本文档
并在 M4 文档留痕表登记。实机证（日志通道）由 WorkBuddy 侧采集，此处只锁代码证，
把覆盖率从人肉 grep 变成可回归指标。

附带锁定（2026-09-18 当日修法）：
  * D1 空规则集拒绝写盘（validate_payload + write_lex 双保险，WDMSG-042/044）
  * engage 重置回归：om_ships_built 旗标守护（一局一次）、om_era 改调 era_sync
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine import strategy_lex  # noqa: E402

MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"
AUTONOMY = MOD_DIR / "common/scripted_effects/overmind_autonomy.txt"
TRIGGERS = MOD_DIR / "common/scripted_triggers/overmind_autonomy_triggers.txt"
EVENTS = MOD_DIR / "events/overmind_autonomy_events.txt"
PROGRESSION = MOD_DIR / "common/scripted_effects/overmind_progression.txt"
ON_ACTIONS = sorted((MOD_DIR / "common/on_actions").glob("*.txt"))


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _autonomy() -> str:
    return _read(AUTONOMY)


# ---------------------------------------------------------------------------
# D1：空规则集拒绝写盘（WDMSG-042/044）
# ---------------------------------------------------------------------------

def _engage_block() -> str:
    t = _autonomy()
    blk = t[t.index("overmind_autonomy_engage = {"):]
    return blk[:blk.index("\n}", 1)]


def test_d1_validate_rejects_empty_ruleset():
    payload = strategy_lex.lex_from_context({"year": 2200}, rev=1)
    payload.rules = []
    problems = strategy_lex.validate_payload(payload)
    assert any("EMPTY" in p for p in problems), problems


def test_d1_write_lex_refuses_empty_ruleset(tmp_path):
    payload = strategy_lex.lex_from_context({"year": 2200}, rev=1)
    payload.rules = []
    with pytest.raises(ValueError, match="empty ruleset"):
        strategy_lex.write_lex(payload, tmp_path)


def test_d1_default_rules_nonempty_with_terminal_fallback():
    rules = strategy_lex.DEFAULT_RULES
    assert 1 <= len(rules) <= 12
    assert rules[-1].get("cond") == 99, "兜底规则必须在末尾"


# ---------------------------------------------------------------------------
# engage 重置回归（2026-09-18 D-34 侦查修法）
# ---------------------------------------------------------------------------

def test_engage_ship_counter_is_flag_guarded():
    engage = _engage_block()
    assert "NOT = { has_country_flag = om_ships_counter_init }" in engage, \
        "计数器初始化必须旗标守护（一局一次），否则每次载档重接都清零累计"


def test_engage_uses_era_sync_not_hardcoded_era1():
    engage = _engage_block()
    # 两步契约（2026-09-18）：字面初始化归 1（接管当年查得到年代，
    # WorkBuddy 契约锁定）+ 随后 era_sync 即时纠正（载档重接不再退回 era1）
    assert "overmind_autonomy_era_sync = yes" in engage, "engage 缺年代同步 —— 旧档重接后退回 era1"
    assert engage.index("set_variable = { which = om_era value = 1 }") < \
        engage.index("overmind_autonomy_era_sync = yes"), "era_sync 必须在字面初始化之后"


# ---------------------------------------------------------------------------
# M4 能力清单 C-01..C-15（判据 = 代码路径存在；锚点见 M4 文档 §3）
# ---------------------------------------------------------------------------

def test_capability_C01_heartbeat_takeover_exists():
    t = _autonomy()
    assert "overmind_autonomy_engage = {" in t
    assert "overmind_autonomy_disengage = {" in t
    assert "===== 主脑已接管帝国，自治层上线 =====" in t
    assert "已交还帝国控制权，自治层下线" in t
    assert any("overmind.302" in _read(p) for p in ON_ACTIONS), "302 自愈挂载缺失"


def test_capability_C02_attribution_guard_event_304():
    t = _read(EVENTS)
    assert "id = overmind.304" in t
    assert "【归因OK】" in t and "【告警】" in t
    assert "human_ai" in t


def test_capability_C03_monthly_tick_and_8_phases():
    t = _autonomy()
    assert "overmind_autonomy_tick = {" in t
    for i in range(1, 9):
        assert f"【相位 {i}/8】" in t, f"相位 {i} 入口日志缺失"


def test_capability_C04_rule_evaluator_and_dispatch():
    t = _autonomy()
    assert "overmind_autonomy_evaluate = {" in t
    assert "overmind_cond_check = {" in t
    for code in (1, 9, 11, 15, 16, 20):
        assert f"overmind_cond_{code} " in t, f"条件码 {code} 派发缺失"
    # 兜底 cond=99 走 else 分支（无命名触发器），锚点在求值器
    assert "兜底相位" in t or "本月零命中" in t, "零命中兜底锚点缺失"
    lex_effects = _read(MOD_DIR / "common/scripted_effects/overmind_lex.txt")
    m = [ln for ln in lex_effects.splitlines() if "om_lex_rule_count" in ln]
    assert m and "value = 0" not in m[0], "生成产物 rule_count=0（D1：过渡期已结束，静默失效）"


def test_capability_C05_gain_ledger_and_breakers():
    t = _autonomy()
    for p in range(1, 9):
        assert f"om_lex_gain_p{p}" in t, f"相位 {p} 收益记账缺失"
    assert t.count("【熔断】") >= 8, "8 相位各需一道熔断门"


def test_capability_C06_slot_rotation_fallback():
    t = _autonomy()
    assert "overmind_autonomy_run_slot = {" in t
    assert "日程格位越界" in t


def test_capability_C07_era_sync_annual_and_threat():
    t = _autonomy()
    assert "overmind_autonomy_era_sync = {" in t
    assert "overmind_autonomy_annual = {" in t
    assert "overmind_evaluate_threat = {" in t
    assert "【威胁评估】" in t
    sync = t[t.index("overmind_autonomy_era_sync = {"):]
    sync = sync[:sync.index("\n}", 1)]
    for threshold in (150, 75, 30, 10):
        assert f"years_passed >= {threshold}" in sync, f"era 阶梯缺 {threshold}"


def test_capability_C08_resource_gates_fullcap_posture():
    t = _autonomy()
    assert t.count("resource_stockpile_percent") >= 4, "满仓抑制门应 ≥4 道"
    # D-24/D-28 教训：满仓门是国家 scope 触发器，星球 scope 下必须 owner 包回（同行形态）
    assert "owner = { resource_stockpile_percent" in t, "满仓门必须 owner 包回国家 scope"
    assert "overmind_autonomy_posture = {" in t
    assert "economic_policy_civilian" in t
    assert "trade_policy_consumer_goods" in t


def test_capability_C09_whitelist_invest():
    t = _autonomy()
    assert "overmind_autonomy_invest = {" in t
    for anchor in ("【白名单·生存期】", "【白名单·产能期】", "【白名单·得分期】"):
        assert anchor in t, f"白名单日志锚点缺失：{anchor}"


def test_capability_C10_expand_and_colonize():
    t = _autonomy()
    assert "overmind_autonomy_expand = {" in t
    for branch in ("overmind_expand_try_mining", "overmind_expand_try_research",
                   "overmind_expand_try_outpost", "overmind_expand_try_colony"):
        assert f"{branch} = {{" in t, f"扩张分支缺失：{branch}"


def test_capability_C11_traditions_and_perks():
    t = _autonomy()
    assert "overmind_advance_tradition_by_era = {" in t
    assert "overmind_pick_ascension_perk = {" in t
    p = _read(PROGRESSION)
    for chain in ("overmind_advance_tradition_tree = {",
                  "overmind_advance_tradition_node = {",
                  "overmind_grant_ascension_perk = {"):
        assert chain in p, f"progression 链缺失：{chain}"


def test_capability_C12_council_agenda():
    t = _autonomy()
    assert "overmind_autonomy_agenda = {" in t
    assert t.count("【议程】") >= 3


def test_capability_C13_situation_management():
    t = _autonomy()
    assert "overmind_autonomy_situations = {" in t
    assert t.count("【局势】") >= 16, "8 局势 × 进入/脱离 两组日志锚点"


def test_capability_C14_war_management():
    t = _autonomy()
    assert "overmind_autonomy_war_management = {" in t
    assert "【止战】" in t
    assert "end_war_effect" in t


def test_capability_C15_starbase_modules():
    t = _autonomy()
    # V-6b（D-48 转案，2026-09-19）：模块 id = 原版本名 anchorage/trading_hub
    # （M4 文档 C-15 误记 starbase_* 检索词 —— 已订正）。V-6c 证伪：outpost 无模块槽。
    assert "module = anchorage" in t
    assert "module = trading_hub" in t
    assert "【锚地】" in t
    assert "【贸易枢纽】" in t
    assert "has_starbase_module = shipyard" in t, "船坞保护门（V-6 硬底线）缺失"
    # V-6b 链路：升堡（outpost → starport）→ 装模块；容量门 + 扣费前置（AC-4′/AC-5′）
    assert "set_starbase_size = starbase_starport" in t, "缺升堡步骤（V-6c 证伪）"
    assert "used_starbase_capacity_percent < 1" in t, "缺容量门（AC-4′，D-49 percent 形态）"
    assert t.count("owner = { add_resource = { alloys = -150 } }") >= 2, \
        "扣费必须前置在写入条件内（D-48：4850 合金泄漏教训）"


def test_d36_no_empty_if_guards() -> None:
    """D-36 回归（2026-09-18）：`if = { limit = {...} }` 块内无体 = 守卫丢失
    （引擎报 Empty if/else_if，槽位块用上月陈旧值求值 → 同月重复熔断）。
    守卫与动作必须同块：合并写法（深度不变）或移入体内。
    """
    import re as _re
    t = _autonomy()
    # limit 内允许一层嵌套花括号（check_variable = {...}）；行尾即闭 = 空体。
    # 已注入反向验证：旧空守卫形态命中 1、合法带体行命中 0。
    empty = _re.findall(r"(?m)^\t*if = \{ limit = \{(?:[^{}]|\{[^{}]*\})*\} \}$", t)
    assert not empty, f"空守卫 if 回流（{len(empty)} 处，首处：{empty[0][:80] if empty else ''}）"


def test_s13_agenda_boost_uses_engine_documented_scale() -> None:
    """S-13 推进分支（2026-09-18）：add_council_agenda_progress_percent 的引擎文档
    取值域是 **-1.0..1.0 浮点**（script_documentation/effects.log:3139），
    不是百分数 —— 写成 10 会越界。推进必须有且取值落在文档域内。
    可见性（WDMSG-066 P0-A）：心跳每 6 次推进一行日志，不许 once 后永续静默。
    """
    import re as _re
    t = _autonomy()
    start = t.index("overmind_autonomy_agenda = {")
    blk = t[start:t.index("\n}", start)]
    assert "add_council_agenda_progress_percent" in blk, "S-13 缺推进分支"
    m = _re.search(r"add_council_agenda_progress_percent = ([0-9.]+)", blk)
    assert m, "推进量缺失"
    v = float(m.group(1))
    assert 0 < v <= 1.0, f"推进量 {v} 越界（引擎文档域 -1.0..1.0）"
    assert "om_agenda_boost_n" in blk and "【议程】 心跳" in blk, \
        "缺推进心跳 —— once 旗标后用户看不见议会在动（可见性缺陷回归）"
    assert "om_agenda_boost_logged" not in blk, "once 旗标残留（可见性缺陷形态）"


def test_s20_dedup_predicate_not_inverted() -> None:
    """WDMSG-068 缺陷B 回归：原 NOR 三连实际语义 = 「新相位被跳过、旧相位放行」
    （rule11 act7 被谎报同相位跳过 140 次）。正确判据 = OR(AND(count>=N, exec_N==act))
    —— 只有真同相才去重。回填文案与截断文案分离。
    """
    t = _autonomy()
    start = t.index("overmind_autonomy_evaluate = {")
    end = t.index("overmind_autonomy_prune_leaders = {", start) if \
        "overmind_autonomy_prune_leaders = {" in t[start:] else len(t)
    region = t[start:]
    assert "NOR = {" not in region, "去重 NOR 反转形态回流"
    assert region.count("om_exec_count value >= 1") >= 12, "OR(AND) 同相判定缺失"
    assert "同相位，去重跳过" in region
    assert "执行集已满" in region, "截断文案未更新（须与同相位区分）"


def test_s20_backfill_covers_unscheduled_phases() -> None:
    """WDMSG-068 缺陷A 回归：规则槽 ≤3 会把 2/3/4/5/7 挤出执行集
    （建设冻结 / traditions 24 年 0 / 熔断噪音 三象一因）。
    规则模式分支必须有回填：未被规则槽覆盖的相位按 om_bf_phase 补跑。
    ⚠️ 回填区禁止 streak 熔断门 —— 未执行相位 streak 永不重置，熔断门 = 活锁
    （2026-09-18 实测：2/4/7 被陈旧 streak 永久拦截，回填只跑了 3/5）。
    """
    t = _autonomy()
    # 双通道（2026-09-19）：指针通道 8 + 探测通道 8 = 16 处 run_slot
    assert t.count("SLOT = om_bf_phase") == 16, "回填必须覆盖 8 相位 × 双通道"
    assert "om_bf_phase value = 8" in t
    bf_start = t.index("om_bf_phase value = 1 }")
    bf_end = t.index('【回填】相位 8', bf_start)
    bf = t[bf_start:bf_end]
    assert "_streak" not in bf, "回填区出现 streak 门 = 活锁（未执行相位永不重置）"


def test_d40_breaker_has_halfopen_probe_bypass() -> None:
    """D-40/R2 回归：三槽 × 8 相位的熔断门必须带半开探测旁路
    （NOT om_probe_rot = P-1），否则 streak 冻死 = 永久熔断（实测 240 月不变）。
    """
    t = _autonomy()
    import re as _re
    bypass = _re.findall(
        r"NOT = \{ check_variable = \{ which = om_probe_rot value = \d+ \} \}", t
    )
    assert len(bypass) == 24, f"探测旁路应 24 处（3 槽 × 8 相位），实 {len(bypass)}"


def test_d40b_bookkeeping_per_slot_gated() -> None:
    """D-40b 回归：streak 累加句必须收进同槽位门（exec_N = P）下 ——
    原无门控形态（4 缩进单行 NOT-gain + change）会让任一槽执行污染全部 8 相位 streak。
    """
    t = _autonomy()
    import re as _re
    stray = _re.findall(
        r"(?m)^\t{4}if = \{ limit = \{ NOT = \{ check_variable = \{ "
        r"which = om_lex_gain_p\d value >= 1 \} \} \} change_variable",
        t,
    )
    assert not stray, f"无门控累加句回流 {len(stray)} 处"


def test_d40_r0_engage_resets_stale_streaks() -> None:
    """D-40/R0 回归：engage 必须全量归零 8 个 streak（存档携带 222 冻结 20 年入局）。"""
    t = _autonomy()
    engage = t[t.index("overmind_autonomy_engage = {"):]
    engage = engage[:engage.index("\n}", 1)]
    for p in range(1, 9):
        assert f"om_lex_gain_p{p}_streak value = 0" in engage, f"engage 缺 streak {p} 归零"


def test_d40_summary_window_and_backfill_throttle() -> None:
    """D-40/R4+R5 回归：rot=11 月打熔断汇总（8 相位条件行）；
    回填双通道结构（2026-09-19 终版）：每相位 = 指针通道块 + 指针推进行 + 探测通道块；
    初始化含越界自愈（存档可能携带旧哨兵 9）。禁哨兵覆写形态。
    """
    t = _autonomy()
    assert "om_probe_rot value = 11" in t
    assert t.count("【熔断汇总】") == 8
    assert "om_bf_phase value = 9" not in t, "哨兵覆写形态回流（实测 5/月）"
    # 双通道：每相位 指针块 + 指针推进行 + 探测块
    assert t.count("# 指针通道 P") == 8
    assert t.count("# 探测通道 P") == 8
    for p in range(1, 8):
        assert f"if = {{ limit = {{ check_variable = {{ which = om_bf_phase value = {p} }} }} set_variable = {{ which = om_bf_phase value = {p + 1} }} }}" in t, \
            f"相位 {p} 缺指针推进行"
    assert "value = 9" not in t.split("# 指针通道 P1")[1], "指针区残留哨兵值"
    # 探测解除：8 相位各有 探测月放行 + 解除行
    assert t.count("回填探测有收益，恢复服务") == 8
    # 无月度重置（只有初始化/越界守卫）—— 月度重置会让指针永远停在第一个未覆盖相位；
    # 初始化必须含越界自愈（存档可能携带旧哨兵 9）
    assert "om_bf_phase value >= 9" in t

def test_d28_cg_deficit_factory_branch() -> None:
    """消费品赤字优先分支（2026-09-19，网络方案①落地）：CG 赤字时民用工厂区
    最高优先 —— zone_factory（城市区，价 1000 矿）走 afford/charge 对；
    触发 = has_deficit = consumer_goods（引擎在册触发器）。"""
    t = _autonomy()
    assert "has_deficit = consumer_goods" in t, "缺 CG 赤字触发"
    assert "overmind_afford_zone_zone_factory = yes" in t
    assert "overmind_charge_zone_zone_factory = yes" in t
    assert "zone = zone_factory" in t
    assert "【消费品】" in t, "缺分支日志锚点"

def test_d45_rare_resource_deficit_bypass() -> None:
    """D-45（2026-09-19 用户指令：全资源短缺需有效方案）：稀有三区原 era3+ 门槛
    使早期冶金师吃气体/微粒时无产能响应。赤字必须 bypass 时代门
    （has_deficit 立即响应），常规库存规则（era3+ 且 <200）保留。
    """
    t = _autonomy()
    for res in ("rare_crystals", "volatile_motes", "exotic_gases"):
        assert f"has_deficit = {res}" in t, f"{res} 缺赤字 bypass"
    assert t.count("era3 门槛曾使早期赤字无产能响应") == 3
