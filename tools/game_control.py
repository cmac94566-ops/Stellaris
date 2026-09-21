#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stellaris 启停控制台（tools/game_control.py）

目标：让智能体自己完成「关游戏 → 开游戏 → 验证新代码生效」，用户不再手动重启。

本机实测事实（2026-09-16，区分确证与推断）
------------------------------------------------------------
· 游戏本体    D:/SteamLibrary/steamapps/common/Stellaris/stellaris.exe
· **mod 真源**  Documents/Paradox Interactive/Stellaris/dlc_load.json
               —— 由**启动器写、游戏引擎读**。已核实 7 个 mod 在列（含
                  mod/stellaris_overmind.mod）。⇒ **直接启动 exe 同样加载 mod**，
                  不必先让启动器 GUI 跑一遍。（确证）
· **启动参数**  `-continuelastsave`
               —— exe 字符串确证，挂在 `frontendidler`（主菜单空转逻辑）下，
                  启动后自动载入最新存档，跳过「继续游戏」点击。（确证）
· 优雅关闭    `taskkill <PID>`（**不带 /F**）向窗口发 WM_CLOSE，游戏正常
               存档退出；带 `/F` 才是强杀。（Windows 行为）
· 启动器本体  `bootstrapper-v2.exe` / `Paradox Launcher.exe`（AppData）
               与游戏目录的 `dowser.exe`（接受 `--gameDir`）。
               启动器只负责写 dlc_load.json 与拉起游戏 ⇒ 常规流程用不到它；
               仅当需要改**播放集**（启停 mod）时才必须走它。（确证）
· `--skip-launcher`  社区说法（填在 Steam 启动项里），**本机 exe 字符串未命中**，
               等级：推断，未采用。

用法
------------------------------------------------------------
  python tools/game_control.py status                 # 进程/会话/最新存档/mod
  python tools/game_control.py stop                   # 优雅关闭（等自动存档）
  python tools/game_control.py start [--continue]     # 启动；--continue 载入最新存档
  python tools/game_control.py restart                # stop + start --continue
  python tools/game_control.py verify                 # 查本会话 OVERMIND 标记 + error.log
  python tools/game_control.py newgame --save <路径>  # 把指定存档置为「最新」后启动
  python tools/game_control.py launcher               # 启动启动器 GUI（改播放集用）

只读原则：本工具不修改、不删除用户存档；`newgame` 只做**复制**。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

GAME_DIR = Path(r"D:/SteamLibrary/steamapps/common/Stellaris")
EXE = GAME_DIR / "stellaris.exe"
DOWser = GAME_DIR / "dowser.exe"
DOCS = Path(os.path.expanduser("~")) / "Documents/Paradox Interactive/Stellaris"
LOG_DIR = DOCS / "logs"
SAVE_DIR = DOCS / "save games"
DLC_LOAD = DOCS / "dlc_load.json"
LAUNCHER = Path(os.path.expanduser("~")) / (
    "AppData/Local/Programs/Paradox Interactive/launcher/bootstrapper-v2.exe"
)
#: 双智能体协同信箱（重启后按用户要求自动通报 zcode）
REPO_ROOT = Path(__file__).resolve().parent.parent
INBOX_ZCODE = REPO_ROOT / "HANDOFF/inbox_zcode.md"

#: 游戏正常退出时的存档写入可能长达数十秒（大帝国更久）。
STOP_TIMEOUT_S = 120


