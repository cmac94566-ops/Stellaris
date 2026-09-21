# -*- coding: utf-8 -*-
"""F3' 存档守望器：把 Stellaris 新产生的年度自动存档镜像备份到安全目录。

原因：2026-09-16 00:36 实测 _1474567734/ 内有 2220/2221/2222 三个年度存档，
00:38 目录被清空（原因未明：疑云 = 云存档同步 / 手动清理 / 游戏轮转）。
F3' 趋势判定需要完整历史，故挂此守望器：只复制、永不删除源文件。
"""
import argparse
import glob
import json
import os
import shutil
import time

SRC_GLOBS = (
    # ① Paradox 用户目录（本地存档）
    r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games/**/*.sav",
    # ② Steam 云端副本 —— **本机 autosave_tocloud=yes，当前局的存档实际只落在这里**。
    #    2026-09-18 修复：原先只扫 ①，导致镜像库对"云存档局"完全抓不到
    #    （实证：本局 4_-1439839595 云端有 2214–2218 五个档，镜像库里却只有 1 个）。
    r"C:/Program Files (x86)/Steam/userdata/*/281990/remote/save games/**/*.sav",
)
DST_DIR = r"C:/Users/<user>/WorkBuddy/2026-09-13-23-02-03/f3_saves"
GAME_LOG = r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/logs/game.log"
LOG_ARCHIVE = os.path.join(DST_DIR, "_logs")
MANIFEST = os.path.join(DST_DIR, "_manifest.json")


def load_manifest() -> dict:
    """读取跨次运行的已备份清单（src -> mtime）。

    2026-09-18 修复：原实现把 `seen` 只放在**进程内**，而自动化用 `--once` 每次新起进程
    ⇒ 每小时那一轮会把**全部源档重拷一遍**、并报成「N new backups」。
    实证：00:31 报 31、00:32 报 30 —— 同一批档被连续两轮报成"新增"，**数量假高**。
    危害：① 存档采样账目失真（把重拷当新增）；② 每轮无谓复制 ~130MB。
    ⇒ 清单落盘（JSON），让 `--once` 也能跨轮去重。
    """
    try:
        with open(MANIFEST, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_manifest(seen: dict) -> None:
    """清单落盘（原子替换，避免半写）。失败不得中断守望。"""
    try:
        tmp = MANIFEST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(seen, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, MANIFEST)
    except Exception as e:
        print("[watcher] manifest save skip:", e, flush=True)


def backup_log() -> None:
    """回捞 game.log。

    2026-09-17 教训：游戏每次重启会**清空** game.log（新会话从空开始）。
    当晚复盘「103 年只有 1 个殖民地」时才发现——旧日志已被新会话覆盖，
    证据只剩我事前读到的计数（幸好已归档，否则无法复现）。
    ⇒ 故纳入守望：按「会话号」命名，覆盖式保留每个会话的最新完整日志。
    """
    if not os.path.exists(GAME_LOG):
        return
    try:
        with open(GAME_LOG, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(200000)          # 只需前段判断会话数
        sessions = head.count("Game Version")
        os.makedirs(LOG_ARCHIVE, exist_ok=True)
        dst = os.path.join(LOG_ARCHIVE, "game_session%d.log" % max(sessions, 1))
        shutil.copy2(GAME_LOG, dst)         # 覆盖式：留最新完整版
    except Exception as e:
        print("[watcher] log backup skip:", e, flush=True)


def scan_once(seen: dict) -> int:
    """扫一次源目录，返回本次新备份的数量。单个文件出错不中断整轮。"""
    backup_log()
    n = 0
    sources: list[str] = []
    for pattern in SRC_GLOBS:
        sources += glob.glob(pattern, recursive=True)
    for src in sources:
        try:
            mtime = os.path.getmtime(src)
            folder = os.path.basename(os.path.dirname(src))
            dst = os.path.join(DST_DIR, folder + "__" + os.path.basename(src))
            # 去重条件 = 清单里 mtime 一致 **且** 目标档确实还在
            # （后者防"清单说有、磁盘上被清掉"的伪去重）
            if seen.get(src) == mtime and os.path.exists(dst):
                continue
            size = os.path.getsize(src)
            if size < 1024:  # 正在写入的半成品跳过
                continue
            tmp = dst + ".tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            seen[src] = mtime
            n += 1
            print(f"[watcher] backed up {folder}/{os.path.basename(src)} ({size//1024//1024}MB)", flush=True)
        except Exception as e:  # 单个文件失败不得中断整轮
            print("[watcher] skip:", src, e, flush=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="F3' 存档守望器：镜像备份年度自动存档")
    ap.add_argument("--once", action="store_true",
                    help="只扫一次就退出（供定时任务/自动化调用）；不加则常驻循环")
    ap.add_argument("--interval", type=int, default=60, help="常驻模式的扫描间隔（秒）")
    args = ap.parse_args()

    os.makedirs(DST_DIR, exist_ok=True)
    print("[watcher] start, dst =", DST_DIR, flush=True)
    # 2026-09-18：改为从落盘清单恢复（跨 --once 进程去重），见 load_manifest 注释。
    seen: dict = load_manifest()

    # 2026-09-17：新增 --once。常驻模式在后台任务里会被连带终止（退出码非 0，但非故障），
    # 且不适合挂定时任务；--once 让"每小时扫一次"成为可能，持续积累跨年存档
    # —— 这正是 AC-4 长窗口（10 年）所缺的样本。
    if args.once:
        n = scan_once(seen)
        save_manifest(seen)
        total = len(glob.glob(os.path.join(DST_DIR, "*.sav")))
        print(f"[watcher] once done: {n} new backup(s); mirrored total = {total}", flush=True)
        return 0

    while True:
        try:
            scan_once(seen)
            save_manifest(seen)
        except Exception as e:  # 守望器自己不能死
            print("[watcher] error:", e, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
