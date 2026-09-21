"""邮箱实时提醒 —— 常驻监听 HANDOFF/ 双信箱，有新件立刻响铃并更新窗口标题。

背景（2026-09-17 用户指令「我想随时接收，你这个太久了」）：
自动化调度器最细只支持**每小时**，做不到"随时"。故改用常驻监听：
邮件一落盘就在几秒内被发现，响铃 + 标题栏显示未读数，用户随时能看到。

与既有工具的分工：
- `tools/mailbox_watch.py` —— 每 20 分钟打印一次摘要（被动，看历史）
- `tools/mail_alert.py` —— **常驻**，只在**有新件**时报警（主动，抓变化）
- 自动化「Overmind 邮箱巡检」—— 每小时**处理**邮件（LLM 回执/归档）

用法::

    py -3.12 tools/mail_alert.py                # 常驻（默认每 5 秒扫一次）
    py -3.12 tools/mail_alert.py --interval 3   # 更密
    py -3.12 tools/mail_alert.py --once         # 只查一次就退出（供定时任务）
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HANDOFF = REPO / "HANDOFF"
INBOX_WB = HANDOFF / "inbox_workbuddy.md"   # zcode 写给我方（收件）
INBOX_ZC = HANDOFF / "inbox_zcode.md"       # 我方写给 zcode（发件 + 其回执）

MSG_RE = re.compile(r"^## \[([A-Z]*MSG-\d+)\]\s*(.*)$")
TOPIC_RE = re.compile(r"^- 主题:\s*(.*)$")

# ---- 门哨兵（2026-09-17 新增；同日升级为「存在 + 正确性」双查）----
# 背景：zcode 报告 `autonomy.txt` 的写入会被编辑器缓冲区反复覆盖（写盘振荡）。
# 我方无权限关闭用户编辑器的标签页，故改为**监测**：一旦满仓抑制门数量不足，
# 立刻告警。这样即使编辑器再覆盖一次，也能被及时发现并请 zcode 重落。
#
# 升级原因（2026-09-17 深夜，zcode 实机取证）：只数「门在不在」是**弱校验** ——
# 当时 4 道门都在，却因 `resource_stockpile_percent` 是**国家 scope 触发器**、
# 从星球 scope 直接调用而报 `Wrong scope` **72 条**，门实际没生效，哨兵毫无反应。
# ⇒ 现在同时查 error.log 中涉 `overmind_autonomy.txt` 的 Wrong scope。
GUARD_FILE = REPO / "mod/stellaris_overmind/common/scripted_effects/overmind_autonomy.txt"
GUARD_NEEDLE = "resource_stockpile_percent"
GUARD_EXPECT = 4          # 满仓抑制门应有 4 道（RELIEF 发电/农业 × 两处）
GUARD_EVERY = 12          # 每 12 次扫描检查一次（约 1 分钟）
GAME_ERR = Path("C:/Users/<user>/Documents/Paradox Interactive/Stellaris/logs/error.log")


def _scope_errors() -> int:
    """本局 error.log 中涉 overmind_autonomy.txt 的 Wrong scope 条数（读取失败按 0 计）。"""
    try:
        txt = GAME_ERR.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for ln in txt.splitlines()
               if "Wrong scope" in ln and "overmind_autonomy" in ln)

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"


def enable_ansi() -> None:
    if os.name != "nt":
        return
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


def beep(kind: str = "alert") -> None:
    """新件响铃。两种提示音区分：收件用告警音、回执用提示音。"""
    try:
        import winsound
        winsound.MessageBeep(
            winsound.MB_ICONEXCLAMATION if kind == "alert" else winsound.MB_ICONASTERISK
        )
    except Exception:
        sys.stdout.write("\a")
        sys.stdout.flush()


def read_msgs(path: Path) -> dict[str, str]:
    """返回 {MSG 编号: 主题}。"""
    try:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    cur = ""
    for line in text.splitlines():
        m = MSG_RE.match(line)
        if m:
            cur = m.group(1)
            out[cur] = ""
            continue
        t = TOPIC_RE.match(line)
        if t and cur:
            out[cur] = t.group(1).strip()
    return out


def pending(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        return len(re.findall(r"(?m)^-\s*状态:\s*待处理\s*$", path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


_guard_state = {"ok": None}


def check_guard() -> None:
    """门哨兵：满仓抑制门「存在 + 正确性」双查，异常时告警。只在状态**变化**时出声。"""
    try:
        n = GUARD_FILE.read_text(encoding="utf-8", errors="replace").count(GUARD_NEEDLE)
    except OSError:
        return
    se = _scope_errors()
    ok = (n >= GUARD_EXPECT) and (se == 0)
    detail = f"{n}/{GUARD_EXPECT} 道" + (f"、Wrong scope {se} 条" if se else "")
    if _guard_state["ok"] is None:          # 首次只建基线
        _guard_state["ok"] = ok
        if not ok:
            print(f"{YELLOW}[哨兵] 注意：抑制门 {detail}{RESET}")
        return
    if ok != _guard_state["ok"]:
        _guard_state["ok"] = ok
        if ok:
            print(f"{GREEN}[哨兵] 门已恢复：{detail}{RESET}")
        else:
            print(f"\n{BOLD}{RED}⚠️ [哨兵] 满仓抑制门异常：{detail}{RESET}")
            if n < GUARD_EXPECT:
                print(f"{DIM}   → 门被覆盖，大概率是编辑器缓冲区自动保存所致，请让 zcode 重落{RESET}")
            if se:
                print(f"{DIM}   → 门在但报 Wrong scope（scope 用错，门实际未生效）；"
                      f"查 error.log 涉 overmind_autonomy.txt 的行{RESET}")
            beep("alert")


def refresh_title() -> None:
    wb = pending(INBOX_WB)
    mark = "●" if wb else "○"
    guard = "" if _guard_state["ok"] in (None, True) else " ⚠门"
    title = f"{mark} 邮箱 {wb} 未读{guard} — Overmind" if wb else f"{mark} 邮箱无未读{guard} — Overmind"
    set_title(title)


def scan(known_wb: set, known_zc: set, first: bool) -> None:
    """扫一次；first=True 时只建基线不报警（避免启动瞬间误报）。"""
    cur_wb = read_msgs(INBOX_WB)
    cur_zc = read_msgs(INBOX_ZC)

    new_wb = [m for m in cur_wb if m not in known_wb]
    new_zc = [m for m in cur_zc if m not in known_zc]

    if not first:
        for m in new_wb:
            print(f"\n{BOLD}{RED}▶ 收到新件 {m}{RESET}  {cur_wb.get(m, '')}")
            print(f"{DIM}   → 在 inbox_workbuddy.md；等我一小时内的巡检处理，或现在叫我{RESET}")
        if new_wb:
            beep("alert")
        for m in new_zc:
            print(f"\n{BOLD}{GREEN}▶ zcode 有新动作 {m}{RESET}  {cur_zc.get(m, '')}")
        if new_zc and not new_wb:
            beep("info")

    known_wb.update(cur_wb.keys())
    known_zc.update(cur_zc.keys())
    refresh_title()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="邮箱实时提醒（常驻监听双信箱）")
    ap.add_argument("--interval", type=float, default=5.0, help="扫描间隔秒（默认 5）")
    ap.add_argument("--once", action="store_true", help="只查一次就退出")
    args = ap.parse_args(argv)

    enable_ansi()
    print(f"{BOLD}邮箱实时提醒{RESET} {DIM}—— 有新件立刻响铃{RESET}")
    print(f"{DIM}监视: {INBOX_WB.name} / {INBOX_ZC.name}{RESET}")
    wb0 = read_msgs(INBOX_WB)
    zc0 = read_msgs(INBOX_ZC)
    print(f"{DIM}当前: 收件 {len(wb0)} 封（未读 {pending(INBOX_WB)}） / "
          f"发件 {len(zc0)} 封{RESET}")
    print(f"{DIM}Ctrl+C 退出{RESET}\n")

    seen_wb = set(wb0.keys())
    seen_zc = set(zc0.keys())
    refresh_title()

    if args.once:
        scan(seen_wb, seen_zc, first=False)
        check_guard()
        return 0

    tick = 0
    try:
        while True:
            time.sleep(args.interval)
            scan(seen_wb, seen_zc, first=False)
            tick += 1
            if tick % GUARD_EVERY == 0:      # 约每分钟查一次门
                check_guard()
                refresh_title()
            sys.stdout.flush()
    except KeyboardInterrupt:
        print(f"\n{DIM}已退出。{RESET}")
        set_title("邮箱提醒已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
