"""主脑实时日志窗口 — 在游戏旁边开一个小窗，实时显示 AI 在做什么。

为什么需要它
------------
自治层（mod 脚本层）每隔一个月执行一次 ``overmind_autonomy_tick``，每个动作都带一句
``log = "OVERMIND: ..."``。这些行会写进游戏的日志目录，但**没人会去翻**——日志动辄
上百万行、混着原版 mod 的本地化警告。这个工具把那几行挑出来，实时打在屏幕上。

日志落在哪个文件（为什么同时盯着多个）
----------------------------------
Clausewitz 的 ``log`` effect 走的是引擎的 game channel，所以主要落点应是
``logs/game.log``。但这一点**没有在对局里实测过**（自治层此前从未真正跑起来过），
而赌错文件的代价是"窗口一片空白，看不出是真没动作还是找错了地方"。
所以这里同时跟 ``game.log`` / ``error.log`` / ``system.log`` / ``debug.log``，
哪个出现 OVERMIND 就报哪个，并在行首标出来源。等实机确认落点后可以收窄。

用法
----
    py -3.12 tools/overmind_log_window.py                # 跟默认日志目录
    py -3.12 tools/overmind_log_window.py --list         # 只看各文件现状，不跟
    py -3.12 tools/overmind_log_window.py --keyword X    # 换关键字（默认 OVERMIND）
    py -3.12 tools/overmind_log_window.py --dir <path>   # 指定日志目录

双击 ``看主脑日志.bat`` 即可启动。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_LOG_DIR = Path(
    os.path.expandvars(
        r"%USERPROFILE%\Documents\Paradox Interactive\Stellaris\logs"
    )
)

#: Log files to tail.  Order is the display priority when several are active.
CANDIDATES = ("game.log", "error.log", "system.log", "debug.log")

#: How much of an existing file to backfill on first attach.
#:
#: 曾经是 0（只看启动之后的新行）—— 结果是**窗口打开一片空白**，而主脑每月
#: 才动作一次，用户看着就像「这窗口根本不更新」（2026-09-16 用户实测反馈）。
#: 改成从文件末尾回填若干字节，启动即可看到最近的主脑动作，之后继续实时跟随。
TAIL_BYTES = 20000

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
MAGENTA = "\x1b[35m"
RED = "\x1b[31m"
GRAY = "\x1b[90m"

#: Colour by what the action does, so a glance tells you the category.
ACTION_COLORS = (
    # 顺序即优先级。D-7 / D-17 引入的三个标记必须排在最前：
    #   [告警]     —— D-17/F1 归因守卫：原版 AI 接管（human_ai），本轮指标
    #                 不可归因，且自治心跳已停摆。用**粗体品红**，比 [异常]
    #                 更醒目 —— 它否定的不是某次动作，而是整轮验收。
    #   [异常]     —— 格位越界告警，最需要一眼看到，用粗体红。
    #   [相位 N/8] —— 每月一条的相位入口日志，用灰色当分隔标记。
    #                若给它醒目颜色，真正的动作行会被淹掉。
    ("【告警】", BOLD + MAGENTA),
    ("【异常】", BOLD + RED),
    # R-1：每月一条的归因心跳，和 [相位 ] 一样是"安静的正面证据"，
    # 用灰色，不与真正的动作行抢注意力。
    ("【归因OK】", GRAY),
    ("【相位 ", GRAY),
    ("建了", GREEN),
    ("接管", MAGENTA),
    ("交还", RED),
    ("征募", YELLOW),
    ("重建", CYAN),
    ("授予", YELLOW),
    ("外交", CYAN),
    ("engaged", MAGENTA),
    ("disengaged", RED),
)


def enable_ansi() -> None:
    """Turn on VT processing so colours work in the legacy console host."""
    if os.name != "nt":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:  # noqa: BLE001
        pass


def colorize(line: str) -> str:
    for needle, color in ACTION_COLORS:
        if needle in line:
            return f"{color}{line}{RESET}"
    return line


class Tailer:
    """Follow one file, yielding only lines appended after we attach."""

    def __init__(self, path: Path, from_start: bool = False) -> None:
        self.path = path
        self.pos = 0
        self.buf = b""
        self._init_pos(from_start)

    def _init_pos(self, from_start: bool) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            self.pos = 0
            return
        if from_start:
            self.pos = 0
        else:
            # Skip existing content but keep a small overlap so a line written
            # mid-attach is not lost.  Reading whole lines is handled below.
            self.pos = max(0, size - TAIL_BYTES)

    def read_new(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:
            # File was rotated or truncated (the game recreates logs on start).
            self.pos = 0
            self.buf = b""
        if size == self.pos:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read()
                self.pos = fh.tell()
        except OSError:
            return []
        self.buf += chunk
        # Split on newline; keep an incomplete trailing fragment buffered.
        *complete, self.buf = self.buf.split(b"\n")
        out: list[str] = []
        for raw in complete:
            text = raw.decode("utf-8", "replace").rstrip("\r")
            if text:
                out.append(text)
        return out


def show_state(log_dir: Path, keyword: str) -> int:
    print(f"{BOLD}日志目录{RESET} {log_dir}")
    if not log_dir.exists():
        print(f"  {RED}目录不存在{RESET} —— 游戏可能还没启动过")
        return 1
    print()
    print(f"{'文件':<16}{'大小':>12}  {'最后修改':<10}  {keyword} 命中")
    print("-" * 56)
    for name in CANDIDATES:
        p = log_dir / name
        if not p.exists():
            print(f"{name:<16}{'—':>12}  {'—':<10}  —")
            continue
        st = p.stat()
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        hits = len(re.findall(re.escape(keyword), txt, re.I))
        size = f"{st.st_size / 1024:.0f} KB"
        mtime = time.strftime("%H:%M:%S", time.localtime(st.st_mtime))
        print(f"{name:<16}{size:>12}  {mtime:<10}  {hits}")
    return 0


def simplify(line: str) -> str:
    """把冗长的引擎日志行提炼成「[游戏内日期] 动作描述」。

    原始行（典型）::

        [effect_impl.cpp:21980]: [2214.1.1] Log effect, common/scripted_effects/
        overmind_autonomy.txt:1527 @ scripted effect overmind_expand_try_mining @ ...
        OVERMIND: 建了一座采矿站 —— 矿物 -100

    提炼后::

        [2214.1.1] 建了一座采矿站 —— 矿物 -100

    用户反馈（2026-09-16）：「我要看游戏的操作」—— 满屏引擎路径等于没信息，
    必须只留「什么时候、干了什么」。
    """
    i = line.find("OVERMIND: ")
    if i == -1:
        return line
    tail = line[i + len("OVERMIND: "):].rstrip()
    m = re.search(r"\[(\d{4}\.\d{1,2}\.\d{1,2})\]", line[:i])
    return (f"[{m.group(1)}] " if m else "") + tail


def main() -> int:
    ap = argparse.ArgumentParser(description="主脑实时日志窗口")
    ap.add_argument("--dir", type=Path, default=DEFAULT_LOG_DIR, help="日志目录")
    ap.add_argument("--keyword", default="OVERMIND", help="过滤关键字")
    ap.add_argument("--list", action="store_true", help="只列现状，不跟")
    ap.add_argument("--all", action="store_true", help="从头显示已有内容")
    ap.add_argument("--interval", type=float, default=0.7, help="轮询间隔（秒）")
    args = ap.parse_args()

    enable_ansi()
    # 给控制台窗口一个一眼能认出的标题（否则默认是 python.exe 路径）
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("主脑日志 — AI 在对局里做什么")
    except Exception:
        pass
    log_dir: Path = args.dir

    if args.list:
        return show_state(log_dir, args.keyword)

    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  主脑实时日志{RESET}  —  关键字 {CYAN}{args.keyword}{RESET}")
    print(f"{DIM}  {log_dir}{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    if not log_dir.exists():
        print(f"{RED}日志目录不存在：{log_dir}{RESET}")
        return 1

    tailers = {}
    for name in CANDIDATES:
        p = log_dir / name
        if p.exists():
            tailers[name] = Tailer(p, from_start=args.all)
    if not tailers:
        print(f"{YELLOW}目录里还没有任何日志文件。先启动游戏。{RESET}")
        return 1

    print(f"{DIM}正在跟 {', '.join(tailers)} ...{RESET}")
    print(f"{DIM}（先把游戏打开并进入对局；AI 接管后这里会开始滚字）{RESET}")
    print()

    kw = args.keyword.lower()
    seen_any = False
    last_heartbeat = time.time()

    try:
        while True:
            emitted = False
            for name, t in tailers.items():
                for line in t.read_new():
                    if kw in line.lower():
                        seen_any = True
                        stamp = time.strftime("%H:%M:%S")
                        body = line
                        # Strip the engine's own [HH:MM:SS] prefix when present
                        # so the timestamp is not printed twice.
                        m = re.match(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.*)$", line)
                        if m:
                            stamp, body = m.group(1), m.group(2)
                        # 提炼成「[游戏内日期] 动作」，颜色按动作类别走。
                        body = simplify(body)
                        print(
                            f"{DIM}{stamp}{RESET} "
                            f"{DIM}{name[:-4]:>6}{RESET}  {colorize(body)}"
                        )
                        emitted = True

            if not emitted and time.time() - last_heartbeat > 60:
                last_heartbeat = time.time()
                if not seen_any:
                    print(
                        f"{DIM}{time.strftime('%H:%M:%S')}  等待中——还没看到 "
                        f"{args.keyword}。游戏进对局了吗？自治层默认自动接管。{RESET}"
                    )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        print(f"{DIM}已停止。{'共捕获若干条主脑日志。' if seen_any else '本次没抓到主脑日志。'}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
