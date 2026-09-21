#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮箱守望 —— 每 20 分钟检查 HANDOFF/ 双信箱，发现新消息立即提醒。

背景（2026-09-16 用户指令）：
    用户要求「时间间隔 20 分钟吧」。但**调度器（automation）的最小粒度是 1 小时**
    （rrule 只支持 HOURLY/DAILY/WEEKLY/MONTHLY/YEARLY），做不到 20 分钟。
    因此用本地守望补足**感知**粒度：脚本每 20 分钟扫描一次信箱，
    发现新消息就写操作日志 + 控制台高亮 + 响铃，让用户立刻知道；
    **实际处理**仍由每小时的「Overmind 邮箱巡检」自动化执行（处理需要智能体能力）。

用法：
    双击项目根目录的「看邮箱.bat」，或
    python tools/mailbox_watch.py [--interval 1200] [--once]

归属说明：本脚本是「通讯基础设施」（非产品代码）。按 2026-09-16 分工，
    tools/ 归 zcode 属地；本文件由 WorkBuddy 按用户直接指派编写，
    后续维护与改造请交给 zcode（改动请在 HANDOFF 信箱知会）。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
from pathlib import Path

# --- 路径 -------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[1]
HANDOFF = REPO / "HANDOFF"
INBOX_WB = HANDOFF / "inbox_workbuddy.md"   # zcode 写给我方
INBOX_ZC = HANDOFF / "inbox_zcode.md"       # 我方写给 zcode（含其回执）
SEEN_FILE = REPO / "logs" / ".mailbox_seen.json"
AGENT_LOG = REPO / "tools" / "agent_log.py"

# --- ANSI -------------------------------------------------------------------
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
GREEN = "\x1b[32m"
CYAN = "\x1b[36m"
GRAY = "\x1b[90m"

# 2026-09-18 修正（MSG-900 自愈指令）：协议 2026-09-17 起改方向前缀 WDMSG-/ZCMSG-，
# 硬编码旧前缀 MSG- 曾致 19 小时漏读 13 条。此处兼容三种形态；
# 且条目定位以「- 状态: 待处理」为准（pending_in_wb），与标题形态解耦。
MSG_RE = re.compile(r"^## \[(?:WD|ZC)?MSG-(\d+)\]\s*(.*)$", re.M)


def enable_ansi() -> None:
    """让 Windows 控制台支持 ANSI 转义。"""
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def set_title(text: str) -> None:
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"wb_max": 0, "zc_max": 0}


def save_seen(data: dict) -> None:
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def read_msg_ids(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(编号, 主题行, 该条全文)]。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = list(MSG_RE.finditer(text))
    out: list[tuple[int, str, str]] = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.append((int(m.group(1)), m.group(2).strip(), text[m.start():end]))
    return out


def pending_in_wb(text: str) -> list[str]:
    """inbox_workbuddy 里「状态: 待处理」的条目主题。"""
    out = []
    for block in re.split(r"(?=^## \[)", text, flags=re.M):
        if not block.strip().startswith("## "):
            continue
        if re.search(r"^-\s*状态:\s*待处理", block, re.M):
            head = block.splitlines()[0].replace("## ", "").strip()
            out.append(head)
    return out


def notify(msg: str, kind: str = "信息") -> None:
    """写操作日志（失败不影响主流程）。"""
    try:
        import subprocess
        subprocess.run(
            [sys.executable, str(AGENT_LOG), "-k", kind, msg],
            cwd=str(REPO), timeout=15, capture_output=True,
            # 2026-09-20：宿主可能是 pythonw/无控制台 => 不带此标志会闪黑窗
            creationflags=0x08000000,
        )
    except Exception:
        pass


def beep() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        print("\a", end="", flush=True)


def scan(seen: dict, first: bool) -> tuple[dict, list[str]]:
    """扫一次，返回 (新的 seen, 提醒行列表)。"""
    alerts: list[str] = []

    if INBOX_WB.exists():
        wb_text = INBOX_WB.read_text(encoding="utf-8", errors="replace")
        pend = pending_in_wb(wb_text)
        if pend and not first:
            alerts.append(f"{RED}{BOLD}【待办 {len(pend)} 条】{RESET} " + " / ".join(pend[:3]))

        ids = [n for n, _, _ in read_msg_ids(INBOX_WB)]
        top = max(ids) if ids else 0
        if top > seen.get("wb_max", 0) and not first:
            for n, topic, _ in read_msg_ids(INBOX_WB):
                if n > seen.get("wb_max", 0):
                    alerts.append(f"{YELLOW}zcode → 我方 新消息 MSG-{n:03d}：{topic}{RESET}")
        seen["wb_max"] = max(top, seen.get("wb_max", 0))

    if INBOX_ZC.exists():
        zc_text = INBOX_ZC.read_text(encoding="utf-8", errors="replace")
        ids = [n for n, _, _ in read_msg_ids(INBOX_ZC)]
        top = max(ids) if ids else 0
        # 我方的信由我方写，所以「编号增长」是我自己写的；
        # 真正要看的是**回执行被填写**（zcode 的动向）
        for n, topic, block in read_msg_ids(INBOX_ZC):
            if n <= seen.get("zc_max", 0):
                continue
        if not first:
            filled = re.findall(r"^-\s*回执:\s*✅", zc_text, re.M)
            alerts.append(f"{GRAY}（inbox_zcode 已填回执 {len(filled)} 条）{RESET}") if filled else None
        seen["zc_max"] = max(top, seen.get("zc_max", 0))

    return seen, [a for a in alerts if a]


def main() -> int:
    ap = argparse.ArgumentParser(description="邮箱守望（每 20 分钟检查 HANDOFF 双信箱）")
    ap.add_argument("--interval", type=int, default=1200,
                    help="轮询间隔秒数（默认 1200 = 20 分钟）")
    ap.add_argument("--once", action="store_true", help="只扫一次就退出（自检用）")
    args = ap.parse_args()

    enable_ansi()
    set_title(f"邮箱守望 — 每 {args.interval // 60} 分钟检查 HANDOFF")
    seen = load_seen()

    print(f"{BOLD}{CYAN}邮箱守望启动{RESET}  间隔 {args.interval // 60} 分钟")
    print(f"  监视：{HANDOFF}")
    print(f"  说明：本窗口只做**提醒**；实际处理由「Overmind 邮箱巡检」自动化（每小时）执行。")
    print(f"  {GRAY}Ctrl+C 退出{RESET}")
    print()

    first = True
    while True:
        seen, alerts = scan(seen, first)
        stamp = time.strftime("%H:%M:%S")
        if alerts:
            print(f"{GRAY}[{stamp}]{RESET} " + f"\n         ".join(alerts), flush=True)
            notify("邮箱守望发现新消息：" + " | ".join(re.sub(r"\x1b\[[0-9;]*m", "", a) for a in alerts), "协同")
            beep()
        elif first:
            print(f"{GRAY}[{stamp}] 已建立基线（只记录当前最新编号，不报警）{RESET}", flush=True)
        else:
            print(f"{GRAY}[{stamp}] 无新消息{RESET}", flush=True)
        save_seen(seen)

        if args.once:
            return 0
        first = False
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n{GRAY}已退出{RESET}")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
