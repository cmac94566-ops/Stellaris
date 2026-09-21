"""Extract the four M1 acceptance metrics from a Stellaris save.

``docs/SRS.md`` AC-4 requires the empire's **districts / buildings / fleets /
traditions** to grow monotonically over ten unattended game years.  This module
turns a ``.sav`` into those numbers.

Where each number comes from (verified against the real 2211 save, 82 MB
gamestate, not guessed):

======================================  ==========================================
metric                                  source
======================================  ==========================================
player country id                       ``player = { { country = N } }``
districts                               sum of ``len(colony.<id>.districts)``
buildings                               sum of ``len(colony.<id>.buildings_cache)``
zones                                   non-null ids in ``districts.<id>.zones``
fleet size                              ``country.<N>.fleet_size``
traditions                              quoted entries in ``country.<N>.traditions``
======================================  ==========================================

Two encoding details matter and both cost an hour to rediscover:

* the file is **not** uniformly indented.  ``colony=`` puts its children at one
  tab and their fields at two; ``planets=``/``planet=`` nests one level deeper.
  A window search with the wrong indent silently reads the *planets* array
  instead — which is why the block reader below brace-matches from an anchor
  rather than trusting a fixed slice.
* districts and buildings are **global arrays** (``districts=``, ``buildings=``)
  with per-colony id lists pointing into them.  The arrays themselves carry no
  owner field, so ownership has to come from the colony lists.

Usage::

    py -3.12 scripts/overmind_metrics.py <save.sav>
    py -3.12 scripts/overmind_metrics.py --newest
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.toml"

NULL_ID = 4294967295


# ---------------------------------------------------------------------------
# Clausewitz text helpers
# ---------------------------------------------------------------------------
def block_at(text: str, brace: int) -> str:
    """Return the balanced ``{...}`` starting at ``brace`` (quote-aware)."""
    depth = 0
    i = brace
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
        i += 1
    return text[brace:]


def child_block(text: str, key: str, *, tabs: int = 2, start: int = 0) -> str | None:
    """Block of ``\\n<tabs><key>=\\n<tabs>{``, or ``None``."""
    pad = "\t" * tabs
    m = re.search(rf"\n{pad}{re.escape(key)}=\n{pad}(\{{)", text[start:])
    if not m:
        return None
    return block_at(text, start + m.start(1))


def int_list(text: str, key: str, *, tabs: int = 2) -> list[int]:
    """Parse ``key = { 1 2 3 }`` into ``[1, 2, 3]``."""
    body = child_block(text, key, tabs=tabs)
    if body is None:
        return []
    return [int(x) for x in re.findall(r"-?\d+", body)]


def quoted_list(text: str, key: str, *, tabs: int = 2) -> list[str]:
    body = child_block(text, key, tabs=tabs)
    if body is None:
        return []
    return re.findall(r'"([^"]+)"', body)


def scalar(text: str, key: str, *, tabs: int = 2) -> str | None:
    pad = "\t" * tabs
    m = re.search(rf"\n{pad}{re.escape(key)}=([^\n]*)", text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Save reading
# ---------------------------------------------------------------------------
def gamestate_of(save: Path) -> str:
    with zipfile.ZipFile(save) as z:
        return z.read("gamestate").decode("utf-8", "replace")


def player_country(text: str) -> int:
    m = re.search(r"\nplayer=\n\{\n(.*?)\n\}", text, re.S)
    if not m:
        raise ValueError("gamestate 里找不到 player 块")
    c = re.search(r"country=(\d+)", m.group(1))
    if not c:
        raise ValueError("player 块里没有 country=")
    return int(c.group(1))


def overmind_country(text: str) -> int | None:
    """按自治层标记定位被主脑接管的帝国（R-3，三席评审红队#2）。

    不再只认 ``player = { country = N }``：``play <id>`` 切换扮演对象、或
    观察者模式下，player 字段会指向别的国家，而 ``overmind_autonomy``
    flag 永远跟着被接管的帝国走。

    返回 None 表示存档里没有任何国家带该标记（旧存档 / 自治层未上线），
    调用方应回退到 ``player_country``。
    """
    anchor = text.find("\ncountry=\n{\n")
    if anchor == -1:
        return None
    countries = block_at(text, anchor + len("\ncountry="))
    inner = countries[1:-1]
    # 精确到 flag 名末尾：必须带 lookahead，否则 "overmind_autonomy_paused"
    # （已被玩家手动叫停的帝国）会被子串匹配误判成接管中 —— QA 复审抓的洞。
    flagged = re.compile(r"overmind_autonomy(?![A-Za-z0-9_])")
    for m in re.finditer(r"\n\t(\d{1,4})=\n\t\{", inner):
        blk = block_at(inner, inner.index("{", m.start()))
        if flagged.search(blk):
            return int(m.group(1))
    return None


def resolve_country(text: str) -> int:
    """自治标记优先，player 字段兜底。"""
    cid = overmind_country(text)
    return player_country(text) if cid is None else cid


def country_block(text: str, cid: int) -> str:
    anchor = text.index("\ncountry=\n{\n")
    m = re.search(rf"\n\t{cid}=\n\t(\{{)", text[anchor:])
    if not m:
        raise ValueError(f"找不到国家 {cid} 的块")
    return block_at(text, anchor + m.start(1))


def colony_blocks(text: str) -> tuple[str, dict[int, int]]:
    """``(colony_block_text, {colony id: offset within it})``."""
    anchor = text.index("\ncolony=\n{\n")
    body = block_at(text, anchor + len("\ncolony=\n"))
    index: dict[int, int] = {}
    for m in re.finditer(r"\n\t(\d+)=\n\t\{", body):
        index[int(m.group(1))] = m.start()
    return body, index


def district_zones(text: str) -> dict[int, int]:
    """``{district id: number of populated zone slots}``.

    Zones are the 4.4.6 replacement for specialist districts, so this is the
    most direct evidence that the JOBS phase did something.  An unpopulated slot
    is stored as ``4294967295`` and must not be counted.

    The zone list holds no nested braces, so it is read with a brace-free
    pattern.  An earlier version anchored on the surrounding indentation, which
    silently returned zero for every district — the capture group starts *after*
    a newline, so a pattern demanding ``\\n`` in front could never match.
    """
    anchor = text.index("\ndistricts=\n{\n")
    body = block_at(text, anchor + len("\ndistricts=\n"))
    out: dict[int, int] = {}
    for m in re.finditer(r"\n\t(\d+)=\n\t\{\n((?:.|\n)*?)\n\t\}", body):
        z = re.search(r"zones=\s*\n?\s*\{([^{}]*)\}", m.group(2))
        ids = [int(x) for x in re.findall(r"\d+", z.group(1))] if z else []
        out[int(m.group(1))] = sum(1 for i in ids if i != NULL_ID)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def read_stockpiles(country_text: str) -> dict:
    """读国家库存（D-22 资源曲线用）。

    结构（实证，2242 存档）：``standard_economy_module=`` → ``resources=`` →
    ``energy=… minerals=… alloys=…``。注意 ``resources=`` 与 ``{`` 之间有换行，
    不能用紧邻匹配。缺失资源（如 food 为 0 时不落盘）直接不出现在结果里。
    """
    m = re.search(r"standard_economy_module\s*=\s*\{", country_text)
    if not m:
        return {}
    # 该模块块内找 resources
    blk = block_at(country_text, country_text.index("{", m.start()))
    r = re.search(r"resources\s*=\s*\{", blk)
    if not r:
        return {}
    res_blk = block_at(blk, blk.index("{", r.start()))
    out: dict[str, float] = {}
    for line in res_blk.splitlines():
        mm = re.match(r"\s*([a-z_]+)\s*=\s*(-?[\d.]+)\s*$", line)
        if mm:
            out[mm.group(1)] = float(mm.group(2))
    return out


def extract(save: Path) -> dict:
    text = gamestate_of(save)
    cid = resolve_country(text)
    country = country_block(text, cid)

    colonies, index = colony_blocks(text)
    owned_colonies = int_list(country, "controlled_colonies") or int_list(
        country, "owned_planets"
    )

    districts = buildings = 0
    owned_district_ids: list[int] = []
    for col in owned_colonies:
        off = index.get(col)
        if off is None:
            continue
        blk = block_at(colonies, colonies.index("{", off))
        ids = int_list(blk, "districts")
        districts += len(ids)
        owned_district_ids.extend(ids)
        buildings += len(int_list(blk, "buildings_cache"))

    zones_by_id = district_zones(text)
    zones = sum(zones_by_id.get(d, 0) for d in owned_district_ids)

    date = re.search(r'\ndate="([^"]+)"', text)
    return {
        "save": save.name,
        "date": date.group(1) if date else "?",
        "year": int(date.group(1).split(".")[0]) if date else 0,
        "country": cid,
        "colonies": len(owned_colonies),
        "districts": districts,
        "buildings": buildings,
        "zones": zones,
        "fleet_size": int(scalar(country, "fleet_size") or 0),
        "military_power": float(scalar(country, "military_power") or 0.0),
        "traditions": len(quoted_list(country, "traditions")),
        "tradition_categories": len(quoted_list(country, "tradition_categories")),
        "pops": int(scalar(country, "num_sapient_pops") or 0),
        "empire_size": int(scalar(country, "empire_size") or 0),
        "resources": read_stockpiles(country),
    }


#: The four AC-4 metrics, plus the extras worth watching.
TRACKED: tuple[str, ...] = (
    "districts", "buildings", "zones", "fleet_size", "traditions",
    "tradition_categories", "colonies", "pops", "empire_size",
)

#: AC-4 明文要求的四项（SRS 里写的是 districts / buildings / fleets / traditions）。
#: ``zones`` 是 4.4.6 用专业区取代专家区划之后的实际等价物，一并纳入。
M1_9_KEYS: tuple[str, ...] = (
    "districts", "buildings", "zones", "fleet_size", "traditions",
)


# ---------------------------------------------------------------------------
# 自治层自身状态（从存档读，不依赖日志）
# ---------------------------------------------------------------------------
#: 存档里的 ``om_*`` 变量与标记名。
#:
#: **必须加负向后视** —— 存档是裸文本，``custom_made`` 里含 ``om_made``、
#: ``galcom_vote`` 里含 ``om_vote``、``random_log_day`` 里含 ``om_log_day``。
#: 第一版用 ``\bom_[a-z_]+`` 抓出 9 个假阳性，把标识符数从 18 虚报成 27。
OM_VAR_RE = re.compile(r"(?<![A-Za-z0-9_])(om_[a-z_0-9]+)\s*=\s*(-?\d+)")

#: 八个格位的顺序与相位名的对应，与 ``overmind_autonomy_run_slot`` 一致。
SLOT_PHASES: tuple[str, ...] = (
    "relief", "housing", "jobs", "amenities",
    "science", "military", "ascension", "war",
)


def probe_overmind(save: Path) -> dict:
    """读自治层自己在存档里留下的状态。

    这条通道**不依赖日志**，所以脚本改动无需重启游戏就能取证 —— 对
    "Clausewitz 不热重载脚本" 这个约束来说，它是唯一能观测运行中进程内部
    状态的办法。

    能读出三类东西：

    ``om_month``
        轮转计数器（1..8）。**判据：它必须在两次年度存档之间变化。**
        每年 +12 个月、mod 8 = +4，所以连续年存档应呈 ``4 → 8 → 4 → 8``。
        若多年停在同一个值，说明 monthly pulse 没在跑（游戏暂停 / on_action 断了）。

    ``om_lex_*``
        编译后的法案。``om_lex_slot1..8`` 就是日程本身；``stance`` /
        ``threat`` / ``tradition_mode`` / ``ap_mode`` / ``bottleneck`` /
        ``focus_zone`` 是各相位的方向位。它们**多年不变**说明法案编译可能
        只在开局跑了一次（目前确实是设计如此，见 M2）。

    ``om_did_*``
        动作标记，**每月初由 tick 全部清零**（见 ``overmind_autonomy.txt``
        的 ``remove_country_flag`` 段）。所以存档里的标记只反映**当月**
        发生了什么 —— 这是"这个相位到底动没动手"的最直接证据。

        但读的时候必须配 ``om_month`` 一起看，否则会得出错误结论：
        1 月的存档里 ``om_month`` 只会是 4 或 8，所以 ``om_did_tradition``
        （slot 7 置位）**不可能出现**，它的缺席不代表传统相位坏了。
    """
    text = gamestate_of(save)
    cid = resolve_country(text)
    country = country_block(text, cid)

    variables: dict[str, int] = {}
    var_block = child_block(country, "variables", tabs=2)
    if var_block:
        for m in OM_VAR_RE.finditer(var_block):
            variables[m.group(1)] = int(m.group(2))

    # 标记不在 variables 块里，它们混在 has_* 那一片国家标志位中。
    flags = {
        m.group(1): int(m.group(2))
        for m in OM_VAR_RE.finditer(country)
        if m.group(1).startswith("om_did_")
    }

    agenda = [
        variables.get(f"om_lex_slot{i}") for i in range(1, 9)
    ]
    month = variables.get("om_month")

    return {
        "save": save.name,
        "country": cid,
        "om_month": month,
        "slot_phase": (
            SLOT_PHASES[month - 1] if month and 1 <= month <= 8 else None
        ),
        "agenda": agenda,
        "agenda_str": (
            "".join(str(x) if x is not None else "?" for x in agenda)
        ),
        "lex": {
            k: v for k, v in sorted(variables.items())
            if k.startswith("om_lex_") and not k.startswith("om_lex_slot")
        },
        "did_flags": sorted(flags),
        "variables": variables,
        # R-2：建舰累计计数 —— fleet_size 之外的第二证据源。
        # 缺失说明该存档早于计数器引入，或自治层从未建舰。
        "ships_built": variables.get("om_ships_built"),
    }


#: 自治层变量里，跨存档变化才有意义的那几个。
#:
#: ``om_lex_*`` 与 ``om_month`` 之外都**不应该**变 —— 每次变化都意味着
#: 法案被重新编译，属于值得在趋势表里显形的异常。
OM_TRACKED_VARS: tuple[str, ...] = (
    "om_month", "om_lex_rev", "om_lex_stance", "om_lex_threat",
    "om_lex_bottleneck", "om_lex_focus_zone", "om_lex_tradition_mode",
    "om_lex_ap_mode",
)


def iter_saves(directory: Path) -> list[Path]:
    """目录下所有 ``.sav``，按 (游戏日期, 文件时间) 排序。

    只按文件时间排是**不够的** —— 同一批存档可能是从别处拷回来的，
    时间戳会全部相同（本机就出现过）。所以主序取存档内记录的 ``date``。
    """
    found = [Path(p) for p in glob.glob(str(directory / "**" / "*.sav"), recursive=True)]
    out: list[tuple[tuple[int, int, int], float, Path]] = []
    for p in found:
        try:
            with zipfile.ZipFile(p) as z:
                head = z.read("meta").decode("utf-8", "replace")[:4000]
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
        m = re.search(r'date="(\d+)\.(\d+)\.(\d+)"', head)
        key = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
        out.append((key, os.path.getmtime(p), p))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


def monotonic_report(rows: list[dict], keys: tuple[str, ...] = M1_9_KEYS) -> dict:
    """逐指标判定「是否单调不减」，并给出证据。

    返回 ``{指标: {"series": [...], "monotonic": bool, "first": x, "last": y,
    "delta": y-x, "breaks": [下标]}}``。

    ``monotonic`` 用**不减**（``>=``）而非**严格递增** —— 十进制存档
    里某些指标会在同一年里被重复统计两次（手动存档 + 自动存档），
    严格递增会把这种重复误判成回归。
    """
    report: dict[str, dict] = {}
    for k in keys:
        series = [r[k] for r in rows]
        breaks = [
            i for i in range(1, len(series)) if series[i] < series[i - 1]
        ]
        report[k] = {
            "series": series,
            "monotonic": not breaks,
            "flat": len(set(series)) <= 1,
            "first": series[0] if series else None,
            "last": series[-1] if series else None,
            "delta": (series[-1] - series[0]) if series else None,
            "breaks": breaks,
        }
    return report


def sustained_declines(series: list, min_run: int = 3) -> list[int]:
    """找出「持续下降段」：连续 >= min_run 个采样点逐点下降的起点下标。

    D-13 决策 A 后，孤立的单点下降（同一年手动+自动存档重复统计、
    战争期舰队损耗等）不再判不通过；只有持续萎缩才判。
    """
    runs: list[int] = []
    i = 1
    while i < len(series):
        if series[i] < series[i - 1]:
            j = i
            while j < len(series) and series[j] < series[j - 1]:
                j += 1
            if j - i + 1 >= min_run:
                runs.append(i - 1)
            i = j
        else:
            i += 1
    return runs


# ---------------------------------------------------------------------------
# AC-4 v3（D-27 活跃度门槛，2026-09-17）：规格 docs/AC-4v3活跃度门槛规格_D-27.md
# v2 的假阳性：只查「不降」，"完全停摆"同样满足 —— 而实机 2207-2211 五项全冻结
# 却判「未失败」。v3 补两道门：门1 活跃度（可判死）/ 门2 效率（只标注）。
# ---------------------------------------------------------------------------

#: 门1 核心指标集 —— pops/colonies 故意不纳入：停摆局 pops 仍在涨（5347→5413），
#: 纳入会掩盖停摆；colonies 易受战争波动（规格 §5-4）。
AC4_V3_CORE: tuple[str, ...] = (
    "districts", "buildings", "zones", "fleet_size", "traditions",
)
AC4_V3_MIN_WINDOW = 5      # 年：窗口不足只报「样本不足」
AC4_V3_MIN_GAIN = 2        # 单项达标下限：净增长 >= max(2, 初值*5%)
AC4_V3_GAIN_RATIO = 0.05


def activity_gate(report: dict) -> dict:
    """AC-4 v3 门1（可判死）：核心指标集内 >=1 项「实质增长」才算活着。

    实质增长 = 净增长 >= max(2, 初值*5%)（下限排单点波动，百分比适配晚期
    大帝国 —— 阈值经四局实测校准，见规格 §1 表）。
    """
    passed: list[str] = []
    details: dict = {}
    for key in AC4_V3_CORE:
        v = report.get(key)
        if not v or v["delta"] is None or v["first"] is None:
            continue
        threshold = max(AC4_V3_MIN_GAIN, v["first"] * AC4_V3_GAIN_RATIO)
        ok = v["delta"] >= threshold
        details[key] = {
            "net": v["delta"], "threshold": round(threshold, 2), "passed": ok,
        }
        if ok:
            passed.append(key)
    return {
        "passed": bool(passed),
        "passed_count": len(passed),
        "passed_keys": passed,
        "details": details,
    }


def efficiency_note(rows: list[dict]) -> dict | None:
    """AC-4 v3 门2（只标注）：energy/food 长期满仓 ⇒ 「⚠️ 效率：资源溢出」。

    判据（规格 §1 门2）：>=80% 采样点处于 >=90% 存储上限。存储上限的精确
    解析需更深的存档字段（D-27 遗留待办）；v1 用「窗口平台」代理 ——
    采样值 >= 0.95*窗口峰值 且 峰值 >= 5000 视为处于满仓平台（实测满仓即
    恒定 25000/15000 平台，代理与规格判据在实测数据上等价）。
    """
    out: dict = {}
    for res in ("energy", "food"):
        series = [
            r.get("resources", {}).get(res)
            for r in rows
            if r.get("resources", {}).get(res) is not None
        ]
        if len(series) < 3:
            continue
        peak = max(series)
        if peak < 5000:
            continue
        at_cap = sum(1 for s in series if s >= peak * 0.95)
        ratio = round(at_cap / len(series), 2)
        if ratio >= 0.8:
            out[res] = {"at_cap_ratio": ratio, "peak": peak}
    return out or None


def ac4_verdict_v3(rows: list[dict]) -> dict:
    """AC-4 v3 单局判定（D-27 规格 §2 流程）：rows 按 date/year 升序。

    流程：样本门槛（窗口 <5 年 → 样本不足）→ v2 三条 → 门1 活跃度 →
    门2 效率标注。verdict ∈ {通过, 停滞·不通过, 下降·不通过, 样本不足}。
    """
    if not rows:
        return {"verdict": "样本不足", "reason": "无采样"}
    years = sorted({r["year"] for r in rows if r.get("year")})
    if years and (years[-1] - years[0] + 1) < AC4_V3_MIN_WINDOW:
        return {
            "verdict": "样本不足",
            "reason": f"窗口仅 {years[-1] - years[0] + 1} 年（< {AC4_V3_MIN_WINDOW}）",
            "years": [years[0], years[-1]],
        }
    report = monotonic_report(rows, AC4_V3_CORE)
    v2 = ac4_verdict(report)
    activity = activity_gate(report)
    # 规格流程顺序：③ v2 三条在前（下降判死）→ ④ 门1 活跃度（停滞判死）。
    if not v2["overall"]:
        verdict = "下降·不通过"
    elif not activity["passed"]:
        verdict = "停滞·不通过"
    else:
        verdict = "通过"
    return {
        "verdict": verdict,
        "v2": v2,
        "activity": activity,
        "efficiency": efficiency_note(rows),
        "window": [years[0], years[-1]] if years else [],
    }


def ac4_v3_grouped(grouped_rows: dict[str, list[dict]]) -> dict[str, dict]:
    """按局分组判定（规格 §2-① 铁律：跨局不可混算）。

    ``grouped_rows``: {局标识(帝国ID/目录名): rows 升序} → 每局独立 v3 判定。
    """
    return {game: ac4_verdict_v3(rows) for game, rows in grouped_rows.items()}


def ac4_verdict(report: dict) -> dict:
    """AC-4 v2 判定（D-13 决策 A，2026-09-15）。

    v1「单调增长」与反应式治理结构性冲突：全部建造动作都挂在
    「出问题才补」上，健康帝国（库存增长、无赤字）本就不该继续
    铺区划 —— v1 会把"治理得好"判成"不通过"。

    v2 三条：
      ① 终值 >= 初值（净下降 = 不通过）；
      ② 无 >= 3 个连续采样点的持续下降段（单点波动放行）；
      ③ 冻结（flat）只标「注意」，不再自动不通过 —— 舰队冻结由
         om_ships_built 累计计数交叉确证（R-2）；区划/建筑冻结在
         健康帝国是反应式设计的预期行为。

    返回 ``{"keys": {指标: {"verdict", "note"}}, "overall": bool}``。
    """
    keys: dict[str, dict] = {}
    for k, v in report.items():
        if v["delta"] is not None and v["delta"] < 0:
            keys[k] = {"verdict": "不通过", "note": f"净下降 {v['delta']}"}
            continue
        runs = sustained_declines(v["series"])
        if runs:
            keys[k] = {"verdict": "不通过", "note": f"位置 {runs} 起持续下降"}
            continue
        if v["flat"]:
            keys[k] = {"verdict": "注意", "note": f"冻结：全程恒为 {v['first']}"}
            continue
        keys[k] = {"verdict": "通过", "note": f"+{v['delta']}"}
    overall = not any(x["verdict"] == "不通过" for x in keys.values())
    return {"keys": keys, "overall": overall}


def all_scores(text: str) -> list[dict]:
    """逐国解析分数档案（V-5 银河分数榜）。

    刻意**不复用** ``country_block``：实测它对 id=0 返回 436 KB 的块，
    边界可疑。这里与国家定位同款做法——先在 ``country=`` 块内按
    ``\n\t<id>=\n\t{`` 逐国定位，再对每块单独做括号配对，
    保证读数归属不串国。
    """
    anchor = text.find("\ncountry=\n{\n")
    if anchor == -1:
        return []
    countries = block_at(text, anchor + len("\ncountry="))
    inner = countries[1:-1]
    out: list[dict] = []
    keys = (
        "victory_rank", "victory_score", "economy_power",
        "tech_power", "military_power", "num_sapient_pops", "empire_size",
    )
    for m in re.finditer(r"\n\t(\d{1,4})=\n\t\{", inner):
        blk = block_at(inner, inner.index("{", m.start()))
        rec: dict = {"id": int(m.group(1))}
        for k in keys:
            mm = re.search(r"(?<![A-Za-z0-9_])" + k + r"=([\d.]+)", blk)
            rec[k] = float(mm.group(1)) if mm else None
        rec["overmind"] = bool(
            re.search(r"overmind_autonomy(?![A-Za-z0-9_])", blk)
        )
        if rec["victory_score"] is not None:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def save_dir() -> Path:
    import tomllib

    with open(CONFIG_PATH, "rb") as fh:
        cfg = tomllib.load(fh)
    return Path(cfg["bridge"]["save_dir"])


def newest_save(directory: Path | None = None) -> Path | None:
    directory = directory or save_dir()
    saves = sorted(
        glob.glob(str(directory / "**" / "*.sav"), recursive=True),
        key=os.path.getmtime,
    )
    return Path(saves[-1]) if saves else None


#: M2-5（规格 docs/M2-5存档回流差值量化规格_2026-09-19.md）：结构/经济/归因的键集
DIFF_STRUCT_KEYS = (
    "colonies", "districts", "buildings", "zones", "fleet_size",
    "traditions", "tradition_categories", "pops", "empire_size",
)
DIFF_RES_KEYS = (
    "energy", "minerals", "food", "consumer_goods", "alloys",
    "unity", "influence", "trade",
)
#: 动作归因：日志锚点 → 语义（窗内计数，标注非证明）
DIFF_ACTION_PATTERNS = {
    "建矿区划": "建了采矿区划",
    "建粮食区划": "建了粮食区划",
    "建发电区划": "建了发电区划",
    "建城市区划": "建了城市区划",
    "锚地安装": "【锚地】",
    "贸易枢纽安装": "【贸易枢纽】",
    "消费品工厂": "【消费品】",
    "回填补跑": "【回填】",
    "传统采纳": "采纳传统树",
    "飞升授予": "授予飞升天赋",
    "军用舰队": "新建了一支军用舰队",
    "传统可采纳": "【传统】判定：凝聚力已足额",
}


def _budget_income(text: str) -> dict[str, dict[str, float]]:
    """解析 budget 段的月收入分项：资源 → {来源: 月收入}。

    预算段形态（实测）：resource = { country_base=… planet_* = … }。
    只取收入侧（段内首个出现的资源-外层块）。
    """
    i = text.find("budget")
    seg = text[i : i + 8000] if i >= 0 else ""
    out: dict[str, dict[str, float]] = {}
    for m in re.finditer(
        r"(?m)^\t{2}([a-z_]+) = \{\n((?:\t{3}[^\n]*\n)+)\t{2}\}", seg
    ):
        res = m.group(1)
        if res not in DIFF_RES_KEYS:
            continue
        src: dict[str, float] = {}
        for kv in re.finditer(r"([a-z_]+) = (-?[\d.]+)", m.group(2)):
            src[kv.group(1)] = float(kv.group(2))
        if src:
            out[res] = src
    return out


def _log_window_actions(date_old: str, date_new: str) -> dict[str, int]:
    """game.log 窗内动作行计数（按 [游戏日] 过滤；日期标准化为整数千分位比较）。"""
    log = Path.home() / "Documents/Paradox Interactive/Stellaris/logs/game.log"
    if not log.exists():
        return {}
    lo = _date_key(date_old)
    hi = _date_key(date_new)
    out: dict[str, int] = {}
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"\[(\d+)\.(\d+)\.(\d+)\]", ln)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if not (lo <= key <= hi):
            continue
        if "OVERMIND" not in ln:
            continue
        for label, pat in DIFF_ACTION_PATTERNS.items():
            if pat in ln:
                out[label] = out.get(label, 0) + 1
    return out


def _date_key(date_str: str) -> tuple[int, int, int]:
    parts = re.findall(r"\d+", date_str)
    if len(parts) < 3:
        return (0, 0, 0)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _starbase_counts(save: Path) -> dict[str, int]:
    """P1-A（WDMSG-086）：星堡域计数 —— 裸词口径（与 WDMSG-081 §五 取证一致）。

    注意：gamestate 全文计数（含 AI 帝国/设计模板引用）—— 用途是**趋势代理**
    （Δ 相对变化），非精确驻留数；精确逐堡比对见 BACKLOG #4。"""
    text = gamestate_of(save)
    c: dict[str, int] = {}
    for key in ("anchorage", "trading_hub", "solar_panel_network"):
        c[key] = len(re.findall(r"(?<![A-Za-z0-9_])" + key + r"(?![A-Za-z0-9_])", text))
    return c


def diff_saves(old: Path, new: Path) -> dict:
    """M2-5：两次年度档差值量化（规格 §4）。结构 Δ / 库存 Δ / 动作归因 三段。"""
    e_old, e_new = extract(old), extract(new)
    p_old, p_new = probe_overmind(old), probe_overmind(new)
    if e_old.get("year", 0) >= e_new.get("year", 0):
        raise SystemExit(f"旧档年份须早于新档：{e_old['year']} vs {e_new['year']}（铁律 18：同局连续）")

    v_old = p_old.get("variables") or {}
    v_new = p_new.get("variables") or {}

    struct = {}
    for k in DIFF_STRUCT_KEYS:
        a, b = e_old.get(k), e_new.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            struct[k] = {"old": a, "new": b, "delta": b - a}

    # 二级 v1.1 待接：budget 段为「月→收支→来源→资源」三层嵌套（实测），
    # 扁平正则不适用 —— 需括号行走解析。当前二级 = 库存差（存量已在 extract.resources）。
    bi_old, bi_new = {}, {}
    budget_income = {}
    for res in sorted(set(bi_old) | set(bi_new)):
        s_old = sum(bi_old.get(res, {}).values())
        s_new = sum(bi_new.get(res, {}).values())
        budget_income[res] = {
            "old": round(s_old, 1), "new": round(s_new, 1),
            "delta": round(s_new - s_old, 1),
        }

    stock = {}
    r_old, r_new = e_old.get("resources") or {}, e_new.get("resources") or {}
    for k in DIFF_RES_KEYS:
        if isinstance(r_old.get(k), (int, float)) and isinstance(r_new.get(k), (int, float)):
            stock[k] = {
                "old": r_old[k], "new": r_new[k],
                "delta": round(r_new[k] - r_old[k], 1),
            }

    counters = {}
    for k in ("om_ships_built", "om_out_built", "om_col_built", "om_wl_built"):
        a, b = v_old.get(k), v_new.get(k)
        counters[k] = {"old": a or 0, "new": b or 0, "delta": (b or 0) - (a or 0)}

    gains = {}
    for k in sorted(set(v_old) | set(v_new)):
        if k.startswith("om_lex_gain_p"):
            gains[k] = {"old": v_old.get(k, 0), "new": v_new.get(k, 0)}

    actions = _log_window_actions(e_old.get("date", ""), e_new.get("date", ""))

    # P1-A（WDMSG-086）：starbases 单列一域 —— V-6 模块审计数据源
    sb_old, sb_new = _starbase_counts(old), _starbase_counts(new)
    starbases = {}
    for k in sorted(set(sb_old) | set(sb_new)):
        starbases[k] = {
            "old": sb_old.get(k, 0), "new": sb_new.get(k, 0),
            "delta": sb_new.get(k, 0) - sb_old.get(k, 0),
        }

    return {
        "old": {"save": e_old["save"], "date": e_old.get("date")},
        "new": {"save": e_new["save"], "date": e_new.get("date")},
        "struct": struct,
        "stock": stock,
        "budget_income": budget_income,
        "counters": counters,
        "gains": gains,
        "actions": actions,
        "starbases": starbases,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从存档读取 Overmind 四类指标")
    ap.add_argument("save", nargs="?", help="存档路径")
    ap.add_argument("--newest", action="store_true", help="取最新存档")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument(
        "--probe", action="store_true",
        help="打印自治层自身状态（om_month / 法案 / 动作标记）",
    )
    ap.add_argument(
        "--trend", nargs="?", const="", metavar="DIR",
        help="扫一个目录下所有存档，出趋势表与 AC-4 判定（默认存档目录）",
    )
    ap.add_argument(
        "--diff", nargs=2, metavar=("OLD", "NEW"), default=None,
        help="M2-5 差值量化：两次年度档的三段差值表（结构/经济/动作归因）",
    )
    ap.add_argument(
        "--score", metavar="SAVE",
        help="打印银河分数榜与主脑帝国排名（V-5，读 victory_score/victory_rank）",
    )
    ap.add_argument(
        "--resources", action="store_true",
        help="趋势模式附带库存表（D-22 资源诊断：能源/矿物/合金/消费品/影响力/凝聚）",
    )
    args = ap.parse_args(argv)

    # ---- 趋势模式 -------------------------------------------------------
    if args.score:
        path = Path(args.score)
        text = gamestate_of(path)
        rows = [r for r in all_scores(text) if r["victory_rank"] is not None]
        rows.sort(key=lambda r: r["victory_rank"])
        cid = resolve_country(text)
        print(f"--- 银河分数榜：{path.name} ---")
        print(f"{'名次':<6}{'分数':>12}{'科技力':>13}{'经济力':>10}{'舰队力':>16}{'国':>5}")
        print("-" * 64)
        for r in rows[:15]:
            mark = "  <= 主脑" if r["id"] == cid else ""
            print(
                f"{int(r['victory_rank']):<6}{r['victory_score']:>12,.0f}"
                f"{(r['tech_power'] or 0):>13,.0f}{(r['economy_power'] or 0):>10,.1f}"
                f"{(r['military_power'] or 0):>16,.0f}{r['id']:>5}{mark}"
            )
        me = next((r for r in rows if r["id"] == cid), None)
        print()
        if me is None:
            print(f"  主脑帝国（id={cid}）没有任何分数读数")
        else:
            sc = me["victory_score"]
            tp = (me["tech_power"] or 0) * 0.25
            ep = (me["economy_power"] or 0) * 1.0
            nxt = next((r for r in rows if r["victory_rank"] > me["victory_rank"]), None)
            print(f"  主脑帝国 id={me['id']}  名次 {int(me['victory_rank'])}/{len(rows)}  总分 {sc:,.0f}")
            print(f"    科技力 {tp:,.0f}（{tp / sc * 100:.1f}%） 经济力 {ep:,.0f}（{ep / sc * 100:.1f}%）")
            print(f"    舰队力 {me['military_power']:,.0f} x 0.00 -> 0 分（defines 确证）")
            if nxt:
                lead = sc / nxt["victory_score"] if nxt["victory_score"] else 0
                print(f"    领先第 {int(nxt['victory_rank'])} 名（{nxt['victory_score']:,.0f}）{lead:.2f} 倍")
        # 必须收口：否则会继续落到 --trend 的 else 兜底分支，
        # 打出「没给存档，也没找到任何存档」的噪音（首次实跑抓获）。
        return 0

    if args.diff:
        old_p, new_p = (Path(x) for x in args.diff)
        report = diff_saves(old_p, new_p)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"=== M2-5 差值量化：{report['old']['date']} → {report['new']['date']} ===")
            print("--- 一级 · 结构 Δ ---")
            for k, v in report["struct"].items():
                print(f"  {k:<24}{v['old']} → {v['new']}  (Δ {v['delta']:+d})")
            print("--- 二级 · 库存 Δ ---")
            for k, v in report["stock"].items():
                print(f"  {k:<24}{v['old']:.0f} → {v['new']:.0f}  (Δ {v['delta']:+.0f})")
            print("--- 三级 · 动作归因（game.log 窗内计数） ---")
            for k, v in report["actions"].items():
                print(f"  {k:<28}× {v}")
            un = report.get("unattributed") or []
            if un:
                print(f"  未归因结构增量: {un}")
        return 0

    if args.trend is not None:
        directory = Path(args.trend) if args.trend else save_dir()
        rows = [extract(p) for p in iter_saves(directory)]
        if not rows:
            print(f"{directory} 下没有可读的存档")
            return 1

        probes = {}
        for p in iter_saves(directory):
            try:
                probes[p.name] = probe_overmind(p)
            except ValueError:
                pass

        fmt = "{:<26}{:>6}" + "{:>12}" * len(TRACKED)
        print(fmt.format("存档", "年份", *TRACKED))
        print("-" * (32 + 12 * len(TRACKED)))
        for r in rows:
            print(fmt.format(r["save"][:26], r["year"], *[r[k] for k in TRACKED]))

        if args.resources:
            rkeys = (
                "energy", "minerals", "alloys", "food",
                "consumer_goods", "influence", "unity", "trade",
            )
            print()
            print("--- 库存表（D-22 资源诊断） ---")
            rf = "{:<26}{:>6}" + "{:>14}" * len(rkeys)
            print(rf.format("存档", "年份", *rkeys))
            print("-" * (32 + 14 * len(rkeys)))
            for r in rows:
                res = r.get("resources") or {}
                cells = [
                    f"{res[k]:,.0f}" if k in res else "-" for k in rkeys
                ]
                print(rf.format(r["save"][:26], r["year"], *cells))

        print()
        print("--- AC-4 判定（v2 · D-13 决策 A：终值不降 + 无持续下降段，冻结只提示） ---")
        report = monotonic_report(rows)
        v2 = ac4_verdict(report)
        for k, x in v2["keys"].items():
            print(f"  {k:<14}{x['verdict']:<8}{x['note']}")
        # R-2 交叉确证：舰队冻结 + om_ships_built 在涨 = 计数与标量矛盾，需人工核查。
        fb = v2["keys"].get("fleet_size")
        if fb and fb["verdict"] == "注意" and probes:
            built = [
                pr["ships_built"]
                for pr in probes.values()
                if pr.get("ships_built") is not None
            ]
            if built and max(built) > 0:
                print(
                    f"  ⚠ fleet_size 冻结但 om_ships_built 最高达 {max(built)}"
                    " —— 计数与标量矛盾，人工核查建舰是否真实发生"
                )
        if len(rows) < 2:
            print("  （只有 1 个存档，无法判定趋势）")
        print(f"  → 总判定: {'通过' if v2['overall'] else '不通过'}")

        if probes:
            print()
            print("--- 自治层状态 ---")
            print(f"{'存档':<26}{'om_month':>9}{'相位':>12}{'日程':>10}   当月动作标记")
            for name, pr in probes.items():
                print(
                    f"{name:<26}{str(pr['om_month']):>9}"
                    f"{str(pr['slot_phase']):>12}{pr['agenda_str']:>10}"
                    f"   {pr['did_flags'] or '（无动作）'}"
                )
        return 0

    # ---- 单单存档模式 ---------------------------------------------------
    save = Path(args.save) if args.save else (newest_save() if args.newest else None)
    if save is None:
        print("没给存档，也没找到任何存档")
        return 1

    if args.probe:
        pr = probe_overmind(save)
        if args.json:
            print(json.dumps(pr, ensure_ascii=False, indent=2))
            return 0
        print(f"{save.name}  国家 {pr['country']}")
        month = pr["om_month"]
        print(f"  om_month      {month}  → 本月格位对应相位 {pr['slot_phase']}")
        print(f"  日程          {pr['agenda_str']}")
        print(f"  om_era        {pr['variables'].get('om_era', '（无记录）')}   ← 年代（相位可达集判据，D-42）")
        for k, v in pr["lex"].items():
            print(f"  {k:<24} {v}")
        print(f"  建舰累计      {pr['ships_built'] if pr['ships_built'] is not None else '（无记录：旧存档或从未建舰）'}")
        print(f"  当月动作标记  {pr['did_flags'] or '（无动作）'}")
        return 0

    data = extract(save)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"{save.name}  ({data['date']})")
        for k in TRACKED:
            print(f"  {k:22} {data[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
