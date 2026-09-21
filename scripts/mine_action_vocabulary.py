"""Mine the game's scriptable action vocabulary straight out of the install.

Why mine instead of listing from memory
--------------------------------------
Stellaris exposes hundreds of script effects.  If we hand-write a list from
memory we will (a) miss whole subsystems and (b) invent names that silently do
nothing — both of which have already bitten this project.

So we derive the vocabulary from evidence:

1. **Candidate extraction** — the exe contains the engine's script-name table
   as NUL-terminated lowercase tokens.  Collect every such token binary-wide.
2. **Usage confirmation** — keep only names that actually appear in the
   shipped vanilla script corpus in *effect position* (``name =`` /
   ``name = {``).  A name the engine knows and vanilla uses is safe to call.
3. **Domain classification** — bucket by verb prefix and by domain keywords,
   with a usage count attached so nothing claims authority without evidence.

Output: ``docs/ACTION_VOCABULARY.json`` (machine-readable) and
``docs/ACTION_VOCABULARY.md`` (human index, grouped by gameplay domain).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TOKEN_RE = re.compile(rb"\x00([a-z][a-z_0-9]{3,44})\x00")

#: Command-line switches.  Two storage layouts actually occur in
#: ``stellaris.exe``, which is why one pattern is not enough:
#:
#: 1. **Dash-prefixed, NUL-delimited.**  ``-continuelastsave`` lives like this in
#:    the ``frontendidler.cpp`` string pool, right next to ``latest`` and
#:    ``SAVE_VERSION``.  ``TOKEN_RE`` above silently drops it because that
#:    pattern anchors on ``[a-z]`` and every dash-prefixed run fails the match.
#:
#: 2. **Bare name, no dash, padded with runs of NULs.**  The launch-parameter
#:    table at ``0x249bd20`` stores bare names in short/long pairs, e.g.::
#:
#:        humanai | human_ai | ... | berserkai | berserk_ai |
#:        fullgalaxyspawn | overnight | game_paused false | gamestatetimer
#:
#:    ``game_paused`` even carries its default value inline (``game_paused
#:    false``), which is the tell that this really is a switch table and not a
#:    list of script effects.  These entries sit behind *multiple* consecutive
#:    NULs, so ``TOKEN_RE`` (strict single-NUL wrapping) misses the ones that do
#:    not happen to land on exactly one NUL on each side — that is how
#:    ``overnight`` and ``human_ai`` got lost while ``game_paused`` survived.
#:
#: Historical note worth keeping: an earlier audit searched the *filtered* token
#: file rather than the raw image and concluded these flags "do not exist in the
#: exe" (0 hits).  A direct byte scan disproves that.  Always re-verify against
#: the raw image before declaring a switch absent.
SWITCH_RE = re.compile(rb"\x00(-[a-z][a-z_0-9]{2,44})\x00")

#: Bare (undashed) names from the same launch-parameter table, including the
#: inline-default form ``name value``.  Padded-NUL runs are matched loosely on
#: the *left* only (``\x00+``) because the table separates entries with long
#: runs of NULs whose length is not constant.
#:
#: This pattern is intentionally *not* used to build the action vocabulary — it
#: matches far too much (≈9.7k hits on this binary) to be useful as a general
#: identifier source.  It exists so that :func:`collect_exe_switches` can prove
#: a switch is present when a caller already suspects one, and so the audit
#: trail records that the table is real.
BARE_SWITCH_RE = re.compile(
    rb"\x00{1,12}([a-z][a-z_0-9]{2,44}(?: [a-z_0-9]+)?)\x00{1,12}"
)

#: Anchor that brackets the launch-parameter table in ``stellaris.exe``.
#: The table is a run of short/long name pairs; ``humanai``/``human_ai`` is its
#: first pair and ``gamestatetimer`` is its last entry, so slicing between the
#: two gives a small, trustworthy window instead of a whole-binary guess.
_SWITCH_TABLE_START = b"humanai\x00human_ai"
_SWITCH_TABLE_END = b"gamestatetimer"


def extract_launch_switch_table(exe: Path) -> list[str]:
    """Return the launch-parameter table verbatim, as NUL-split strings.

    Direct byte scan of the region ``0x249bd20``.. reads::

        ... humanai | human_ai | daily_jobs_update | berserkai | berserk_ai |
        fullgalaxyspawn | overnight | game_paused false | gamestatetimer | ...

    Note the entries carry **no dash prefix** here; only ``-continuelastsave``
    is stored dash-prefixed, and it lives in a different pool
    (``frontendidler.cpp``).  ``game_paused false`` keeps its default inline.

    Raises ``SystemExit`` when the anchors are missing, which would mean either
    a different game build or a stripped binary — both cases where a silent
    empty list would be worse than a loud failure.
    """
    data = exe.read_bytes()
    start = data.find(_SWITCH_TABLE_START)
    if start < 0:
        raise SystemExit("找不到启动参数表锚点 humanai/human_ai")
    end = data.find(_SWITCH_TABLE_END, start)
    if end < 0:
        raise SystemExit("找不到启动参数表结束锚点 gamestatetimer")
    region = data[start: end + len(_SWITCH_TABLE_END)]
    return [p.decode("ascii") for p in region.split(b"\x00") if p]

USAGE_RE = r"^\s*{name}\s*[=]"

#: Verb prefixes, i.e. the engine's own taxonomy of what an effect does.
VERBS = (
    "add_",
    "set_",
    "remove_",
    "clear_",
    "create_",
    "destroy_",
    "give_",
    "grant_",
    "every_",
    "any_",
    "random_",
    "ordered_",
    "upgrade_",
    "downgrade_",
    "enable_",
    "disable_",
    "toggle_",
    "start_",
    "stop_",
    "abort_",
    "finish_",
    "complete_",
    "activate_",
    "deactivate_",
    "declare_",
    "join_",
    "leave_",
    "change_",
    "move_",
    "queue_",
    "build_",
    "hire_",
    "fire_",
    "kill_",
    "revive_",
    "spawn_",
    "unlock_",
    "lock_",
    "switch_",
    "transfer_",
    "reduce_",
    "increase_",
    "apply_",
)

#: Domain keywords -> gameplay domain.  First match wins, so put the most
#: specific domains first.  These are heuristics for *grouping a report*, not
#: for deciding what the executor may call.
DOMAINS: list[tuple[str, tuple[str, ...]]] = [
    ("战争与舰队", (
        "fleet", "ship", "navy", "war", "army", "bombard", "wargoal", "war_goal",
        "starbase", "military", "combat", "admiral", "general", "armada",
        "defense_army", "assault", "siege", "occupation", "casus", "peace",
    )),
    ("科技与传统", (
        "tech", "research", "tradition", "ascension", "perk", "ap_", "tr_",
        "technology", "insight", "breakthrough",
    )),
    ("外交与情报", (
        "diplo", "opinion", "envoy", "federation", "subject", "overlord",
        "agreement", "espionage", "spy", "intel", "first_contact", "truce",
        "alliance", "rival", "contact", "embassy", "galactic_community",
        "resolution", "senate", "vote",
    )),
    ("经济与建造", (
        "district", "building", "zone", "resource", "job", "pop", "market",
        "trade", "energy", "mineral", "food", "alloy", "consumer", "stockpile",
        "colony", "planet", "deposit", "blocker", "unemploy", "housing",
        "amenit", "crime", "stability", "edict", "policy", "budget",
    )),
    ("扩张与领土", (
        "colon", "outpost", "border", "system", "survey", "bypass", "gateway",
        "hyperlane", "territory", "claim", "megastructure",
    )),
    ("领袖与内政", (
        "leader", "council", "agenda", "faction", "election", "government",
        "civic", "ethic", "trait", "species", "governor", "minister",
    )),
    ("事件与脚本", (
        "event", "flag", "variable", "modifier", "effect", "trigger", "tooltip",
        "log", "hidden", "if", "while", "switch", "custom",
    )),
]


def collect_exe_tokens(exe: Path) -> set[str]:
    data = exe.read_bytes()
    return {m.group(1).decode("ascii") for m in TOKEN_RE.finditer(data)}


def collect_exe_switches(exe: Path, *, include_bare: bool = True) -> set[str]:
    """Harvest launch switches from the raw image.

    Kept separate from :func:`collect_exe_tokens` because those are identifiers
    (never dash-prefixed) and land in the *action vocabulary*, whereas switches
    are launch options.  Mixing them would pollute the action list; dropping
    them entirely is what caused the earlier "these flags do not exist"
    mis-conclusion.

    ``include_bare`` also returns the undashed entries from the launch-parameter
    table (``overnight``, ``human_ai``, ``game_paused false``, ...).  Those names
    are ordinary-looking and will collide with unrelated identifiers, so callers
    that need a clean answer must intersect them with the known table region or
    cross-check against ``docs/PROJECT_PLAN.md`` C6.
    """
    data = exe.read_bytes()
    found = {m.group(1).decode("ascii") for m in SWITCH_RE.finditer(data)}
    if include_bare:
        found |= {
            m.group(1).decode("ascii") for m in BARE_SWITCH_RE.finditer(data)
        }
    return found


def collect_script_corpus(game_dir: Path) -> str:
    chunks: list[str] = []
    for sub in ("common", "events"):
        root = game_dir / sub
        if not root.exists():
            continue
        for path in root.rglob("*.txt"):
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(chunks)


def domain_of(name: str) -> str:
    for domain, keys in DOMAINS:
        if any(k in name for k in keys):
            return domain
    return "其他"


def verb_of(name: str) -> str:
    for v in VERBS:
        if name.startswith(v):
            return v.rstrip("_")
    return "其他"


def mine(game_dir: Path, min_len: int = 4) -> dict:
    exe = game_dir / "stellaris.exe"
    if not exe.exists():
        raise SystemExit(f"找不到 {exe}")

    candidates = collect_exe_tokens(exe)
    corpus = collect_script_corpus(game_dir)

    # Single pass over the corpus.  The obvious formulation — compile one
    # regex per candidate and scan the whole corpus with it — is O(candidates x
    # corpus) and took over five minutes on this install.  Harvest every
    # assignment key once instead, then intersect; same result, seconds.
    usage: Counter[str] = Counter()
    for m in re.finditer(r"^\s*([a-z][a-z_0-9]{3,44})\s*=", corpus, re.M):
        usage[m.group(1)] += 1

    confirmed: dict[str, int] = {
        name: hits
        for name, hits in usage.items()
        if len(name) >= min_len and name in candidates
    }

    by_domain: dict[str, list[tuple[str, int]]] = defaultdict(list)
    by_verb: Counter[str] = Counter()
    for name, hits in confirmed.items():
        by_domain[domain_of(name)].append((name, hits))
        by_verb[verb_of(name)] += 1
    for names in by_domain.values():
        names.sort(key=lambda kv: (-kv[1], kv[0]))

    return {
        "exe_candidates": len(candidates),
        "confirmed": len(confirmed),
        "domains": {k: v for k, v in sorted(by_domain.items())},
        "verbs": dict(by_verb.most_common()),
    }


def render_markdown(data: dict) -> str:
    lines = [
        "# 群星脚本动作清单（从游戏本体挖出来，不是凭记忆列的）",
        "",
        "生成方式：",
        "1. 从 `stellaris.exe` 提取引擎的脚本名表（NUL 分隔的小写 token）；",
        "2. 只保留**在官方脚本语料里真的以 effect 形式被调用过**的名字（`name =` / `name = {`）；",
        "3. 按动词前缀与领域关键词归类，并附上官方脚本里的调用次数作为证据。",
        "",
        f"- exe 候选 token：**{data['exe_candidates']}**",
        f"- 经官方脚本确认可用的名字：**{data['confirmed']}**",
        "",
        "> 「确认」= 这个名字在官方脚本里被调用过。它是**可用的上界**，",
        "> 不代表每一个都能在玩家帝国上安全使用（有些是危机/事件专用）。",
        "",
        "## 按动词分布",
        "",
        "| 动词 | 数量 |",
        "|---|---|",
    ]
    for verb, count in data["verbs"].items():
        lines.append(f"| `{verb}_` | {count} |")

    lines += ["", "## 按领域分布", "", "| 领域 | 名字数 | 高频样例（官方调用次数） |", "|---|---|---|"]
    for domain, names in data["domains"].items():
        if domain == "其他":
            continue
        sample = "、".join(f"`{n}`({c})" for n, c in names[:8])
        lines.append(f"| {domain} | {len(names)} | {sample} |")

    lines += ["", "## 全部领域明细", ""]
    for domain, names in data["domains"].items():
        lines.append(f"### {domain}（{len(names)}）")
        lines.append("")
        for name, hits in names:
            lines.append(f"- `{name}` — {hits}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="挖掘群星的脚本动作清单")
    ap.add_argument("--game-dir", default=r"D:/SteamLibrary/steamapps/common/Stellaris")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "docs"))
    args = ap.parse_args(argv)

    game_dir = Path(args.game_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = mine(game_dir)
    (out_dir / "ACTION_VOCABULARY.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "ACTION_VOCABULARY.md").write_text(
        render_markdown(data), encoding="utf-8"
    )

    print(f"exe 候选 token : {data['exe_candidates']}")
    print(f"官方脚本确认   : {data['confirmed']}")
    print()
    for domain, names in data["domains"].items():
        print(f"  {domain:<12} {len(names)}")
    print()
    print(f"已写出 {out_dir / 'ACTION_VOCABULARY.md'}")
    print(f"已写出 {out_dir / 'ACTION_VOCABULARY.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
