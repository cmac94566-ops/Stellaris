#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能体操作日志 —— 实时小窗口（tools/agent_log_window.py）

用户需求（2026-09-16）：「日志小窗口让我看到不然不知道干啥」。
本窗口跟随 `logs/agent_actions.log`，实时滚动显示智能体的每一步动作，
让人一眼看出「它现在在干什么、干到哪了」。

启动方式：双击项目根目录的 `看智能体操作.bat`（推荐），或直接
    python tools/agent_log_window.py

选项：
    --lines N     启动时先回显最近 N 行（默认 40）
    --interval S  轮询间隔秒（默认 0.8）
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "agent_actions.log"

KINDS = {
    "信息": "\x1b[37m",
    "动作": "\x1b[36m",
    "验证": "\x1b[32m",
    "推进": "\x1b[35m",
    "采样": "\x1b[34m",
    "警告": "\x1b[33m",
    "错误": "\x1b[31m",
    "协同": "\x1b[96m",
}
RESET = "\x1b[0m"
DIM = "\x1b[90m"
BOLD = "\x1b[1m"


def enable_ansi() -> None:
    """让 Windows 控制台支持 ANSI 颜色。"""
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        k.GetConsoleMode(h, ctypes.byref(mode))
        k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def set_title(text: str) -> None:
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def paint(line: str) -> str:
    color = ""
    for k, c in KINDS.items():
        if f"[{k}]" in line:
            color = c
            break
    return f"{color}{line}{RESET}" if color else line


def read_lines() -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        return LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=40)
    ap.add_argument("--interval", type=float, default=0.8)
    a = ap.parse_args(argv)

    enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    set_title("Overmind 智能体操作日志")

    os.system("cls" if os.name == "nt" else "clear")
    print(f"{BOLD}=== Overmind 智能体操作日志 ==={RESET}  "
          f"{DIM}实时跟随 {LOG_PATH.name} · Ctrl+C 退出{RESET}")
    print(f"{DIM}{'─' * 72}{RESET}")

    shown = read_lines()
    for line in shown[-a.lines:]:
        print(paint(line))
    if not shown:
        print(f"{DIM}（还没有日志。智能体一旦开始动作，这里会实时出现。）{RESET}")

    # 实时跟随：按行数增量读取（比按字节安全，不会切碎多字节字符）
    count = len(shown)
    try:
        while True:
            time.sleep(a.interval)
            lines = read_lines()
            if len(lines) > count:
                for line in lines[count:]:
                    print(paint(line))
                count = len(lines)
            elif len(lines) < count:      # 日志被清空/轮转
                print(f"{DIM}{'─' * 72}{RESET}")
                print(f"{DIM}（日志已重置，等待新内容）{RESET}")
                count = len(lines)
    except KeyboardInterrupt:
        print(f"\n{DIM}窗口已关闭。{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
