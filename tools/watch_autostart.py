"""看动态窗口随游戏自启/自关的守护进程（用户指令 2026-09-19「能不能随着游戏自启」）。

行为（轮询默认 5s，纯本机、不联网）：
- 检测游戏进程（默认 stellaris.exe）：
  - 在跑 且 监视窗口未开  → 新控制台窗口启动 tools/unified_watch.py
                            （窗口标题 Overmind Watch，由 unified_watch 自设）
  - 退出   且 监视窗口在  → 关窗口：先 taskkill 无 /F（WM_CLOSE），3s 后仍在才 /F 兜底。
                            窗口是只读日志视图、无状态可丢，/F 兜底安全；
                            **对游戏本体仍严禁 /F（会丢档）—— 两者是不同对象，勿混淆**。
- 单实例锁：logs/.watch_autostart.pid（含 PID 存活检查；Startup 自启 + 手动双开时后者直接退出）
- 窗口句柄：logs/.watch_window.pid
- 运行留痕：logs/watch_autostart.log（>256KB 自动截断）

常驻方式（二选一）：
- 开机自启：Startup 目录的 overmind_watch_autostart.bat（pythonw 静默跑本脚本，无窗口）
- 会话内：  run_in_background 挂起（退出即失效 —— 与 wait_mail 同 limitation）

用法：
    py -3.12 tools/watch_autostart.py                # 常驻
    py -3.12 tools/watch_autostart.py --interval 10
    py -3.12 tools/watch_autostart.py --selftest     # 用 ping.exe 假扮游戏，演练一轮开窗→关窗
    py -3.12 tools/watch_autostart.py --status       # 查看当前状态（不启动）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
SUP_PID = LOGS / ".watch_autostart.pid"
WIN_PID = LOGS / ".watch_window.pid"
LOG = LOGS / "watch_autostart.log"
WATCH = ROOT / "tools" / "unified_watch.py"
PY = Path(r"C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe")
if not PY.exists():
    PY = Path(sys.executable)

CREATE_NEW_CONSOLE = 0x00000010
# 2026-09-20：本守护跑在 pythonw（无控制台）下，子进程若不带此标志，
# Windows 会为**每个子进程分配新控制台窗口** => 用户看到黑窗每 5 秒闪一次。
# 所有 tasklist/taskkill/ping 调用必须带它；只有监视窗口那一处故意用 NEW_CONSOLE。
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    try:
        if LOG.exists() and LOG.stat().st_size > 256 * 1024:
            LOG.write_text(line + "\n", encoding="utf-8")
        else:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _out(r: subprocess.CompletedProcess) -> str:
    # tasklist/taskkill 输出是控制台代码页（中文系统 = GBK）；匹配目标（进程名/PID）是
    # ASCII，宽松解码即可。曾在 text=True 下被按 utf-8 强解炸出 UnicodeDecodeError。
    return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")


def proc_running(image: str) -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % image],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    return image.lower() in _out(r).lower()


def pid_alive(pid: int) -> bool:
    r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    return str(pid) in _out(r)


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def supervisor_lock() -> bool:
    """单实例锁。返回 True=本实例可运行。"""
    if SUP_PID.exists():
        old = read_json(SUP_PID) or {}
        pid = old.get("pid")
        if isinstance(pid, int) and pid_alive(pid):
            return False
    SUP_PID.parent.mkdir(parents=True, exist_ok=True)
    SUP_PID.write_text(json.dumps({"pid": os.getpid(), "at": time.strftime("%F %T")}),
                       encoding="utf-8")
    return True


def open_window(backfill: int) -> int:
    p = subprocess.Popen(
        [str(PY), "-u", str(WATCH), "--backfill", str(backfill)],
        cwd=str(ROOT), creationflags=CREATE_NEW_CONSOLE)
    WIN_PID.write_text(json.dumps({"pid": p.pid, "at": time.strftime("%F %T")}),
                       encoding="utf-8")
    return p.pid


def close_window(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                   creationflags=CREATE_NO_WINDOW)
    time.sleep(3)
    if pid_alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        log("窗口 %d 未响应 WM_CLOSE，已 /F 兜底（只读窗口，无状态可丢）" % pid)
    try:
        WIN_PID.unlink()
    except FileNotFoundError:
        pass


def status() -> int:
    game = proc_running("stellaris.exe")
    sup = read_json(SUP_PID) or {}
    sup_ok = isinstance(sup.get("pid"), int) and pid_alive(sup["pid"])
    win = read_json(WIN_PID) or {}
    win_ok = isinstance(win.get("pid"), int) and pid_alive(win["pid"])
    print("游戏 stellaris.exe : %s" % ("运行中" if game else "未运行"))
    print("守护进程           : %s%s" % ("运行中 pid=%s" % sup["pid"] if sup_ok else "未运行",
                                          "（Startup 自启）" if sup_ok else ""))
    print("监视窗口           : %s" % ("开着 pid=%s" % win["pid"] if win_ok else "未开"))
    return 0


def selftest() -> int:
    """用 ping.exe 假扮游戏，演练一轮 开窗→游戏退→关窗。不碰 Stellaris。"""
    log("[selftest] 开始：ping.exe 假扮游戏（约 14s）")
    dummy = subprocess.Popen(["ping", "127.0.0.1", "-n", "14"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
    proc, opened, closed = "ping.exe", False, False
    deadline = time.time() + 40
    try:
        while time.time() < deadline:
            w = read_json(WIN_PID) or {}
            win_open = isinstance(w.get("pid"), int) and pid_alive(w["pid"])
            if proc_running(proc) and not win_open and not opened:
                pid = open_window(6)
                opened = True
                log("[selftest] 游戏在跑且窗口未开 → 已开窗 pid=%d" % pid)
            if not proc_running(proc) and win_open and not closed:
                close_window(w["pid"])
                closed = True
                log("[selftest] 游戏已退 → 已关窗")
            if opened and closed:
                break
            time.sleep(2)
    finally:
        try:
            dummy.kill()
        except Exception:
            pass
    ok = opened and closed
    log("[selftest] 结果: %s（开窗=%s 关窗=%s）" % ("通过" if ok else "未通过", opened, closed))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="看动态窗口随游戏自启/自关守护")
    ap.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    ap.add_argument("--backfill", type=int, default=12, help="窗口回填行数")
    ap.add_argument("--selftest", action="store_true", help="用 ping.exe 演练一轮开窗/关窗")
    ap.add_argument("--status", action="store_true", help="只看当前状态")
    a = ap.parse_args(argv)

    if a.status:
        return status()
    if a.selftest:
        return selftest()

    if not supervisor_lock():
        print("已有守护实例在跑（logs/.watch_autostart.pid），本实例退出。")
        return 0
    log("守护启动（interval=%.0fs，目标=stellaris.exe）" % a.interval)
    try:
        while True:
            try:
                w = read_json(WIN_PID) or {}
                win_open = isinstance(w.get("pid"), int) and pid_alive(w["pid"])
                game = proc_running("stellaris.exe")
                if game and not win_open:
                    pid = open_window(a.backfill)
                    log("游戏在跑且窗口未开 → 开窗 pid=%d" % pid)
                elif not game and win_open:
                    close_window(w["pid"])
                    log("游戏已退 → 关窗")
            except Exception as e:
                log("轮询异常（继续）: %r" % e)
            time.sleep(a.interval)
    finally:
        try:
            SUP_PID.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
