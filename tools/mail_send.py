#!/usr/bin/env python
"""幂等发信器 —— 防止"脚本被重放导致同一封信发两遍"（本项目已踩 3 次）。

背景：Bash 命令偶被沙箱升级重放，追加类写入会执行两遍。历史上：
  · WDMSG-040 在 inbox 内重复一次；
  · WDMSG-046/047 内容完全相同（同主题、同时间）连发两封。
⇒ 本工具把"算号 + 判重 + 追加 + 复核"收在一处，重复执行不会产生第二封。

用法：
  python tools/mail_send.py --subject "[例行] xxx" --body-file msg.md      # 从文件读正文
  python tools/mail_send.py --subject "[阻塞] xxx" --body "正文..."        # 直接给正文
  python tools/mail_send.py --dedupe                                       # 只做去重体检，不发信
  python tools/mail_send.py --dedupe --apply                               # 去重并写回

判重口径：① 主题与既有**待处理件**相同 ⇒ 视为重复，直接退出（不发）；
          ② 正文哈希与既有件相同 ⇒ 同上。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INBOX = REPO / "HANDOFF/inbox_zcode.md"
ARCH = REPO / "HANDOFF/archive_zcode.md"
HDR = re.compile(r"(?m)^## \[WDMSG-(\d+)\]\s*(.*)$")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def blocks(text: str) -> list[str]:
    return [b for b in re.split(r"(?m)^(?=## \[)", text) if b.startswith("## [")]


def norm(s: str) -> str:
    """把时间戳与序号抹掉后做比较用（避免"同内容不同号"被判成不同件）。"""
    s = re.sub(r"^## \[WDMSG-\d+\]\s*[^\n]*", "", s)
    s = re.sub(r"(?m)^- 状态:.*$", "", s)
    s = re.sub(r"(?m)^- 回执:.*$", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def h(s: str) -> str:
    return hashlib.sha1(norm(s).encode("utf-8")).hexdigest()[:12]


RESERVED_FROM = 900


def next_no() -> int:
    """正常序列取号 —— **排除 900+ 保留段**。

    2026-09-18 踩坑：破局用的信标条曾编为 `MSG-900` / `WDMSG-901`；若不过滤，
    算号会从 902 一路爬升，把正常序列（…055 → 056 → …）永久污染。
    900+ 保留给「信标 / 特殊件」，**不参与递增**。
    """
    nums: list[int] = []
    for f in (INBOX, ARCH):
        nums += [int(m.group(1)) for m in HDR.finditer(read(f))]
    nums = [n for n in nums if n < RESERVED_FROM]
    return max(nums) + 1 if nums else 1


COOLDOWN_SEC = 300
TS = re.compile(r"(?m)^## \[WDMSG-(\d+)\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")


def newest_ts():
    """我方最近一封（inbox + archive 全量）的 (编号, 时间)。用于发送冷却。"""
    best = None
    for f in (INBOX, ARCH):
        for m in TS.finditer(read(f)):
            n = int(m.group(1))
            if n >= RESERVED_FROM:
                continue
            try:
                t = datetime.datetime.strptime(m.group(2), "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if best is None or t > best[1]:
                best = (n, t)
    return best


def send(subject: str, body: str, force: bool = False) -> int:
    # ---------------------------------------------------------------- 发送冷却锁
    # 2026-09-19 动机：**同一分钟（14:33）我方发了 WDMSG-073 与 074 两封**，
    # 且两封对 D-41 的编号/裁定互相冲突 —— 根因是「自动化巡检」与「人工回信」
    # 并行发送、各自只检查"是否已发过"，但检查与写入之间存在竞态。
    # 这与 2026-09-18 的 22:25/22:30 同族，是**第二次复现**，故升级为工具级强制：
    # **冷却窗口内一律拒绝，除非显式 --force（用于 [阻塞] 例外）。**
    if not force:
        nt = newest_ts()
        if nt is not None:
            delta = (datetime.datetime.now() - nt[1]).total_seconds()
            if delta < COOLDOWN_SEC:
                print("⛔ 发送冷却中：最近一封 WDMSG-%03d 发于 %s（%.0f 秒前，阈值 %d 秒）"
                      % (nt[0], nt[1].strftime("%H:%M"), delta, COOLDOWN_SEC))
                print("   ⇒ 按攒批纪律**本轮不发**；确需立即发（如 [阻塞]）请加 --force。")
                print("   ⇒ 若内容实为修订，请把修订并入下一封，不要连发两封。")
                return 3

    inbox = read(INBOX)
    mine = [b for b in blocks(inbox) if b.startswith("## [WDMSG-")]
    bh = h(subject + "\n" + body)
    for b in mine:
        if h(b) == bh:
            print("判重命中：完全相同的件已在待处理队列 ⇒ 不重复发（幂等生效）")
            return 0
        m = re.search(r"(?m)^- 主题:\s*(.*)$", b)
        if m and m.group(1).strip() == subject.strip():
            print("判重命中：同主题的待处理件已存在 ⇒ 不重复发（幂等生效）")
            print("  既有件：", b.splitlines()[0][:80])
            return 0

    n = next_no()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = ("\n## [WDMSG-%03d] %s WorkBuddy → zcode\n"
           "- 状态: 待处理\n"
           "- 主题: %s\n"
           "- 内容:\n%s\n- 回执:\n") % (n, now, subject, body.rstrip())
    with INBOX.open("a", encoding="utf-8", newline="") as fh:
        fh.write(msg)

    # 复核：刚写入的编号必须只出现一次
    again = read(INBOX)
    cnt = len(re.findall(r"^## \[WDMSG-%03d\]" % n, again, re.M))
    if cnt != 1:
        print("⚠️ 复核失败：WDMSG-%03d 出现 %d 次（预期 1）" % (n, cnt))
        return 2
    print("已发 WDMSG-%03d（复核：出现 1 次 ✅）" % n)
    return 0


def dedupe(apply: bool) -> int:
    """体检/清理：inbox 内"同主题（时间不同）"或"同正文哈希"的多余副本。"""
    inbox = read(INBOX)
    bs = [b for b in blocks(inbox) if b.startswith("## [WDMSG-")]
    seen: dict[str, str] = {}
    dup: list[str] = []
    for b in bs:
        m = re.search(r"(?m)^- 主题:\s*(.*)$", b)
        subj = m.group(1).strip() if m else "(无主题)"
        key = h(b)
        if key in seen or subj in seen.values():
            dup.append(b)
            continue
        seen[key] = subj
    if not dup:
        print("去重体检：无重复 ✅")
        return 0
    print("发现 %d 条重复：" % len(dup))
    for b in dup:
        print("   ·", b.splitlines()[0][:80])
    if not apply:
        print("（只体检；加 --apply 才会删除重复副本，保留最早一条）")
        return 0
    keep = inbox
    for b in dup:
        keep = keep.replace(b, "", 1)
    keep = re.sub(r"\n{3,}", "\n\n", keep)
    INBOX.write_text(keep, encoding="utf-8", newline="")
    print("已删除 %d 条重复副本" % len(dup))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject")
    ap.add_argument("--body")
    ap.add_argument("--body-file")
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="绕过发送冷却（%d 秒）；仅用于 [阻塞] 例外" % COOLDOWN_SEC)
    a = ap.parse_args()

    if a.dedupe:
        return dedupe(a.apply)
    if not a.subject or not (a.body or a.body_file):
        ap.print_help()
        return 1
    body = io.open(a.body_file, encoding="utf-8").read() if a.body_file else a.body
    return send(a.subject, body, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
