#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信箱 lint + 未读超时看门狗（通讯基础设施，双方共用）

背景（2026-09-18，用户指令「那完善我们的机制吧」）
--------------------------------------------------
多智能体协作的两大固有失败模式，本项目都撞过：
  ① **并发写冲突** —— 双方共写同一文件（信箱走 git ⇒ 提交互卷）；
  ② **协调死锁 (coordination deadlock)** —— 消息发出后对方**永远没读到**
     （前缀匹配错配 ⇒ 19 小时漏读 13 条）。
业界对死锁的标准缓解手段是 **timeouts + 指定仲裁者**。本脚本补的正是「超时」这一半：
任一条目「待处理」超过阈值仍未被处理 ⇒ 告警（仲裁者 = 用户）。

同时做**格式校验**：把协议里靠人记的规则（五字段 / 编号连续 / 状态取值 / 方向前缀）
变成可执行断言 —— 「90% 的逻辑应该是代码，不是提示词」。

用法
----
    python tools/mail_lint.py                  # 全量校验 + 超时检查
    python tools/mail_lint.py --watchdog       # 只看超时（适合挂在巡检里）
    python tools/mail_lint.py --json           # 机器可读
    python tools/mail_lint.py --stale-min 60   # 自定义超时阈值（默认 30 分钟）

退出码：0 = 无问题；1 = 有告警/校验失败（便于自动化判读）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
HANDOFF = REPO / "HANDOFF"

# 信箱 → (期望的标题前缀, 该信箱的收件方)
INBOXES = {
    "inbox_zcode.md": ("WDMSG", "zcode"),
    "inbox_workbuddy.md": ("ZCMSG", "WorkBuddy"),
}
ARCHIVES = ("archive_zcode.md", "archive_workbuddy.md")

HDR = re.compile(r"^## \[([A-Z]+)-(\d+)\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(.+?)\s*$", re.M)
STATUS = re.compile(r"^-\s*状态:\s*(.*?)\s*$", re.M)
FIELDS = ("状态", "主题", "内容")
RESERVED_FROM = 900          # 900+ 保留段（信标/特殊件），不参与正常序列
VALID_STATUS = ("待处理", "已完成", "作废")
# 2026-09-18 新增「作废」态：消息**已在信箱里发出、事后发现不应生效**时使用
# （首例 WDMSG-069：共享工具 game_control.restart 的 notify 误署名）。
# 处置：状态改为「作废（原因，见 WDMSG-nnn）」并在正文**追加**订正说明（正文不改，符合"不删改"）。
# 作废 = 已闭环：不进「待处理」计数、不触发看门狗。


def parse_entries(path: Path) -> list[dict]:
    """把信箱切成条目列表。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    out: list[dict] = []
    marks = list(HDR.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.start():end]
        st = STATUS.search(body)
        raw_status = st.group(1).strip() if st else ""
        out.append({
            "prefix": m.group(1),
            "no": int(m.group(2)),
            "ts": m.group(3),
            "route": m.group(4).strip(),
            "status_raw": raw_status,
            "done": (raw_status.startswith("✅") or "已完成" in raw_status
                     or "作废" in raw_status or "撤回" in raw_status),
            "has": {f: bool(re.search(r"^-\s*%s:" % f, body, re.M)) for f in FIELDS},
            "chars": len(body),
        })
    return out


def age_minutes(ts: str) -> float | None:
    try:
        return (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M")).total_seconds() / 60
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="信箱 lint + 未读超时看门狗")
    ap.add_argument("--watchdog", action="store_true", help="只看超时（简洁输出）")
    ap.add_argument("--stale-min", type=float, default=30.0, help="超时阈值（分钟，默认 30）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    report = {"inboxes": {}, "stale": [], "problems": [], "archived": {}}

    for fname, (want_prefix, receiver) in INBOXES.items():
        ents = parse_entries(HANDOFF / fname)
        pending = [e for e in ents if not e["done"]]
        report["inboxes"][fname] = {"total": len(ents), "pending": len(pending)}

        for e in ents:
            # ① 方向前缀与信箱一致
            if e["prefix"] != want_prefix:
                report["problems"].append(
                    f"{fname}: [{e['prefix']}-{e['no']}] 前缀不符（该信箱应为 {want_prefix}-）")
            # ② 状态取值合法
            if not any(v in e["status_raw"] for v in VALID_STATUS):
                report["problems"].append(
                    f"{fname}: [{e['prefix']}-{e['no']}] 状态非法或缺失：{e['status_raw']!r}")
            # ③ 字段齐全
            missing = [f for f, ok in e["has"].items() if not ok]
            if missing:
                report["problems"].append(
                    f"{fname}: [{e['prefix']}-{e['no']}] 缺字段：{'/'.join(missing)}")
            # ④ 超时看门狗（只查待处理）
            if not e["done"]:
                age = age_minutes(e["ts"])
                if age is not None and age > args.stale_min:
                    report["stale"].append({
                        "box": fname, "msg": f"{e['prefix']}-{e['no']}",
                        "waiting_min": round(age, 1), "receiver": receiver,
                    })

    # ⑤ 编号连续性（排除 900+ 保留段）
    for fname in INBOXES:
        nums = sorted({e["no"] for e in parse_entries(HANDOFF / fname) if e["no"] < RESERVED_FROM})
        if nums:
            gaps = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
            report["archived"][fname] = {"range": [nums[0], nums[-1]], "gaps": gaps}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if (report["problems"] or report["stale"]) else 0

    ok = True
    if not args.watchdog:
        print("=== 信箱 lint ===")
        for fname, st in report["inboxes"].items():
            print(f"  {fname}: 条目 {st['total']}｜待处理 {st['pending']}")
        for fname, st in report["archived"].items():
            g = st["gaps"]
            print(f"  编号区间 {fname}: {st['range'][0]}–{st['range'][1]}"
                  + (f"  ⚠️ 跳号 {g}" if g else "  ✅ 连续"))
        if report["problems"]:
            ok = False
            print("\n=== 格式问题 ===")
            for p in report["problems"]:
                print(f"  ⚠️ {p}")
        else:
            print("\n  ✅ 字段齐全 / 方向前缀正确 / 状态取值合法")

    if report["stale"]:
        ok = False
        print(f"\n=== ⏰ 未读超时告警（>{args.stale_min:.0f} 分钟）===")
        for s in report["stale"]:
            print(f"  🔴 {s['msg']} 在 {s['box']} 已等待 {s['waiting_min']} 分钟"
                  f"（收件方 {s['receiver']} 未处理）")
        print("  ⇒ 按 README 机制 v2：请升级给**用户**仲裁，或检查对方巡检是否停摆。")
    elif not args.watchdog:
        print(f"\n  ✅ 无超时（阈值 {args.stale_min:.0f} 分钟）")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
