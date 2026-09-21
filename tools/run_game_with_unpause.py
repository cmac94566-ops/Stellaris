"""一次完成：启动游戏 → 取消暂停 → 等 N 秒 → 关闭。

为什么必须打包成一个原子操作：本环境里后台任务有生存时限，游戏进程一旦
脱离宿主就会被回收；而"启动"和"取消暂停"如果分两次工具调用，中间那次
调用返回时宿主就死了，游戏跟着没了。所以全部塞进同一个后台任务。

取消暂停只用一次键盘输入（SendInput 空格），不做截图/坐标点击。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

EXE = r"D:\SteamLibrary\steamapps\common\Stellaris\stellaris.exe"
WORKDIR = r"D:\SteamLibrary\steamapps\common\Stellaris"
RUN_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 100

u = ctypes.windll.user32
k = ctypes.windll.kernel32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def find_window(timeout_s: float = 90.0) -> int:
    """轮询等待可见的 Stellaris 主窗口出现。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found = []

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(h, l):
            if u.IsWindowVisible(h):
                n = u.GetWindowTextLengthW(h)
                if n:
                    b = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(h, b, n + 1)
                    r = wintypes.RECT()
                    u.GetWindowRect(h, ctypes.byref(r))
                    # 只在屏幕内的窗口才算主窗口
                    if b.value == "Stellaris" and r.right > 0 and r.bottom > 0:
                        found.append(h)
            return True

        u.EnumWindows(CB(cb), 0)
        if found:
            return found[0]
        time.sleep(2)
    raise SystemExit("等不到 Stellaris 主窗口")


def force_foreground(hwnd: int) -> bool:
    fg = u.GetForegroundWindow()
    if fg == hwnd:
        return True
    cur = k.GetCurrentThreadId()
    tgt = u.GetWindowThreadProcessId(fg, None)
    u.AttachThreadInput(cur, tgt, True)
    u.BringWindowToTop(hwnd)
    u.SetForegroundWindow(hwnd)
    u.SetFocus(hwnd)
    u.AttachThreadInput(cur, tgt, False)
    time.sleep(0.5)
    return u.GetForegroundWindow() == hwnd


VK_SPACE = 0x20
VK_RETURN = 0x0D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    _anonymous_ = ("_u",)
    _fields_ = [("type", wintypes.DWORD), ("_u", _U)]


def send_key(vk: int) -> None:
    arr = (INPUT * 2)()
    arr[0].type = 1
    arr[0].ki.wVk = vk
    arr[1].type = 1
    arr[1].ki.wVk = vk
    arr[1].ki.dwFlags = 2  # KEYEVENTF_KEYUP
    u.SendInput(2, arr, ctypes.sizeof(INPUT))


def fresh_log_mtime() -> float:
    import os

    p = r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/logs/game.log"
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def main() -> int:
    print("启动游戏...", flush=True)
    proc = subprocess.Popen([EXE, "-continuelastsave"], cwd=WORKDIR)
    print(f"PID={proc.pid}", flush=True)

    hwnd = find_window()
    print(f"窗口 hwnd={hwnd}", flush=True)

    # 等引擎把 mod 脚本全部初始化完（实测约 25s）。
    time.sleep(50)

    ok = force_foreground(hwnd)
    print(f"前台化成功={ok}", flush=True)

    # 关键：``-continuelastsave`` 只是**准备好**存档路径，游戏仍然停在主菜单。
    # 必须敲一次 Enter 才真的载入。空格在主菜单没有任何作用（实测踩过：
    # 4 次空格全打在主菜单上，游戏完全没动，日志停在初始化完成那一刻）。
    print("按 Enter 载入存档...", flush=True)
    send_key(VK_RETURN)
    time.sleep(3)
    send_key(VK_RETURN)

    # 载入 80MB 存档需要时间；这段时间游戏在跑 IO，不是在菜单。
    print("等待载入完成...", flush=True)
    time.sleep(60)

    # 载入后游戏是暂停的，取消暂停才会走月度脉冲。
    print("取消暂停...", flush=True)
    for i in range(3):
        force_foreground(hwnd)
        send_key(VK_SPACE)
        print(f"  空格 #{i + 1}", flush=True)
        time.sleep(3)

    print(f"保持运行 {RUN_SECONDS}s...", flush=True)
    time.sleep(RUN_SECONDS)

    if proc.poll() is None:
        print("关闭游戏", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
