#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能体操作日志（tools/agent_log.py）

用户需求（2026-09-16）：「日志小窗口让我看到不然不知道干啥」——
智能体每做一步都往固定日志文件写一行，再由 `看智能体操作.bat`
（内部调 tools/agent_log_window.py）实时滚动显示，用户随时知道在干什么。

用法
------------------------------------------------------------
  python tools/agent_log.py "打开了游戏主菜单"                  # 默认 [信息]
  python tools/agent_log.py -k 动作 "点击「继续」按钮"
  python tools/agent_log.py -k 验证 "verify 全绿：相位2 归因OK2 告警0"
  python tools/agent_log.py -k 推进 "S-10 影响力经济：宣布宿敌 +1.5/月"
  python tools/agent_log.py -k 警告 "直接跑 exe 会退出，必须走启动器"
  python tools/agent_log.py --tail 30                          # 直接打印最近 30 行

也可作为库用：  from agent_log import log;  log("做了什么", kind="动作")
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

#: 日志文件位置（与项目同仓库，方便随 git 追踪；已在 .gitignore 排除内容体）
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "agent_actions.log"

#: 类别 → ANSI 颜色（与窗口脚本共用同一套约定）
KINDS = {
    "信息": "\x1b[37m",     # 白
    "动作": "\x1b[36m",     # 青
    "验证": "\x1b[32m",     # 绿
    "推进": "\x1b[35m",     # 品红
    "采样": "\x1b[34m",     # 蓝
    "警告": "\x1b[33m",     # 黄
    "错误": "\x1b[31m",     # 红
    "协同": "\x1b[96m",     # 亮青
}
RESET = "\x1b[0m"


def log(msg: str, kind: str = "信息") -> None:
    """追加一行操作日志（带时间戳与类别）。失败不抛异常（不能因日志崩掉主流程）。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{kind}] {msg}"
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def tail(n: int) -> int:
    if not LOG_PATH.exists():
        print(f"（暂无日志文件：{LOG_PATH}）")
        return 0
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-n:]:
        paint = ""
        for k, c in KINDS.items():
            if f"[{k}]" in line:
                paint = c
                break
        print(f"{paint}{line}{RESET}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="智能体操作日志")
    ap.add_argument("message", nargs="?", help="要记录的文本")
    ap.add_argument("-k", "--kind", default="信息", choices=list(KINDS))
    ap.add_argument("--tail", type=int, metavar="N", help="打印最近 N 行后退出")
    a = ap.parse_args(argv)

    if a.tail:
        return tail(a.tail)
    if not a.message:
        ap.print_help()
        return 2
    log(a.message, a.kind)
    # 同时在 stdout 回显，便于在命令输出里也看到（不依赖窗口）
    print(f"{KINDS.get(a.kind, '')}[{a.kind}] {a.message}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
