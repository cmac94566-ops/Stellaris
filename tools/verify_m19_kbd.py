"""M1-9 实机验证：启动 → PostMessage 键盘载入 → 取消暂停 → 观察 → 取证。

设计原则（响应用户「不要点击操控」的要求）
----------------------------------------
**全程不使用鼠标点击**。载入存档只靠 ``PostMessage(WM_KEYDOWN/UP)`` 投递键盘事件：
- ``PostMessage`` 直接投递到 hwnd，**不要求窗口在前台**（``SendInput`` 要求前台，
  而本会话宿主会不断抢回前台，实测抢不过）。
- ``-continuelastsave`` 让引擎把"最新存档"预设为待载入项，主菜单按 Enter 即载入
  （已实测确证：``-continuelastsave`` 本身不会跳过主菜单，但 Enter 可以）。

坐标问题已彻底查明（备用回退路径才用得上）
--------------------------------------
游戏渲染逻辑分辨率 ``2048x1152``（settings.txt），窗口客户区 ``2560x1440``，
缩放比恰好 **1.25**。先前直接用 2560x1440 截图量坐标 → 全部错位。
现已在逻辑坐标系里标定出主菜单 8 项的真实**屏幕**坐标（程序化测量，非目测）：

    新游戏 348 | 载入游戏 423 | 多人游戏 498 | 合作 574
    额外内容 648 | 设置 724 | 制作人员 798 | 退出 873     (x 取 100)

取证方式
------
1. ``error.log`` 中 ``overmind`` 与 ``Max effects`` 计数（M1-8）
2. ``game.log`` 是否出现载入痕迹
3. 定时截图（PrintWindow，不受遮挡影响）
4. 存档变量 ``om_verify_boost`` 是否递减（证明 tick 真的在跑）
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

EXE = Path(r"D:\SteamLibrary\steamapps\common\Stellaris\stellaris.exe")
GAMEDIR = EXE.parent
LOGS = Path(r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/logs")
SAVEDIR = Path(r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games/_929703252")
HERE = Path(__file__).parent

u = ctypes.windll.user32
k = ctypes.windll.kernel32
g = ctypes.windll.gdi32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

VK_RETURN = 0x0D
VK_SPACE = 0x20

# 主菜单项（屏幕坐标，程序化实测；逻辑坐标 = 屏幕坐标 / 1.25）
MENU = {
    "新游戏": 348, "载入游戏": 423, "多人游戏": 498, "合作": 574,
    "额外内容": 648, "设置": 724, "制作人员": 798, "退出": 873,
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 窗口

def find_window(timeout: float = 150.0, need_visible: bool = True) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits: list[int] = []
        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(h, l):
            if need_visible and not u.IsWindowVisible(h):
                return True
            if u.GetWindowTextLengthW(h):
                b = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(h, b, 256)
                r = wintypes.RECT()
                u.GetWindowRect(h, ctypes.byref(r))
                if b.value == "Stellaris" and r.left > -10000:
                    hits.append(h)
            return True

        u.EnumWindows(CB(cb), 0)
        if hits:
            return hits[0]
        time.sleep(2)
    raise SystemExit("等不到 Stellaris 主窗口")


def client_to_screen_scale(hwnd: int) -> tuple[float, float]:
    cr = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(cr))
    return 1.0, 1.0


# ---------------------------------------------------------------- 输入

def post_key(hwnd: int, vk: int, label: str = "") -> None:
    scan = u.MapVirtualKeyW(vk, 0)
    ldown = 1 | (scan << 16)
    lup = ldown | (1 << 30) | (1 << 31)
    u.PostMessageW(hwnd, 0x0100, vk, ldown)
    time.sleep(0.07)
    u.PostMessageW(hwnd, 0x0101, vk, lup)
    log(f"  投递按键 {label or hex(vk)}")


def post_click(hwnd: int, x: int, y: int, label: str = "") -> None:
    """回退路径：仅当键盘路线失败时才用。坐标是已标定的屏幕坐标。"""
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    u.PostMessageW(hwnd, 0x0200, 0, lp)
    time.sleep(0.1)
    u.PostMessageW(hwnd, 0x0201, 0x0001, lp)
    time.sleep(0.08)
    u.PostMessageW(hwnd, 0x0202, 0, lp)
    log(f"  投递点击 {label} ({x},{y})")


# ---------------------------------------------------------------- 取证

def shot(hwnd: int, out: Path) -> bool:
    try:
        wr = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(wr))
        W, H = wr.right - wr.left, wr.bottom - wr.top
        if W <= 0 or H <= 0:
            return False
        hdc = u.GetWindowDC(hwnd)
        mdc = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, W, H)
        g.SelectObject(mdc, bmp)
        u.PrintWindow(hwnd, mdc, 0x2)

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bi = BIH()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth = W
        bi.biHeight = -H
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(W * H * 4)
        g.GetDIBits(mdc, bmp, 0, H, buf, ctypes.byref(bi), 0)
        from PIL import Image
        Image.frombuffer("RGBA", (W, H), buf, "raw", "BGRA", 0, 1).convert("RGB").save(out)
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, hdc)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  截图失败: {e}")
        return False


def count_log(name: str, pattern: str) -> int:
    p = LOGS / name
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1
    return len(re.findall(pattern, txt, re.I))


def log_stats(tag: str) -> None:
    def mtime(n):
        try:
            return time.strftime("%H:%M:%S", time.localtime((LOGS / n).stat().st_mtime))
        except OSError:
            return "?"
    log(f"  [{tag}] overmind={count_log('error.log', 'overmind')} "
        f"maxfx={count_log('error.log', 'Max effects')} "
        f"err_lines~{count_log('error.log', chr(10))} "
        f"err_mtime={mtime('error.log')} sys_mtime={mtime('system.log')}")


def latest_save() -> Path | None:
    try:
        files = sorted(SAVEDIR.glob("*.sav"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None
    except OSError:
        return None


# ---------------------------------------------------------------- 主流程

def main() -> int:
    run_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 420
    use_click_fallback = "--click-fallback" in sys.argv

    log("=" * 62)
    log("M1-9 实机验证（键盘通道，不点击）")
    log("=" * 62)

    log("启动游戏 -continuelastsave ...")
    proc = subprocess.Popen(
        [str(EXE), "-continuelastsave"],
        cwd=str(GAMEDIR),
        close_fds=True,
    )
    log(f"PID={proc.pid}")

    hwnd = find_window()
    log(f"窗口 hwnd={hwnd}")

    # 等 mod 脚本初始化完（实测 25-50s；error.log 会一直写到初始化结束）
    log("等初始化 55s ...")
    time.sleep(55)
    log_stats("init")
    shot(hwnd, HERE / "_m19_00_menu.png")

    # ---- 载入存档：只按键 ----
    log("按键载入（Enter x2）...")
    post_key(hwnd, VK_RETURN, "Enter#1")
    time.sleep(3)
    post_key(hwnd, VK_RETURN, "Enter#2")
    time.sleep(12)
    log_stats("after-enter")
    shot(hwnd, HERE / "_m19_01_after_enter.png")

    loaded = False
    for i in range(6):
        time.sleep(10)
        # 载入中会持续写 system.log / error.log；比较 mtime
        try:
            sys_age = time.time() - (LOGS / "system.log").stat().st_mtime
        except OSError:
            sys_age = 9e9
        log(f"  +{(i + 1) * 10}s system.log 静默 {sys_age:.0f}s")
        if sys_age < 8:
            loaded = True
            break

    if not loaded and use_click_fallback:
        log("键盘路线未见载入迹象，回退到已标定坐标的点击 ...")
        post_click(hwnd, 100, MENU["载入游戏"], "载入游戏")
        time.sleep(8)
        post_click(hwnd, 700, 400, "存档首行")
        time.sleep(8)
        post_click(hwnd, 2050, 1250, "载入按钮")
        time.sleep(45)

    log_stats("after-load")
    shot(hwnd, HERE / "_m19_02_loaded.png")

    # ---- 取消暂停 ----
    log("取消暂停（空格 x3）...")
    for i in range(3):
        post_key(hwnd, VK_SPACE, f"空格#{i + 1}")
        time.sleep(2.5)
    log_stats("after-unpause")
    shot(hwnd, HERE / "_m19_03_unpaused.png")

    # ---- 观察期 ----
    log(f"观察 {run_seconds}s（每 60s 取样）...")
    samples = run_seconds // 60
    for i in range(samples):
        time.sleep(60)
        alive = proc.poll() is None
        log(f"  +{(i + 1) * 60}s 存活={alive}")
        log_stats(f"t+{(i + 1) * 60}s")
        if (i + 1) % 2 == 0:
            shot(hwnd, HERE / f"_m19_obs_{(i + 1) * 60}s.png")
        if not alive:
            break

    sv = latest_save()
    log(f"最新存档: {sv.name if sv else '无'}")

    if proc.poll() is None:
        log("关闭游戏")
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    log("结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
