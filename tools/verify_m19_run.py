"""在一段存活窗口内跑完 M1-9 验证：启动 → 关弹窗 → 载入 → 取消暂停 → 观察 → 退出。

为什么必须挤在一个脚本里
------------------------
这个沙箱会在启动它的命令结束时清理所有子孙进程，``DETACHED_PROCESS`` 也挡不住
（实测：detached 启动后 6 秒进程就没了，日志停在初始化阶段）。所以游戏只能在
**一个后台任务的存活期内**跑。凡是需要 GUI 交互的步骤，必须在这个窗口里做完。

为什么用 PostMessage 而不是 SendInput
------------------------------------
``SendInput`` 需要目标窗口是**前台**窗口，而这个会话里宿主窗口（WorkBuddy）会不断
把前台抢回去 —— 实测 ``SetForegroundWindow`` 返回 True 之后，点击仍然落在宿主上。
``PostMessage(WM_LBUTTONDOWN/UP)`` 直接把消息投递到目标 hwnd，**不要求前台**，
坐标用的是客户区坐标。游戏全屏且客户区原点在 (0,0)，所以客户区坐标 == 屏幕坐标
（已用 DPI 感知探针确认：GetClientRect = 2560x1440，客户区原点 0,0）。

坐标从 2560x1440 截图直接量取，不做缩放推算。
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

EXE = Path(r"D:\SteamLibrary\steamapps\common\Stellaris\stellaris.exe")
LOGS = Path(r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/logs")
SAVES = Path(r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games")

u = ctypes.windll.user32
k = ctypes.windll.kernel32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_window(timeout: float = 120.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits: list[int] = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(h, l):
            if u.IsWindowVisible(h) and u.GetWindowTextLengthW(h):
                b = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(h, b, 256)
                r = wintypes.RECT()
                u.GetWindowRect(h, ctypes.byref(r))
                if b.value == "Stellaris" and r.right > 0 and r.bottom > 0:
                    hits.append(h)
            return True

        u.EnumWindows(CB(cb), 0)
        if hits:
            return hits[0]
        time.sleep(2)
    raise SystemExit("等不到游戏窗口")


def post_click(hwnd: int, x: int, y: int, label: str = "") -> None:
    """把点击直接投递到窗口，不需要前台。"""
    lp = (y << 16) | (x & 0xFFFF)
    u.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.08)
    u.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.06)
    u.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    log(f"  PostMessage 点击 {label} ({x},{y})")


def post_key(hwnd: int, vk: int, label: str = "") -> None:
    ldown = 1 | (u.MapVirtualKeyW(vk, 0) << 16)
    lup = ldown | (1 << 30) | (1 << 31)
    u.PostMessageW(hwnd, 0x0100, vk, ldown)
    time.sleep(0.05)
    u.PostMessageW(hwnd, 0x0101, vk, lup)
    log(f"  PostMessage 按键 {label}")


def main() -> int:
    run_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300

    log("启动游戏（带上 -game_paused false）...")
    proc = subprocess.Popen(
        [str(EXE), "-continuelastsave", "-game_paused", "false"],
        cwd=str(EXE.parent),
        close_fds=True,
    )
    log(f"PID={proc.pid}")

    hwnd = find_window()
    log(f"窗口 hwnd={hwnd}")

    # 等 mod 脚本全部初始化完（实测约 25-50s）
    time.sleep(55)
    log(f"进程存活={proc.poll() is None}")

    # 1) 关更新说明弹窗（坐标从 2560x1440 截图量取）
    post_click(hwnd, 1925, 1114, "关闭弹窗")
    time.sleep(2.5)

    # 2) 主菜单「载入游戏」。第一项「新游戏」约 y=290，「载入游戏」约 y=456。
    post_click(hwnd, 200, 456, "载入游戏")
    time.sleep(6)

    # 3) 存档列表里点第一个（最新存档），约在列表首行
    post_click(hwnd, 700, 400, "第一个存档")
    time.sleep(8)
    # 有些版本要点右下「载入」
    post_click(hwnd, 2050, 1250, "载入按钮")
    time.sleep(45)  # 80MB 存档载入需要时间

    # 4) 取消暂停（空格）
    for i in range(3):
        post_key(hwnd, 0x20, f"空格#{i + 1}")
        time.sleep(2)

    log(f"观察 {run_seconds}s ...")
    for i in range(run_seconds // 20):
        time.sleep(20)
        alive = proc.poll() is None
        log(f"  +{(i + 1) * 20}s 存活={alive}")
        if not alive:
            break

    if proc.poll() is None:
        log("关闭游戏")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    log("结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