# --------------------------------------------------------------------------
# 基础查询
# --------------------------------------------------------------------------
def game_pids() -> list[int]:
    """返回 stellaris.exe 的 PID 列表（无则空）。

    用 **Windows API 直接枚举**，不依赖 `tasklist`：受限执行环境下外部命令
    可能被拦（stdout=None）或直接查不到，会导致误判「游戏未运行」而不去关闭。
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE or not snap:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids: list[int] = []
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "stellaris.exe":
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids


def session_start() -> str | None:
    """game.log 首行时间戳（= 本次进程启动时刻）。"""
    f = LOG_DIR / "game.log"
    if not f.exists():
        return None
    with f.open("r", encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    m = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", first)
    return m.group(1) if m else None


def newest_save() -> Path | None:
    """最新修改的存档（游戏 `-continuelastsave` 会选的正是它）。"""
    if not SAVE_DIR.exists():
        return None
    files = [p for p in SAVE_DIR.rglob("*.sav") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def save_date(path: Path) -> str:
    """存档内的游戏日期（读 zip 里的 gamestate 头部，失败则返回 '?'）。"""
    try:
        with zipfile.ZipFile(path) as z:
            head = z.read("gamestate")[:400].decode("utf-8", "replace")
        m = re.search(r'date="([^"]+)"', head)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def mods_enabled() -> list[str]:
    import json
    try:
        d = json.loads(DLC_LOAD.read_text(encoding="utf-8"))
        return list(d.get("enabled_mods", []))
    except Exception:
        return []


# --------------------------------------------------------------------------
# 动作
# --------------------------------------------------------------------------
def cmd_status() -> int:
    pids = game_pids()
    print(f"游戏进程    : {'运行中 PID ' + ', '.join(map(str, pids)) if pids else '未运行'}")
    print(f"本次会话起点: {session_start() or '（无 game.log）'}")
    s = newest_save()
    if s:
        age = int((time.time() - s.stat().st_mtime) / 60)
        print(f"最新存档    : {s.relative_to(SAVE_DIR)}  游戏内 {save_date(s)}  （{age} 分钟前）")
    else:
        print("最新存档    : 无")
    mods = mods_enabled()
    has_ours = any("overmind" in m for m in mods)
    print(f"启用 mod    : {len(mods)} 个；主脑 mod {'在列 ✅' if has_ours else '不在列 ❌'}")
    print(f"exe 存在    : {EXE.exists()}   |  dlc_load.json 存在: {DLC_LOAD.exists()}")
    return 0


def cmd_stop() -> int:
    pids = game_pids()
    if not pids:
        print("游戏未运行，无需关闭。")
        return 0
    print(f"请求关闭 PID {pids}（游戏会写退出存档）…")
    # 优先直接向游戏主窗口投递 WM_CLOSE（等同点右上角 ×，游戏正常退出）。
    # 不依赖 taskkill：受限执行环境下外部命令可能被拦。
    import ctypes
    u32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    hwnd = u32.FindWindowW(None, "Stellaris")
    if hwnd:
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        print("  已向窗口投递 WM_CLOSE")
    else:
        print("  找不到游戏窗口，退回 taskkill（不带 /F = 请求式关闭）")
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, text=True,
                           creationflags=0x08000000)
    t0 = time.time()
    while time.time() - t0 < STOP_TIMEOUT_S:
        if not game_pids():
            print(f"已退出，用时 {time.time() - t0:.0f}s。")
            return 0
        time.sleep(3)
    print(f"⚠️ {STOP_TIMEOUT_S}s 仍未退出（可能卡在存档对话框）。")
    print("   未强杀 —— 请人工确认游戏窗口是否有弹窗；如需强杀： "
          + "taskkill /F /PID " + " ".join(map(str, pids)))
    return 1


def cmd_start(continue_save: bool) -> int:
    if game_pids():
        print("游戏已在运行，忽略 start。")
        return 0
    if not EXE.exists():
        print(f"❌ 找不到 {EXE}")
        return 1
    args = [str(EXE), "-gdpr-compliant"]
    if continue_save:
        # ⚠️ 必须是**双横线** `--continuelastsave`：启动器日志里它实际传的是
        # `--continuelastsave,-gdpr-compliant`（单横线版本实测不起作用）。
        args.append("--continuelastsave")
    # ⚠️ 必须注入 Steam 环境变量：直接跑 exe 时缺 SteamAppId 会在
    # 「defines loaded」之后立刻退出（实测两次复现）。补上后稳定运行。
    env = os.environ.copy()
    env.update({
        "SteamAppId": "281990",
        "SteamGameId": "281990",
        "SteamOverlayGameId": "281990",
    })
    print(f"启动: {' '.join(args)}")
    print(f"   cwd = {GAME_DIR}   （已注入 SteamAppId=281990）")
    try:
        subprocess.Popen(args, cwd=str(GAME_DIR), env=env, close_fds=True)
    except Exception as e:  # pragma: no cover
        print(f"❌ 启动失败：{e}")
        return 1
    # 等进程出现（Steam DRM 校验需要几秒）
    t0 = time.time()
    while time.time() - t0 < 60:
        if game_pids():
            print(f"进程已起（{time.time() - t0:.0f}s）。游戏加载 mod 需要 1-3 分钟。")
            if continue_save:
                s = newest_save()
                print(f"将自动载入最新存档：{s.relative_to(SAVE_DIR) if s else '?'}")
            return 0
        time.sleep(2)
    print("⚠️ 60s 内未出现进程。请确认 Steam 在运行（DRM 校验需要）。")
    return 1


def cmd_launcher() -> int:
    """启动启动器 GUI（仅当要改播放集/启停 mod 时需要）。"""
    if not LAUNCHER.exists():
        print(f"❌ 找不到启动器 {LAUNCHER}")
        return 1
    print(f"启动启动器 GUI：{LAUNCHER}")
    subprocess.Popen([str(LAUNCHER)], close_fds=True)
    print("（启动器内改动播放集后会写回 dlc_load.json；改完点 PLAY 或关闭即可。）")
    return 0


def cmd_verify() -> int:
    """验证当前会话：OVERMIND 标记 + error.log 里的 mod 报错。"""
    if not game_pids():
        print("⚠️ 游戏未运行 —— 验证需要游戏至少启动过一次（进程内会写日志）。")
    gj = LOG_DIR / "game.log"
    ej = LOG_DIR / "error.log"
    if not gj.exists():
        print("找不到 game.log")
        return 1
    text = gj.read_text(encoding="utf-8", errors="replace")
    n_phase = text.count("【相位")
    n_attr = text.count("【归因OK】")
    n_warn = text.count("【告警】")
    print(f"会话起点: {session_start()}   游戏日志行数: {len(text.splitlines())}")
    print(f"【相位 N/8】: {n_phase}   【归因OK】: {n_attr}   【告警】: {n_warn}")
    if n_phase == 0:
        print("  ⚠️ 零相位标记 ⇒ 本局跑的是 D-7 之前的代码，或 mod 未加载。")
    elif n_warn == 0:
        print("  ✅ 相位在线且零告警 ⇒ 归因干净（F3' 前置条件成立）。")
    if ej.exists():
        etext = ej.read_text(encoding="utf-8", errors="replace")
        checks = {
            "Invalid relative power token": "D-24（zcode 已修，需新会话复验）",
            "Invalid macro": "D-21（已知噪音，宏运行时正常）",
        }
        for key, label in checks.items():
            n = etext.count(key)
            print(f"error.log 中 {key!r}: {n} 条  ← {label}")
    return 0


def notify_zcode(topic: str, detail_lines: list[str]) -> None:
    """按 HANDOFF/README 的五字段格式，往 zcode 信箱末尾追加一条通报。

    用户要求（2026-09-16）：「游戏重启了记得通知 zcode」。原因是
    Clausewitz **只在进程启动时加载 mod** —— 重启是「新代码生效」的唯一
    时刻，也正好是 zcode 关心的实机复验窗口（例如 D-24 的 error.log 复核）。
    """
    if not INBOX_ZCODE.exists():
        print(f"（未找到 {INBOX_ZCODE}，跳过通报 zcode）")
        return
    # 2026-09-18（机制 v2）：**取号与写入一律委托 `mail_send.py`** —— 本函数不再自己算号。
    #
    # 踩坑记录：此处曾自建**第二套**取号逻辑（扫 inbox + archive 取 `max+1`），
    # 未排除 `900+` 保留段 ⇒ 被 archive 里残留的破局信标 `MSG-900` 拉高，
    # 于 21:03 发出 `WDMSG-901`（**正常号应为 060**）。
    # ⇒ **协议必须单一真相源**：两套取号逻辑迟早不一致（同一天已在 mail_send 修过同款问题）。
    import subprocess
    import sys

    body = "\n".join(f"  {line}" for line in detail_lines)
    sender = Path(__file__).resolve().parent / "mail_send.py"
    r = subprocess.run(
        [sys.executable, str(sender), "--subject", f"[例行] {topic}", "--body", body],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        creationflags=0x08000000,   # 2026-09-20：宿主无控制台时不闪黑窗
    )
    out = (r.stdout or "").strip()
    print(out if out else "（通知 zcode：无输出）")
    if (r.stderr or "").strip():
        print((r.stderr or "").strip())
    # b458835 残留修复（2026-09-18 zcode）：委托 mail_send 后 msg_id 已不存在，
    # 此处原 print 引用悬空变量令 restart 链在 notify 步崩溃（游戏停了没起）。
    # 编号由 mail_send 输出自带，不再重复打印。


def cmd_newgame(save: str) -> int:
    """把指定存档置为「最新」后启动 —— 用 `-continuelastsave` 等效「开新局」。

    原理：`-continuelastsave` 载入**最新修改**的存档。所以只要把准备好的
    开局存档复制进 save games 并让它的 mtime 成为最新，启动即进该局。
    只用复制，不动原文件。
    """
    src = Path(save)
    if not src.exists():
        print(f"❌ 找不到存档 {src}")
        return 1
    if game_pids():
        print("⚠️ 游戏正在运行 —— 请先 stop，否则新存档不会被载入。")
        return 1
    dst_dir = SAVE_DIR / "_om_newgame"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    os.utime(dst, None)  # 确保 mtime = 现在（成为最新）
    print(f"已复制为最新存档：{dst.relative_to(SAVE_DIR)}（游戏内 {save_date(dst)}）")
    return cmd_start(continue_save=True)


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stellaris 启停控制（智能体自用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("stop")
    p_start = sub.add_parser("start")
    p_start.add_argument("--continue", dest="cont", action="store_true",
                         help="带 -continuelastsave，自动载入最新存档")
    p_restart = sub.add_parser("restart")
    # 2026-09-18：restart 后**默认不通知** zcode（重启已由 zcode 自办，见 restart 分支注释）。
    # 确需通知时显式加 --notify。
    p_restart.add_argument("--notify", action="store_true",
                           help="重启成功后额外通报 zcode（默认关闭；zcode 自办重启无须此通知）")
    sub.add_parser("verify")
    sub.add_parser("launcher")
    p_notify = sub.add_parser("notify", help="手动向 zcode 信箱通报一条消息")
    p_notify.add_argument("--topic", required=True)
    p_notify.add_argument("--detail", action="append", default=[],
                          help="可多次传入，每条一行")
    p_new = sub.add_parser("newgame")
    p_new.add_argument("--save", required=True, help="开局存档路径")
    args = ap.parse_args(argv)

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "stop":
        return cmd_stop()
    if args.cmd == "start":
        return cmd_start(args.cont)
    if args.cmd == "restart":
        rc = cmd_stop()
        if rc != 0:
            return rc
        rc = cmd_start(continue_save=True)
        if rc == 0 and getattr(args, "notify", False):
            # 2026-09-18 归属订正（重要）：
            # 本段原为**无条件**通知，源于用户 2026-09-16 的要求「游戏重启了记得通知 zcode」
            # —— 当时重启由 WorkBuddy 侧执行，通知署名 `WorkBuddy → zcode` 是对的。
            # 但 21:0x 起「重启改由 zcode 自办」（用户裁定）⇒ 同一个共享工具再发通知，
            # 就变成**用我方署名、烧我方 WDMSG 号，发一条 zcode 自己动作产生、且内容还陈旧的通知**
            # （实测 WDMSG-069：署名 WorkBuddy，实为 zcode 重启所触发，且自报会话起点为重启前的 21:51:45）。
            # 这与「第二套取号逻辑」（WDMSG-901）同族：**共享工具里写死了单一发送方**。
            # ⇒ 默认改为**不通知**；确需通知时显式加 `--notify`。
            #    zcode 自办重启无须此通知（它自己知道），其复验窗口应由它自己发 ZCMSG。
            s = newest_save()
            notify_zcode(
                "游戏已重启（新代码已加载，实机复验窗口开启）",
                [
                    f"会话起点（game.log 首行）：{session_start() or '?'}",
                    f"自动载入存档：{s.relative_to(SAVE_DIR) if s else '（无）'}",
                    f"启用 mod：{len(mods_enabled())} 个",
                    "请复核 error.log 的 'Invalid relative power token' 是否清零（D-24）；"
                    "新一轮实机数据我会写入 docs/胜利进度追踪.md",
                ],
            )
        return rc
    if args.cmd == "verify":
        return cmd_verify()
    if args.cmd == "launcher":
        return cmd_launcher()
    if args.cmd == "notify":
        notify_zcode(args.topic, args.detail or ["（无详情）"])
        return 0
    if args.cmd == "newgame":
        return cmd_newgame(args.save)
    return 2


if __name__ == "__main__":
    sys.exit(main())
