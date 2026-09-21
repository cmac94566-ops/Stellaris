"""主脑监视窗口 —— 只看自治层（主脑）在游戏里做了什么，按动作类型着色。

演进（三次收窄，每次都按用户实测反馈）：
  2026-09-16：用户要「三合一」（主脑 / 智能体 / 邮箱 三窗合一）；
  2026-09-16 晚：看过输出后收窄为「只保留智能体操作跟主脑操作」—— 邮箱改 `--mail` 可选；
  **2026-09-18：再收窄为「只保留主脑日志」** —— 智能体改 `--agent` 可选，默认关闭。

保留可选路（而非删代码）的理由：邮箱是「与 zcode 通讯」的唯一可视入口、
智能体日志是「谁改了什么」的留痕，临时排障都要用；只是不该占常态视野。

数据源：
  主脑   —— Stellaris logs/game.log 中含 OVERMIND 的行（默认显示）
  智能体 —— logs/agent_actions.log（WorkBuddy / zcode 的操作留痕，--agent 打开）
  邮箱   —— HANDOFF/inbox_workbuddy.md（收）+ inbox_zcode.md（发，--mail 打开）

两个已踩过的坑，本脚本必须守住：
  1. 启动要**回填**最近内容，否则主脑每月才动作一次，用户看到的是空白窗口
     （2026-09-16 反馈「这个窗口根本不更新」）。
  2. Windows 控制台要先开 VT 处理，否则 ANSI 颜色是一串乱码。

用法::

    py -3.12 tools/unified_watch.py                # 只看主脑（默认）
    py -3.12 tools/unified_watch.py --agent        # 追加智能体操作
    py -3.12 tools/unified_watch.py --mail         # 追加邮箱
    py -3.12 tools/unified_watch.py --backfill 20  # 回填行数
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GAME_LOG = Path(os.path.expanduser("~")) / "Documents/Paradox Interactive/Stellaris/logs/game.log"
AGENT_LOG = REPO / "logs" / "agent_actions.log"
HANDOFF = REPO / "HANDOFF"
INBOX_WB = HANDOFF / "inbox_workbuddy.md"   # zcode 写给我方（收件）
INBOX_ZC = HANDOFF / "inbox_zcode.md"       # 我方写给 zcode（发件，含其回执）

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
GREEN = "\x1b[32m"
GRAY = "\x1b[90m"
RED = "\x1b[31m"

# 来源 -> 颜色（一眼分清是哪一路）
SRC_COLOR = {"主脑": CYAN, "智能体": YELLOW, "邮箱": MAGENTA}

# 心跳行 = 每月必然刷屏、但没有"做了什么"信息的行。
# 2026-09-18 用户反馈「窗口起来了但内容不对」：实测最近 60 条里
# 【归因OK】24 + 【相位 x/8】24 = 48 条（80%）都是心跳，真正有信息量的动作行只有 9 条
# ⇒ 默认折叠心跳，每 HEARTBEAT_EVERY 条汇总一次（保留"归因OK 是正面证据"的价值），
#   加 --all 可全看。
#
# 2026-09-19 同步：S-18 求值器上线后 **【求值】变成第一噪音源** —— 归档会话实测
# 435 条 OVERMIND 行里 【求值】280 条 = **64.4%**（其次 归因OK 12.9%、相位 x/8 15%）
# ⇒ 把 【求值】 一并纳入折叠，否则窗口会被它刷屏。
# 判定原则：**频率高 且 不携带"做了什么"信息** 才折叠；
#   【熔断汇总】仅 2.8%、且是每月熔断/解除的聚合证据 ⇒ **不折叠**（保留可观测性）。
HEARTBEAT_TAGS = ("【相位 ", "【归因OK】", "【求值】")
HEARTBEAT_HINT = "相位 / 归因OK / 求值"
HEARTBEAT_EVERY = 12          # 每折叠 12 条（≈半年）汇总一次


def is_heartbeat(line: str) -> bool:
    """判断 game.log 的一行是不是心跳行（按 OVERMIND 之后的标签判断）。"""
    i = line.find("OVERMIND: ")
    if i < 0:
        return False
    tail = line[i + len("OVERMIND: "):]
    return any(tail.startswith(tag) for tag in HEARTBEAT_TAGS)


# 主脑动作按做什么着色（沿用 overmind_log_window.py 的既有约定）
# ⚠️ 两点实现约束（2026-09-18 修订时踩到）：
#   ① colorize() 取**首个命中**的规则 ⇒ 有包含关系的标签必须**长的在前**
#      （如 【熔断解除】 必须排在 【熔断】 之前，否则永远显示成熔断色）；
#   ② 带后缀的标签要用**前缀式**匹配（如 【白名单·产能期】 不含子串「【白名单】」，
#      故规则写成不带右括号的 "【白名单"）。
# 2026-09-19 重新对齐：`grep -rho 'OVERMIND: 【[^】]*】' mod/` 实得 **34 类标签**
#   （原表 26 类已过期）。本次补入 7 个新标签：
#     【熔断汇总】【传统】【回填】【探测】【领袖】【星堡】【贸易枢纽】
#   —— 分别来自 D-44 双通道重构、P0-B 相位7 判定自报、S-15 领袖、V-6 星港。
#   复核方式同旧：**每次改 mod 日志标签后，必须回头跑一遍这个 grep 与窗口表做差集**
#   （口径说明本身也是代码 —— 漏同步 = 窗口把新行显示成无色，等于看不见）。
ACTION_COLORS = (
    # —— 警戒类（红/品红优先，一眼看到出问题）——
    ("【告警】", BOLD + MAGENTA),
    ("【异常】", BOLD + RED),
    ("【宣战评估】", BOLD + RED),
    # —— A3 收益门（注意：【熔断解除】须在【熔断】之前）——
    ("【熔断解除】", GREEN),
    ("【熔断汇总】", BOLD + YELLOW),   # 2026-09-19 新增：每月熔断/解除聚合行（不折叠）
    ("【熔断】", BOLD + YELLOW),
    ("【收益】", GREEN),
    # —— A1/A2 求值与议程 ——
    ("【求值】", BOLD + CYAN),
    ("【议程】", CYAN),
    ("【局势】", YELLOW),
    ("【威胁评估】", YELLOW),
    ("【飞升路线】", MAGENTA),
    ("【传统】", MAGENTA),            # 2026-09-19 新增：相位7 判定自报（afford/树位/熔断态）
    # —— 通道/机械类（D-44 双通道重构后新增）——
    #   这两行是"机制在不在转"的证据，**不折叠**（当前 D-44 正靠它们取观测）
    ("【回填】", CYAN),
    ("【探测】", CYAN),
    # —— 动作类 ——
    ("【救火】", BOLD + YELLOW),
    ("【止战】", GREEN),
    ("【锚地】", CYAN),
    ("【产能】", GREEN),
    ("【领袖】", GREEN),              # 2026-09-19 新增：S-15 领袖招募/就任
    ("【星堡】", CYAN),               # 2026-09-19 新增：V-6 星港模块
    ("【贸易枢纽】", CYAN),           # 2026-09-19 新增：V-6 星港模块
    ("【白名单", GRAY),          # 前缀式：覆盖 【白名单】【白名单·产能期/得分期/生存期】
    ("【归因OK】", GRAY),
    ("【相位 ", GRAY),
    # —— 换版/接管等关键事件（非【】形态的行）——
    ("lex applied rev", BOLD + MAGENTA),
    ("自治层上线", BOLD + MAGENTA),
    ("宣战", BOLD + RED),
    ("前哨拓土", GREEN),
    ("建立新殖民地", GREEN),
    ("建了", GREEN),
    ("接管", MAGENTA),
    ("交还", RED),
    ("征募", YELLOW),
    ("重建", CYAN),
    ("授予", YELLOW),
    ("外交", CYAN),
)

MSG_RE = re.compile(r"^## \[([A-Z]*MSG-\d+)\]\s*([^\n]*)")
TOPIC_RE = re.compile(r"^- 主题:\s*(.*)$")
DATE_RE = re.compile(r"\[(\d{4}\.\d+\.\d+)\]")


def enable_ansi() -> None:
    """开启 VT 处理，否则 Windows 控制台把 ANSI 转义当乱码打印。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def colorize(body: str) -> str:
    for key, col in ACTION_COLORS:
        if key in body:
            return col + body + RESET
    return body


