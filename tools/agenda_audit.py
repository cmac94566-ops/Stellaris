#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-13 内阁议程审计器（WorkBuddy 属地，独立对账器 —— 刻意不 import 被测工具）。

用途：从 gamestate 直读每个国家的 `council_agenda` / `council_agenda_progress`，
      并用「本局究竟有没有执行过 set_council_agenda」的**状态级判据**验收 S-13。

判据（状态级，不锚定日志）：
  * 我方 S-13 旗标 `om_agenda_1_done` / `om_agenda_2_done` / `om_agenda_exhausted`
    在存档里**存在** ⇒ `if = { limit = { has_agenda_selected = no } }` 分支至少执行过一次。
  * 我方 `council_agenda_progress` 的**月增量**（跨两档差分）⇒ 量化推进速率。

只读存档，绝不修改/移动。

用法：
    py tools/agenda_audit.py <a.sav> [<b.sav> ...]
"""
import collections
import re
import sys
import zipfile

OUR_MARK = "om_agenda_boost_n"
S13_FLAGS = ("om_agenda_1_done", "om_agenda_2_done", "om_agenda_exhausted")


def read_gamestate(path):
    with zipfile.ZipFile(path) as z:
        return z.read("gamestate").decode("utf-8", errors="replace")


def field(block, name):
    """字符串字段：name="value"。"""
    m = re.search(re.escape(name) + r'="([^"\n]*)"', block)
    return m.group(1).strip() if m else None


def num_field(block, name):
    """数值字段：name=123.45。"""
    m = re.search(re.escape(name) + r"=([-\d.]+)", block)
    return float(m.group(1)) if m else None


def _brace_end(text, start):
    depth = 0
    k = start
    n = len(text)
    while k < n:
        c = text[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return n - 1


def _brace_up(text, i):
    """从 i 处**向后**回溯，返回直接包围 i 的那个 '{' 的下标（真配对，不猜邻域）。"""
    d = 0
    k = i
    while k >= 0:
        c = text[k]
        if c == "}":
            d += 1
        elif c == "{":
            d -= 1
            if d < 0:
                return k
        k -= 1
    return -1


def find_our_block(gs, mark=OUR_MARK):
    """返回 (country_id, block_text)：从 mark 逐层向上，取第一个含 council_agenda= 的祖先块。

    不依赖容器键名（`countries=` / `country=` 各版本不一致 —— 铁律 19）。
    """
    i = gs.find(mark)
    if i < 0:
        return None, None
    q = i
    for _ in range(6):
        q = _brace_up(gs, q)
        if q < 0:
            break
        e = _brace_end(gs, q)
        blk = gs[q:e + 1]
        if "council_agenda=" in blk:
            m = re.search(r"\n\s*(\d+)=\s*\n\s*\{[^{]*$", gs[max(0, q - 60):q + 1])
            cid = int(m.group(1)) if m else None
            return cid, blk
        q = q - 1          # 继续向上：从该 '{' 之前再回溯
    return None, None


def audit(path):
    gs = read_gamestate(path)
    cid, blk = find_our_block(gs)
    if blk is None:
        return {"path": path, "ours": None, "flags": {}, "ai": []}

    flags = {f: (f in blk) for f in S13_FLAGS}
    pos = re.search(r"council_positions=\s*\{([^}]*)\}", blk)
    res = {
        "path": path,
        "ours": {
            "country_id": cid,
            "council_agenda": field(blk, "council_agenda"),
            "council_agenda_progress": num_field(blk, "council_agenda_progress"),
            "council_positions": pos.group(1).split() if pos else [],
            "om_agenda_boost_n": num_field(blk, "om_agenda_boost_n"),
            "om_era": num_field(blk, "om_era"),
        },
        "flags": flags,
        "ai": [],
    }
    return res


def census(folder):
    """跨局对照：扫全镜像，统计三面 S-13 旗标在多少档/多少局里出现过（他局对照纪律）。"""
    import os
    flags = collections.defaultdict(int)
    per_bucket = collections.defaultdict(lambda: collections.defaultdict(int))
    n = 0
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".sav"):
            continue
        try:
            gs = read_gamestate(os.path.join(folder, fn))
        except Exception:  # noqa: BLE001
            continue
        n += 1
        bucket = fn.split("__autosave_")[0]
        for f in S13_FLAGS:
            if f in gs:
                flags[f] += 1
                per_bucket[bucket][f] += 1
    print(f"扫描 {n} 档")
    for f in S13_FLAGS:
        print(f"  {f}: 出现于 {flags[f]} 档")
    print("按桶：")
    for b, d in sorted(per_bucket.items()):
        print(f"  {b}: " + ", ".join(f"{k}×{v}" for k, v in d.items()) or f"  {b}: 无")
    return 0


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--census":
        return census(sys.argv[2])
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    rows = []
    for p in sys.argv[1:]:
        try:
            r = audit(p)
        except Exception as exc:  # noqa: BLE001
            print("ERR", p, exc)
            continue
        rows.append(r)
        import os
        name = os.path.basename(r["path"])
        if r["ours"] is None:
            print(f"{name}: 未定位我方国家（{OUR_MARK} 缺失）")
            continue
        o = r["ours"]
        print(f"{name}: 国 {o['country_id']} | agenda={o['council_agenda']} "
              f"progress={o['council_agenda_progress']} | era={o['om_era']} "
              f"boost_n={o['om_agenda_boost_n']} | 席位={len(o['council_positions'])}")
        print(f"    S-13 旗标: " + ", ".join(f"{k}={'有' if v else '无'}" for k, v in r["flags"].items()))
        if r["ai"]:
            print("    AI 对照: " + " ; ".join(
                f"国{a['id']} {a['agenda']}={a['progress']}" for a in r["ai"][:4]))

    # 跨档差分速率
    if len(rows) >= 2:
        a, b = rows[0], rows[-1]
        if a["ours"] and b["ours"] and a["ours"]["council_agenda"] == b["ours"]["council_agenda"]:
            dp = b["ours"]["council_agenda_progress"] - a["ours"]["council_agenda_progress"]
            import os
            print(f"\n[差分] {os.path.basename(a['path'])} -> {os.path.basename(b['path'])}: "
                  f"Δprogress = {dp:.3f}（同 agenda={a['ours']['council_agenda']} 可比）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
