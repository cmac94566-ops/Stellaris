# -*- coding: utf-8 -*-
"""score_audit —— `--score` 的**独立**对账器（对账 #37 / #38，D-52）。

用途
----
1. 取存档真值（主脑定位 / 总分 / 科技力 / 经济力 / 舰队力 / 分母），供与
   `scripts/overmind_metrics.py --score` 的打印值**逐位对撞**（对账 #37）。
2. 给出「**按 `victory_score` 的位序**」与 `victory_rank` 字段的可信度统计
   （是否大量并列 / 是否与分数序冲突），用于判定「名次」口径是否可用（对账 #38 / D-52）。

设计约束（务必保持）
--------------------
* **刻意不 import `scripts/overmind_metrics.py`** —— 用被测工具验证被测工具会
  共用同一处解析错误（本项目反复踩的坑）。故本文件自带 zip 读取 + 栈式括号配对。
* **只读存档**，不修改、不移动任何文件（铁律：绝不触碰用户存档）。

用法
----
    python tools/score_audit.py <save.sav> [<save2.sav> ...]
    python tools/score_audit.py --json <save.sav>

输出（stdout）
    [file]   文件名 / 国家数 / gamestate 字符数
    [marker] `overmind_autonomy` 标记命中的国家（主脑）
    [player] `player.country`
    [truth]  主脑 victory_rank / victory_score / tech / economy / military
    [denom]  有 `victory_score` 的国家数（= 可能的位序分母）
    [rank]   不同 rank 值个数 / 并列情况 / 填充值样例
    [me]     主脑：分数、字段名次、**按分数的真实位序**

判读纪律（2026-09-20 定案）
    `名次 N/M` 由「存档 `victory_rank` 字段」÷「有分数的国家数」拼接，**两端不同源**
    ⇒ **名次类判据一律禁用**；只用 `victory_score` + 「按分数的位序」。
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

MARKER = re.compile(r"overmind_autonomy(?![A-Za-z0-9_])")
SCORE_KEYS = ("victory_rank", "victory_score", "tech_power", "economy_power", "military_power")


def gamestate_text(path: Path) -> str:
    """从 .sav（zip 容器）取出 gamestate 文本。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        target = "gamestate" if "gamestate" in names else names[-1]
        return z.read(target).decode("utf-8", "replace")


def match_block(text: str, i: int) -> str:
    """``text[i] == '{'`` ⇒ 返回含花括号的完整块（自写栈式配对，不做正则近似）。"""
    if text[i] != "{":
        raise ValueError(f"expected '{{' at {i}, got {text[i:i + 10]!r}")
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise ValueError("unbalanced braces")


def country_blocks(text: str) -> list[tuple[int, str]]:
    """``[(country_id, block_text), …]``；顶层 ``country=`` 段内逐国切块。"""
    anchor = text.find("\ncountry=\n{\n")
    if anchor == -1:
        return []
    body = match_block(text, text.index("{", anchor + len("\ncountry=")))
    inner = body[1:-1]
    return [
        (int(m.group(1)), match_block(inner, inner.index("{", m.start())))
        for m in re.finditer(r"\n\t(\d{1,4})=\n\t\{", inner)
    ]


def num(blk: str, key: str) -> float | None:
    m = re.search(r"(?<![A-Za-z0-9_])" + key + r"=([\d.]+)", blk)
    return float(m.group(1)) if m else None


def audit(path: Path) -> dict:
    text = gamestate_text(path)
    blocks = country_blocks(text)
    flagged = [(cid, blk) for cid, blk in blocks if MARKER.search(blk)]
    pm = re.search(r"\nplayer=\n\{\n(.*?)\n\}", text, re.S)
    pc = re.search(r"country=(\d+)", pm.group(1)) if pm else None
    player = int(pc.group(1)) if pc else None

    cid = flagged[0][0] if flagged else None
    me_blk = dict(flagged).get(cid) if cid is not None else None
    me = {k: num(me_blk, k) for k in SCORE_KEYS} if me_blk else {}

    scored: list[tuple[int, float, float | None]] = []
    for c, b in blocks:
        s = num(b, "victory_score")
        if s is not None:
            scored.append((c, s, num(b, "victory_rank")))

    by_score = sorted(scored, key=lambda x: x[1], reverse=True)
    true_rank = next((i + 1 for i, x in enumerate(by_score) if x[0] == cid), None)
    ranks: dict[int, list[tuple[int, float]]] = {}
    for c, s, r in scored:
        if r is not None:
            ranks.setdefault(int(r), []).append((c, s))
    dup = {r: v for r, v in ranks.items() if len(v) > 1}

    return {
        "file": path.name,
        "countries": len(blocks),
        "gamestate_chars": len(text),
        "marker_ids": [c for c, _ in flagged],
        "player_country": player,
        "main_brain": cid,
        "truth": me,
        "denom": len(scored),
        "distinct_rank_values": len(ranks),
        "duplicated_rank_values": len(dup),
        "duplicated_countries": sum(len(v) for v in dup.values()),
        "fill_rank_samples": {
            str(r): sorted({int(s) for _, s in v})[:6] for r, v in sorted(dup.items())[:3]
        },
        "true_rank_by_score": true_rank,
        "top_by_score": [
            {"id": c, "score": s, "rank_field": int(r) if r else None}
            for c, s, r in by_score[:12]
        ],
    }


def report(rep: dict) -> None:
    print(f"[file] {rep['file']}  countries={rep['countries']}  "
          f"gamestate_chars={rep['gamestate_chars']:,}")
    print(f"[marker] 含 overmind_autonomy 的国家数 = {len(rep['marker_ids'])}  "
          f"ids={rep['marker_ids']}")
    print(f"[player] player.country = {rep['player_country']}")
    t = rep["truth"]
    print("[truth] id=%s victory_rank=%s victory_score=%s tech=%s eco=%s mil=%s"
          % (rep["main_brain"], t.get("victory_rank"), t.get("victory_score"),
             t.get("tech_power"), t.get("economy_power"), t.get("military_power")))
    print(f"[denom] 有 victory_score 的国家数 = {rep['denom']}")
    print(f"[rank] 不同名次值 = {rep['distinct_rank_values']}  "
          f"并列的名次值 = {rep['duplicated_rank_values']}  "
          f"并列涉及国家数 = {rep['duplicated_countries']}")
    for r, scores in rep["fill_rank_samples"].items():
        print(f"        名次 {r} 组内分数样本 {scores}")
    print(f"[me] 分数={t.get('victory_score')}  字段名次={t.get('victory_rank')}  "
          f"**按分数真实位序={rep['true_rank_by_score']}/{rep['denom']}**")
    print("[top] 分数榜前 12（国, 分, rank字段）:",
          [(x["id"], int(x["score"]), x["rank_field"]) for x in rep["top_by_score"]])


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    for p in paths:
        if not p.exists():
            print(f"[error] 找不到存档：{p}", file=sys.stderr)
            return 1
        rep = audit(p)
        if as_json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