def clean_overmind(line: str) -> str | None:
    """从 game.log 的一行里提炼出「[游戏内日期] 动作」。"""
    i = line.find("OVERMIND: ")
    if i < 0:
        return None
    tail = line[i + len("OVERMIND: "):].rstrip().strip('"')
    dm = DATE_RE.search(line)
    date = dm.group(1) if dm else "?"
    return f"[{date}] {colorize(tail)}"


class Tail:
    """记住读到哪，下次只取新增部分。文件被轮转/清空则从头读。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pos = 0
        self.seen_rotated = False

    def new_lines(self) -> list[str]:
        try:
            if not self.path.exists():
                return []
            size = self.path.stat().st_size
            if size < self.pos:
                self.pos = 0  # 被轮转或清空
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.pos)
                data = fh.read()
                self.pos = fh.tell()
        except OSError:
            return []
        return [ln for ln in data.splitlines() if ln.strip()]

    def backfill(self, n: int) -> list[str]:
        try:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            self.pos = self.path.stat().st_size
        except OSError:
            return []
        return [ln for ln in lines[-n:] if ln.strip()]


def mail_ids(path: Path) -> list[tuple[str, str]]:
    """返回 [(编号, 主题)]，文件不存在则空。"""
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    cur_id = ""
    for line in text.splitlines():
        m = MSG_RE.match(line)
        if m:
            cur_id = m.group(1)
            out.append((cur_id, ""))
            continue
        t = TOPIC_RE.match(line)
        if t and out:
            out[-1] = (out[-1][0], t.group(1).strip())
    return out


def pending_count(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        return len(re.findall(r"(?m)^-\s*状态:\s*待处理\s*$", path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="主脑监视：自治层动作（默认只看主脑）")
    ap.add_argument("--agent", action="store_true",
                    help="额外显示智能体操作（默认关闭 —— 用户 2026-09-18 收窄为只看主脑）")
    ap.add_argument("--all", action="store_true",
                    help="连心跳行一起显示（默认折叠心跳 —— 见 HEARTBEAT_TAGS）")
    ap.add_argument("--mail", action="store_true",
                    help="同时监视邮箱（默认关闭）")
    ap.add_argument("--backfill", type=int, default=12, help="每个源启动时回填行数")
    ap.add_argument("--interval", type=float, default=0.6, help="轮询间隔秒")
    args = ap.parse_args(argv)

    enable_ansi()
    # 2026-09-19：随游戏自启（watch_autostart.py）时窗口由脚本创建，
    # 这里自设控制台标题，保证与手开 bat 的观感一致（ASCII —— 标题栏经 UTF-16 API，中文亦可，但保持与 bat 同款）
    try:
        ctypes.windll.kernel32.SetConsoleTitleW("Overmind Watch - Autonomy Log")
    except Exception:
        pass
    game = Tail(GAME_LOG)
    agent = Tail(AGENT_LOG)

    print(f"{BOLD}主脑监视{DIM} —— 自治层在游戏里做了什么{DIM}"
          f"（加 --agent 看智能体操作，--mail 看邮箱）{RESET}")
    print(f"{DIM}主脑配色：{RED}警戒{MAGENTA}·告警 {YELLOW}救火/威胁 {GREEN}收益·建设(含领袖) "
          f"{CYAN}求值·锚地·回填·探测·星堡 {MAGENTA}传统·飞升·换版 {GRAY}相位·白名单{RESET}")
    print(f"{DIM}心跳行（{HEARTBEAT_HINT}）默认折叠，每 {HEARTBEAT_EVERY} 条汇总一次；加 --all 可全看{RESET}")
    print(f"{DIM}Ctrl+C 退出{RESET}\n")

    # ---- 启动回填（教训：不回填就是空白窗口）----
    print(f"{DIM}--- 回填最近内容（动作）---{RESET}")
    # 心跳行占比很高（实测 80%），所以回溯较多行、只取"动作"，凑够 args.backfill 条为止。
    picked: list[str] = []
    for ln in reversed(game.backfill(args.backfill * 25)):
        if not args.all and is_heartbeat(ln):
            continue
        c = clean_overmind(ln)
        if c:
            picked.append(c)
            if len(picked) >= args.backfill:
                break
    for c in reversed(picked):
        print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['主脑']}[主脑]{RESET} {c}")
    if not picked:
        print(f"{DIM}{stamp()} {GRAY}[提示]{RESET} "
              f"近期只有心跳行（{HEARTBEAT_HINT}），暂无动作 —— 加 --all 可看心跳{RESET}")
    if args.agent:
        for ln in agent.backfill(args.backfill):
            print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['智能体']}[智能体]{RESET} {ln[:150]}")
    if args.mail:
        for mid, topic in mail_ids(INBOX_WB)[-3:]:
            print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['邮箱']}[邮箱]{RESET} 收件 {mid} {topic}")
        pend = pending_count(INBOX_WB)
        print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['邮箱']}[邮箱]{RESET} "
              f"未读 {pend} 封（收） / {pending_count(INBOX_ZC)} 封（发给 zcode）")
    print(f"{DIM}--- 实时跟随 ---{RESET}\n")

    known_wb = {m for m, _ in mail_ids(INBOX_WB)}
    known_zc = {m for m, _ in mail_ids(INBOX_ZC)}
    last_mail_check = 0.0
    hb = 0                        # 已折叠的心跳条数

    try:
        while True:
            for ln in game.new_lines():
                if not args.all and is_heartbeat(ln):
                    hb += 1
                    if hb % HEARTBEAT_EVERY == 0:
                        print(f"{DIM}{stamp()} {GRAY}[心跳]{RESET} "
                              f"已折叠 {hb} 条（{HEARTBEAT_HINT}）—— 自治层正常运行{RESET}")
                    continue
                c = clean_overmind(ln)
                if c:
                    print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['主脑']}[主脑]{RESET} {c}")
            if args.agent:
                for ln in agent.new_lines():
                    print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['智能体']}[智能体]{RESET} {ln[:150]}")

            if args.mail and time.time() - last_mail_check > 5:
                last_mail_check = time.time()
                for path, known, tag in ((INBOX_WB, known_wb, "收件"), (INBOX_ZC, known_zc, "发件")):
                    cur = mail_ids(path)
                    for mid, topic in cur:
                        if mid not in known:
                            known.add(mid)
                            print(f"{DIM}{stamp()}{RESET} {SRC_COLOR['邮箱']}[邮箱]{RESET} "
                                  f"新{tag} {BOLD}{mid}{RESET} {topic}")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}已退出。{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
