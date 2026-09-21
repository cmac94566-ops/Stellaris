"""把自治层的 ``log`` 消息从"只说是哪类动作"升级为"带星球名 + 具体对象"。

为什么要写脚本而不是手工改
--------------------------
23 处 ``log`` 里，同一条消息（``autonomy built a district``）出现 9 次，
但每处的 **上下文不同**：有的是 ``district_generator``、有的是 ``district_mining``，
分布在 ``RELIEF`` / ``HOUSING`` / ``JOBS`` 等不同相位。手工改极易张冠李戴 ——
给建发电区划的那行写成"建了矿区划"，而日志本身又不会报错，错了也看不出来。

所以这里**不改逐条文本**，而是：
1. 定位每一条 ``log = "OVERMIND: ..."``；
2. 向上回溯若干行，从同一个 ``random_owned_planet`` 块里抽出真正决定动作的标识符
   （``add_district`` 的 ``district_type``、``add_zone`` 的 ``zone``、
   ``add_building`` 的 ``building``）；
3. 用抽到的东西生成消息，并附上 ``[This.GetName]``。

做法保守：**只替换字符串字面量**，不动任何结构性字段（``if`` / ``limit`` /
``random_owned_planet`` 的层级一个都不碰）。所以嵌套深度不变，
``_max_scripted_effect_depth`` 的结论继续成立。

如果某条 log 回溯不到标识符，脚本**原样保留**并在末尾列出，而不是猜一个填进去。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = Path(
    "mod/stellaris_overmind/common/scripted_effects/overmind_autonomy.txt"
)

#: How far up to look for the identifier that decides what is being built.
#: 40 lines comfortably covers the largest block between `if` and `log`
#: (the building blocks run ~19 lines).
LOOKBACK = 40

#: Extract the value of a nested key, e.g. ``district_type = district_mining``.
def _find_key(block: str, key: str) -> str | None:
    pat = re.compile(rf"\b{re.escape(key)}\s*=\s*([A-Za-z_][A-Za-z_0-9]*)")
    # last occurrence wins: the `add_*` call sits closest to the log line
    hits = pat.findall(block)
    return hits[-1] if hits else None


#: Branch openers.  A window must never cross one of these, or the search walks
#: into a sibling branch and picks up *its* identifiers.
_BRANCH_RE = re.compile(r"^\s*(?:else_if|else|if)\s*=\s*\{")


def _scope_window(lines: list[str], line_no: int, max_lines: int = LOOKBACK) -> str:
    """Return the text of the innermost branch enclosing ``lines[line_no]``.

    The naive version — a fixed-size lookback — crosses ``else_if`` boundaries.
    That is not a theoretical concern: it made a block whose ``add_zone`` reads
    ``zone = zone_foundry`` report ``zone_industrial``, because a sibling branch
    further up also wrote ``zone =``.  A wrong-but-plausible log line is worse
    than no log line, since nothing ever errors out.

    So the window is trimmed upward to the nearest branch opener.  ``limit``
    blocks are *not* treated as boundaries (they are inside the branch we want),
    and neither are ``random_owned_planet`` scopes.
    """
    start = max(0, line_no - max_lines)
    for i in range(line_no - 1, start - 1, -1):
        if _BRANCH_RE.match(lines[i]):
            start = i + 1
            break
    return "\n".join(lines[start:line_no])


#: Map the bare identifier to a human-readable Chinese action verb phrase.
#: Keys are the identifiers that actually appear in this file.
DISTRICT_LABEL = {
    "district_generator": "发电区划",
    "district_mining": "采矿区划",
    "district_farming": "农业区划",
    "district_city": "城市区划",
}
ZONE_LABEL = {
    "zone_industrial": "工业区",
    "zone_foundry": "铸造区",
    "zone_factory": "消费品区",
    "zone_research": "研究区",
    "zone_trade": "贸易区",
    "zone_unity": "凝聚力区",
}
#: Building names are free-form; fall back to the raw id when unknown.
#: Verified against common/buildings/ — these three are the only ones the
#: autonomy layer builds today.
BUILDING_LABEL = {
    "building_holo_theatres": "全息剧场",
    "building_luxury_residence": "豪华住宅",
    "building_research_lab_1": "一级研究实验室",
}


def label_for(kind: str, ident: str) -> str:
    """Return the display name for an identifier, or ``''`` when unknown.

    Returning empty (rather than echoing the id) lets callers print either
    ``名字（id）`` or just ``id``, instead of the redundant
    ``建筑building_holo_theatres（building_holo_theatres）`` that an
    echo-the-id fallback produced.
    """
    if kind == "district":
        return DISTRICT_LABEL.get(ident, "")
    if kind == "zone":
        return ZONE_LABEL.get(ident, "")
    return BUILDING_LABEL.get(ident, "")


def _named(kind: str, ident: str) -> str:
    """``名字（id）`` when the id is known, else the bare id."""
    label = label_for(kind, ident)
    return f"{label}（{ident}）" if label else ident


def rebuild(line_no: int, lines: list[str]) -> str | None:
    """Return a replacement log line for ``lines[line_no]``, or None to skip."""
    original = lines[line_no]
    m = re.search(r'log\s*=\s*"(OVERMIND: [^"]*)"', original)
    if not m:
        return None
    old_msg = m.group(1)

    window = _scope_window(lines, line_no)
    indent = original[: len(original) - len(original.lstrip())]

    place = "[This.GetName]"

    if "built a district" in old_msg:
        ident = _find_key(window, "district_type")
        if not ident:
            return None
        return (
            f'{indent}log = "OVERMIND: 建了{_named("district", ident)}于 {place}"'
        )

    if "built a zone" in old_msg:
        # Only trust `zone` inside the add_zone block.  A bare `zone =` search
        # previously reached into the *next* else_if branch and reported
        # zone_industrial for a block whose add_zone said zone_foundry —
        # exactly the cross-branch mix-up this script exists to avoid.
        add_zone = None
        for z in re.finditer(r"add_zone\s*=\s*\{([^}]*)\}", window):
            add_zone = z  # keep the last one inside our own scope window
        ident = _find_key(add_zone.group(1), "zone") if add_zone else None
        if not ident:
            return None
        return (
            f'{indent}log = "OVERMIND: 建了{_named("zone", ident)}于 {place}"'
        )

    if "built a building" in old_msg:
        # Buildings use the compact form `add_building = building_holo_theatres`,
        # not a nested block, so a generic `building =` search finds nothing.
        bld = re.search(
            r"\badd_building\s*=\s*([A-Za-z_][A-Za-z_0-9]*)", window
        )
        if not bld:
            return None
        ident = bld.group(1)
        return (
            f'{indent}log = "OVERMIND: 建了建筑{_named("building", ident)}于 {place}"'
        )

    # The remaining messages are already specific enough (they name the action
    # and have no per-object ambiguity), so only attach the location where the
    # enclosing scope is a planet.
    if "raised an assault army" in old_msg:
        return f'{indent}log = "OVERMIND: 征募了一支突击陆战队于 {place}"'
    if "rebuilt a border outpost" in old_msg:
        return f'{indent}log = "OVERMIND: 重建了边境前哨站"'
    if "granted an ascension perk" in old_msg:
        return f'{indent}log = "OVERMIND: 授予了一项飞升天赋"'

    return None


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    lines = text.splitlines()

    changed: list[tuple[int, str, str]] = []
    skipped: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        if 'log = "OVERMIND' not in line:
            continue
        new = rebuild(i, lines)
        if new is None:
            kept = re.search(r'"(OVERMIND: [^"]*)"', line)
            skipped.append((i + 1, kept.group(1) if kept else line.strip()))
            continue
        if new.strip() != line.strip():
            changed.append((i + 1, line.strip(), new.strip()))
            lines[i] = new

    if "--check" in sys.argv:
        print(f"将修改 {len(changed)} 处，跳过 {len(skipped)} 处")
        for ln, _old, new in changed:
            print(f"  L{ln}: {new}")
        print()
        for ln, msg in skipped:
            print(f"  [跳过] L{ln}: {msg}")
        return 0

    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已修改 {len(changed)} 处，跳过 {len(skipped)} 处")
    for ln, old, new in changed:
        print(f"  L{ln}")
        print(f"    - {old}")
        print(f"    + {new}")
    if skipped:
        print()
        print("未改动（脚本无法确定具体对象，保留原样）：")
        for ln, msg in skipped:
            print(f"  L{ln}: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
