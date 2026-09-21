#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话实机验证 —— 职责三清单一键化（2026-09-18 用户指令：重复操作写脚本）。

用法：
    py tools/verify_session.py            # 立即扫当前会话，出验证报告
    py tools/verify_session.py --wait 120 # 先等 120 秒（给游戏时间跑）再扫
    py tools/verify_session.py --json     # 机器可读输出（供 cron/信箱引用）

清单（COORDINATION 职责三 + 各实机待验项）：
  1. 会话识别：game.log 头部启动时间 + OVERMIND 行数
  2. 接管 / lex applied rev（新局/接管必有；续档继承变量时二者可缺 —— 标注即可）
  3. 【求值】全量次数 + 规则命中分布（S-18）
  4. 【熔断】相位分布（A3 健康度信号）
  5. 【领袖】创建/就任（S-15 exists 修法）
  6. 【相位 1-8/8】齐全性（C-03）
  7. error.log（限本会话时间段）：Invalid context switch / 涉 overmind 的 Wrong scope —— 应 0
  8. 【告警】（原版 AI 接管）—— 应 0
  9. 顺带更新 logs/.restart_watch.json 基线（rev 行数）

退出码：0 = 无 ❌ 项；1 = 有 ❌（供 cron 判断是否需要通报）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = Path("C:/Users/<user>/Documents/Paradox Interactive/Stellaris/logs")
GAME_LOG = LOG_DIR / "game.log"
ERR_LOG = LOG_DIR / "error.log"
WATCH = REPO / "logs/.restart_watch.json"

PHASE_TAGS = [f"【相位 {i}/8】" for i in range(1, 9)]


def _hms(line: str) -> str | None:
    m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", line)
    return m.group(1) if m else None


