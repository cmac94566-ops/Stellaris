#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信箱归档器 —— 单一真相源（2026-09-19 立）

为什么要有这个工具
------------------
我方历次归档都是「临时写个 _archNN.py」，两次犯同一个错：
  ① 只填 `回执:` 忘了把 `状态:` 改成 ✅已完成 ⇒ archive 里堆出 7 条"待处理"残留
     （对按状态扫全库的脚本会假报未处理）；
  ② 有时忘了真的从 inbox 删原条 ⇒ 出现"归档了但 inbox 还在"。
⇒ 按铁律 22 的思路：**归档动作也应有单一入口**，不要每次新写一段。

用法
----
    py -3.12 tools/mail_archive.py --msg ZCMSG-054 --receipt-file body.txt
    py -3.12 tools/mail_archive.py --msg ZCMSG-054            # 只归档，回执留空待填
    py -3.12 tools/mail_archive.py --list                     # 列出 inbox 未读（行锚定）
    py -3.12 tools/mail_archive.py --residual                 # 扫 archive 里的"状态: 待处理"残留

约定
----
- 读 `HANDOFF/inbox_workbuddy.md`（我方收件箱），写 `HANDOFF/archive_workbuddy.md`（我方归档）。
- 归档 = ① 状态改 ✅已完成（带归档时间 + 引用回信号）② 可选填 `回执:` ③ 从 inbox 删原条。
- **幂等**：目标已在 archive ⇒ 不重复追加；目标不在 inbox 且不在 archive ⇒ 报错退出 1。
- 只动这两个文件，绝不碰代码/文档（铁律 21 属地）。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import time
from pathlib import Path

INBOX = Path("HANDOFF/inbox_workbuddy.md")
ARCH = Path("HANDOFF/archive_workbuddy.md")
HDR = re.compile(r"^## \[([A-Z]+MSG-\d+)\]", re.M)
PENDING = re.compile(r"(?m)^-\s*状态:\s*待处理\s*$")
STATUS_LINE = re.compile(r"(?m)^-\s*状态:.*$")
RECEIPT_EMPTY = re.compile(r"(?m)^-\s*回执:\s*$")


def read(p: Path) -> str:
    return io.open(p, encoding="utf-8").read() if p.exists() else ""


def write(p: Path, s: str) -> None:
    io.open(p, "w", encoding="utf-8", newline="").write(s)


def split_blocks(t: str):
    parts = re.split(r"(?m)^(?=## \[)", t)
    return parts[0], [b for b in parts[1:] if b.startswith("## [")]


def list_unread() -> int:
    c = read(INBOX)
    ms = HDR.findall(c)
    pend = len(PENDING.findall(c))
    print(f"inbox 条目 {len(ms)}｜待处理(行锚定) {pend}｜{' '.join(ms)}")
    return 0


def scan_residual() -> int:
    c = read(ARCH)
    hits = PENDING.findall(c)
    print(f"archive_workbuddy 残留「状态: 待处理」: {len(hits)} 条")
    for b in split_blocks(c)[1]:
        if PENDING.search(b):
            print("   " + b.splitlines()[0][:90])
    return 0


def fix_residual() -> int:
    t = read(ARCH)
    head, blocks = split_blocks(t)
    n = 0
    out = [head]
    for b in blocks:
        if PENDING.search(b):
            b = STATUS_LINE.sub("- 状态: ✅ 已完成（历史归档补正，2026-09-19）", b, count=1)
            n += 1
        out.append(b)
    write(ARCH, "".join(out))
    print(f"已补正 {n} 条残留状态")
    return 0


def archive(msg: str, receipt_file: str | None) -> int:
    key = msg.strip().upper()
    if not key.startswith("ZCMSG-"):
        # 归档器只处理我方收件箱；我方发信由 zcode 侧归档
        print(f"⛔ {key} 不是我方收件箱的编号（应形如 ZCMSG-nnn，zcode → WorkBuddy）")
        return 1

    arc = read(ARCH)
    if f"## [{key}]" in arc:
        print(f"⏭  {key} 已在归档中（幂等：不重复追加）")
        # 仍确保 inbox 里没有残留
        head, blocks = split_blocks(read(INBOX))
        rest = [b for b in blocks if not b.startswith(f"## [{key}]")]
        if len(rest) != len(blocks):
            write(INBOX, head.rstrip("\n") + "\n" + "".join("\n" + b.rstrip("\n") + "\n" for b in rest))
            print("   并从 inbox 清除了残留副本")
        return 0

    head, blocks = split_blocks(read(INBOX))
    target = [b for b in blocks if b.startswith(f"## [{key}]")]
    if not target:
        print(f"⛔ 在 inbox 找不到 {key}（可能已归档或编号有误）→ 未做任何改动")
        return 1

    b = target[0]
    ts = time.strftime("%Y-%m-%d %H:%M")

    # 回执
    if receipt_file:
        rc = io.open(receipt_file, encoding="utf-8").read().strip()
        line = "- 回执: " + rc
        if RECEIPT_EMPTY.search(b):
            b = RECEIPT_EMPTY.sub(lambda _m: line, b, count=1)
        elif re.search(r"(?m)^-\s*回执:", b):
            b = re.sub(r"(?m)^-\s*回执:.*$", lambda _m: line, b, count=1)
        else:
            b = b.rstrip() + "\n" + line + "\n"

    # 状态 → 已完成
    if STATUS_LINE.search(b):
        b = STATUS_LINE.sub(f"- 状态: ✅ 已完成（{ts} 归档）", b, count=1)
    else:
        b = b.rstrip() + f"\n- 状态: ✅ 已完成（{ts} 归档）\n"

    b = b if b.endswith("\n") else b + "\n"

    arc = arc.rstrip("\n") + "\n\n" + b
    write(ARCH, arc)

    rest = [x for x in blocks if not x.startswith(f"## [{key}]")]
    write(INBOX, head.rstrip("\n") + "\n" + "".join("\n" + x.rstrip("\n") + "\n" for x in rest))

    print(f"✅ 已归档 {key}（状态→已完成；回执={'有' if receipt_file else '留空'}；inbox 剩余 {len(rest)} 条）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="信箱归档器（单一入口，幂等）")
    ap.add_argument("--msg", help="要归档的编号，如 ZCMSG-054")
    ap.add_argument("--receipt-file", help="回执正文文件（写入 `- 回执:` 行）")
    ap.add_argument("--list", action="store_true", help="列出 inbox 未读")
    ap.add_argument("--residual", action="store_true", help="扫 archive 残留「待处理」")
    ap.add_argument("--fix-residual", action="store_true", help="补正 archive 残留「待处理」")
    a = ap.parse_args(argv)

    if a.list:
        return list_unread()
    if a.residual:
        return scan_residual()
    if a.fix_residual:
        return fix_residual()
    if not a.msg:
        ap.print_help()
        return 1
    return archive(a.msg, a.receipt_file)


if __name__ == "__main__":
    sys.exit(main())
