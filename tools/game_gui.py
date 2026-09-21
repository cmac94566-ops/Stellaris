#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stellaris GUI 控制（tools/game_gui.py）

为什么需要它：Stellaris 用 Clausewitz 自绘 UI，**无障碍接口读不到控件**，
所以「自己开新对局」只能走「截图 → 识别按钮 → 模拟点击」这条路。

实现选择：**纯 ctypes 直调 Windows API**，零第三方依赖（不装 pyautogui），
避免污染用户环境。

用法
------------------------------------------------------------
  python tools/game_gui.py focus            # 把 Stellaris 窗口激活到前台
  python tools/game_gui.py shot [--full]    # 截图（默认裁游戏窗口）→ C:/tmp/omshots/
  python tools/game_gui.py click X Y        # 在**屏幕坐标**点击
  python tools/game_gui.py key esc          # 按键（esc/enter/space/f1..f12/单字符）
  python tools/game_gui.py probe            # 报告窗口矩形/前台状态/DPI

安全：只发窗口激活、鼠标、按键；不改任何文件、不写游戏配置。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

u32 = ctypes.windll.user32
SHOT_DIR = Path(r"C:/tmp/omshots")
TITLE = "Stellaris"

# 让本进程 DPI 感知与窗口矩形/截图坐标一致（否则点击会整体偏移）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass

SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002

VK = {
    "esc": 0x1B, "enter": 0x0D, "space": 0x20, "tab": 0x09,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def find_game() -> int:
    hwnd = u32.FindWindowW(None, TITLE)
    if not hwnd:
        raise SystemExit(f"找不到窗口标题为 {TITLE!r} 的进程（游戏没开？）")
    return hwnd


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def foreground_title() -> str:
    h = u32.GetForegroundWindow()
    n = ctypes.create_unicode_buffer(256)
    u32.GetWindowTextW(h, n, 256)
    return n.value


def cmd_probe() -> int:
    try:
        h = find_game()
    except SystemExit as e:
        print(e)
        return 1
    print(f"窗口 hwnd   : {h}")
    print(f"窗口矩形    : {rect_of(h)}  (left, top, right, bottom)")
    print(f"前台窗口    : {foreground_title()!r}")
    print(f"是否前台    : {u32.GetForegroundWindow() == h}")
    return 0


def cmd_focus() -> int:
    h = find_game()
    u32.ShowWindow(h, SW_RESTORE)
    # SetForegroundWindow 对非前台进程有限制，配合 Alt 轻敲提高成功率
    u32.keybd_event(0x12, 0, 0, 0)
    u32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
    u32.SetForegroundWindow(h)
    time.sleep(0.6)
    ok = u32.GetForegroundWindow() == h
    print(f"激活 {'成功 ✅' if ok else '失败 ❌'}（前台={foreground_title()!r}）")
    return 0 if ok else 1


def cmd_shot(full: bool) -> int:
    from PIL import ImageGrab
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    img = ImageGrab.grab()
    if full:
        out = SHOT_DIR / "shot_full.png"
    else:
        h = find_game()
        l, t, r, b = rect_of(h)
        img = img.crop((l, t, r, b))
        out = SHOT_DIR / "shot_game.png"
    # 缩到宽 ≤1280，便于阅读，同时保留比例
    if img.width > 1280:
        ratio = 1280 / img.width
        img = img.resize((1280, int(img.height * ratio)), 1)
    img.save(out)
    print(f"截图已保存: {out}  尺寸={img.size}   前台={foreground_title()!r}")
    return 0


def cmd_click(x: int, y: int) -> int:
    try:
        h = find_game()
    except SystemExit as e:
        print(e)
        return 1
    if u32.GetForegroundWindow() != h:
        print("⚠️ 游戏不在前台 —— 先跑 focus，避免点到别的窗口")
        return 1
    u32.SetCursorPos(x, y)
    time.sleep(0.15)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print(f"已点击 ({x}, {y})")
    return 0


def cmd_click_ratio(rx: float, ry: float) -> int:
    """按**窗口内相对比例**点击（0~1）。

    启动器（Electron）窗口位置每次启动都在变，绝对坐标不可靠；而按钮在
    窗口内的相对位置是稳定的（实测两次截图「继续」都在 (0.453, 0.282)）。
    """
    try:
        h = find_game()
    except SystemExit as e:
        print(e)
        return 1
    l, t, r, b = rect_of(h)
    x = int(l + rx * (r - l))
    y = int(t + ry * (b - t))
    print(f"窗口矩形 {(l, t, r, b)} → 比例 ({rx}, {ry}) 折算屏幕坐标 ({x}, {y})")
    return cmd_click(x, y)


def cmd_key(name: str) -> int:
    code = VK.get(name.lower())
    if code is None:
        if len(name) == 1:
            code = u32.VkKeyScanW(ord(name)) & 0xFF
        else:
            print(f"未知按键 {name!r}；可用: {', '.join(sorted(VK))}")
            return 1
    u32.keybd_event(code, 0, 0, 0)
    time.sleep(0.05)
    u32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
    print(f"已按下 {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stellaris GUI 控制（ctypes 零依赖）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("focus")
    p_shot = sub.add_parser("shot")
    p_shot.add_argument("--full", action="store_true")
    p_click = sub.add_parser("click")
    p_click.add_argument("x", type=int)
    p_click.add_argument("y", type=int)
    p_cr = sub.add_parser("click-ratio", help="按窗口内相对比例点击（0~1）")
    p_cr.add_argument("rx", type=float)
    p_cr.add_argument("ry", type=float)
    p_key = sub.add_parser("key")
    p_key.add_argument("name")
    a = ap.parse_args(argv)

    if a.cmd == "probe":
        return cmd_probe()
    if a.cmd == "focus":
        return cmd_focus()
    if a.cmd == "shot":
        return cmd_shot(a.full)
    if a.cmd == "click":
        return cmd_click(a.x, a.y)
    if a.cmd == "click-ratio":
        return cmd_click_ratio(a.rx, a.ry)
    if a.cmd == "key":
        return cmd_key(a.name)
    return 2


if __name__ == "__main__":
    sys.exit(main())