def _sec(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stellaris 会话实机验证清单")
    ap.add_argument("--wait", type=int, default=0, help="扫描前等待秒数")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()
    if args.wait:
        print(f"等待 {args.wait}s 让游戏积累日志……")
        time.sleep(args.wait)

    checks: list[dict] = []

    def add(name: str, ok: bool | str, detail: str) -> None:
        checks.append({"check": name, "verdict": ok, "detail": detail})

    gt = GAME_LOG.read_text(encoding="utf-8", errors="replace") if GAME_LOG.exists() else ""
    et = ERR_LOG.read_text(encoding="utf-8", errors="replace") if ERR_LOG.exists() else ""
    glines = gt.splitlines()

    # 1. 会话识别
    boot = _hms(glines[0]) if glines else None
    om_lines = [ln for ln in glines if "OVERMIND" in ln]
    add("会话", bool(boot and om_lines),
        f"启动={boot} OVERMIND行={len(om_lines)}")

    # 2. 接管 / rev
    takeover = sum(1 for ln in om_lines if "主脑已接管帝国" in ln)
    revs = [ln for ln in om_lines if "lex applied rev" in ln]
    rev_m = re.search(r"lex applied rev (\d+)", revs[-1]) if revs else None
    note = "续档继承变量时不产生，属预期" if not takeover else ""
    add("接管/rev", "ok" if (takeover or revs) else "note",
        f"接管×{takeover} rev={rev_m.group(1) if rev_m else '—'} {note}")

    # 3. 求值分布（S-18）
    evals = [ln for ln in om_lines if "【求值】" in ln]
    full = sum(1 for ln in evals if "全量求值完成" in ln)
    hits = Counter(m.group(1) for ln in evals
                   if (m := re.search(r"规则 #(\d+) 命中", ln)))
    add("求值(S-18)", "ok" if full else ("note" if not evals else "warn"),
        f"全量×{full} 命中分布={dict(sorted(hits.items(), key=lambda x: -x[1]))}")

    # 4. 熔断分布 + 解除哨兵（WDMSG-076 P2：拆两条 —— 分布供调优；解除>0 才证明无单向锁死）
    brk = Counter()
    for ln in om_lines:
        m = re.search(r"【熔断】相位 (\d)/8", ln)
        if m:
            brk[m.group(1)] += 1
    released = sum(1 for ln in om_lines if "【熔断解除】" in ln)
    total_months = sum(1 for ln in om_lines if "全量求值完成" in ln)
    # 活锁哨兵：熔断已成周期（≥3 行 ≈ 至少一轮三槽）且 240+ 月无一次解除 → warn
    if brk and released == 0 and total_months >= 24:
        rel_verdict = "warn"
    else:
        rel_verdict = "ok"
    add("熔断(A3)", "ok", f"分布={dict(sorted(brk.items()))}（熔断本身=设计行为，分布供调优）")
    add("熔断解除(D-41哨兵)", rel_verdict,
        f"解除×{released} / 熔断×{sum(brk.values())} / 求值月={total_months}"
        + (" ⚠️ 熔断已成周期但零解除 —— 增益自报链嫌疑" if rel_verdict == "warn" else ""))

    # 5. 领袖（S-15）
    lead_make = sum(1 for ln in om_lines if "创建指挥官" in ln)
    lead_take = sum(1 for ln in om_lines if "指挥官已就任" in ln)
    verdict = "ok" if (lead_make + lead_take) else "note"
    if lead_make > 12:  # S-15b 口径：整局创建应 ≈1-2，逐月循环 = assign 未贴上
        verdict = "warn"
    add("领袖(S-15)", verdict,
        f"创建×{lead_make} 就任×{lead_take}"
        + ("（⚠️ 逐月循环 = assign 未贴上，S-15b 信号）" if verdict == "warn" else "（无无领袖舰队时静默属预期）"))

    # 6. 相位齐全性 —— 按 era 可达集判定（D-42，WDMSG-075）：
    #    era≤2 和平局相位 6/7/8 结构不可达（rule #11 须 om_era≥3，#1/#2 须战争/威胁）
    #    ⇒ 可达集 {1,2,3,4,5}；era≥3 ⇒ 全 8 相位。era 在下方存档探测取值，先暂存 seen。
    seen_phases = {int(p.group(1)) for ln_ in om_lines
                   if (p := re.search(r"【相位 (\d)/8】", ln_))}
    phase_era_placeholder = True  # 判定推迟到存档探测（第 9 项）之后执行

    # 7/8. error.log（限本会话时间段）+ 告警
    if boot:
        b = _sec(boot)
        session_err = [ln for ln in et.splitlines()
                       if (t := _hms(ln)) and _sec(t) >= b]
    else:
        session_err = et.splitlines()
    invalid_om = sum(1 for ln in session_err
                     if "Invalid context switch" in ln and "overmind" in ln.lower())
    invalid_all = sum(1 for ln in session_err if "Invalid context switch" in ln)
    wrong_sc = sum(1 for ln in session_err
                   if "Wrong scope" in ln and "overmind" in ln.lower())
    add("error.log", "ok" if (invalid_om == 0 and wrong_sc == 0) else "fail",
        f"涉overmind的InvalidContextSwitch={invalid_om}"
        f"（另有 {invalid_all - invalid_om} 条来自原版/他mod，不计责）"
        f" 涉overmind的WrongScope={wrong_sc}")
    # 9. 存档变量通道（P1，WDMSG-071）：续档会话无 rev 行 ⇒ 从最新存档 om_lex_rev 读版本
    import subprocess
    import glob as _glob
    import os as _os
    rev_read = "—（无可用存档）"
    era = None
    try:
        pats = [
            "C:/Program Files (x86)/Steam/userdata/*/281990/remote/save games/*/*.sav",
            "C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games/*/*.sav",
        ]
        saves: list[str] = []
        for pat in pats:
            saves += _glob.glob(pat)
        if saves:
            newest = max(saves, key=_os.path.getmtime)
            r = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "overmind_metrics.py"), "--probe", newest],
                capture_output=True, text=True, timeout=180,
            )
            m = re.search(r"om_lex_rev\s+(\d+)", r.stdout or "")
            c = re.search(r"om_lex_rule_count\s+(\d+)", r.stdout or "")
            me = re.search(r"om_era\s+(\d+)", r.stdout or "")
            era = int(me.group(1)) if me else None
            if m:
                rev_read = f"{m.group(1)} (rule_count={c.group(1) if c else '?'}, era={era if era else '?'}, {_os.path.basename(newest)})"

    except Exception:
        rev_read = "—（探测失败）"

    # 6b. 相位覆盖判定（D-42）：seen 覆盖「era 可达集」即通过
    # 可达集（WDMSG-075 口径）：era≤2 → {1,2,3,4,5}（6/7/8 结构不可达）；
    # era=3 → {1,2,3,4,5,7}（6/8 需战争/威胁，和平期不可达）；
    # era≥4 → 全 8（扩张/危机期战争循环按执行手册应可达）。
    reachable = set(range(1, 9))
    era_note = ""
    if era is not None:
        if era <= 2:
            reachable = {1, 2, 3, 4, 5}
            era_note = f"era={era}（相位 6/7/8 结构不可达属预期）"
        elif era == 3:
            reachable = {1, 2, 3, 4, 5, 7}
            era_note = f"era={era}（相位 6/8 需战争/威胁，和平期不执行属预期）"
        else:
            era_note = f"era={era}"
    covered = seen_phases & reachable
    if len(seen_phases) == 8 or (era is not None and covered == reachable):
        ph_verdict = "ok"
    else:
        ph_verdict = "warn"
    add("相位(C-03/D-42)", ph_verdict,
        f"见 {len(seen_phases)}/8 {sorted(seen_phases)}；可达集 {sorted(reachable)}（{era_note}）")
    add("存档rev(P1)", "ok" if "—" not in rev_read else "note", f"om_lex_rev={rev_read}")

    # 10. 告警（原版 AI 接管）—— 应 0
    alarm = sum(1 for ln in om_lines if "【告警】" in ln)
    add("告警(AI接管)", "ok" if alarm == 0 else "fail", f"【告警】×{alarm} 应 0")

    # 9. 基线
    rev_n = len(revs)
    WATCH.parent.mkdir(parents=True, exist_ok=True)
    WATCH.write_text(json.dumps({
        "game_log_rev_lines": rev_n,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "boot": boot,
        "note": "由 verify_session.py 维护；配合进程启动时间判定新会话",
    }, ensure_ascii=False), encoding="utf-8")

    bad = [c for c in checks if c["verdict"] == "fail"]
    if args.json:
        print(json.dumps({"boot": boot, "checks": checks, "fail": len(bad)},
                         ensure_ascii=False, indent=2))
    else:
        print(f"=== 会话验证报告（启动 {boot}）===")
        for c in checks:
            v = {"ok": "✅", "warn": "⚠️", "note": "ℹ️ ", "fail": "❌"}.get(c["verdict"], "·")
            print(f"{v} {c['check']}: {c['detail']}")
        print(f"基线已更新 logs/.restart_watch.json（rev 行={rev_n}）")
        # D-42 三档结论：fail=❌ 不通过 / warn=⚠️ 带保留通过（缺口不得被"全绿"掩盖）
        warn = [c for c in checks if c["verdict"] == "warn"]
        print("结论：" + ("❌ 存在失败项，需处置" if bad else
                          ("⚠️ 带保留通过（warn：" + "；".join(c["check"] for c in warn) + "）" if warn else "✅ 全绿")))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
