"""Check whether Overmind directives actually reached the game.

The engine writes directives to <user dir>/mod/stellaris_overmind/ai_bridge/.
The injector turns them into `event overmind.NNN <country_id>` console commands.
Only after the game runs those events will the save contain Overmind markers.

This script is the definitive end-to-end check: it scans the newest autosave
and reports whether any empire carries Overmind modifiers / flags / policies.

Usage:
    py -3.12 check_overmind.py
"""

from __future__ import annotations

import glob
import os
import re
import sys
import zipfile

SAVE_DIR = r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games"
MOD_MARKERS = [
    "overmind_active",
    "overmind_aggressive",
    "overmind_defensive",
    "overmind_full_assault",
    "overmind_expansion_focus",
    "overmind_military_focus",
    "overmind_economy_focus",
    "overmind_research_focus",
    "overmind_diplomacy_focus",
    "overmind_war_preparation",
    "overmind_defense_focus",
    "overmind_consolidation",
    "overmind_colonize_focus",
    "overmind_starbase_focus",
    "overmind_espionage_focus",
]


def newest_save() -> str | None:
    saves = sorted(
        glob.glob(os.path.join(SAVE_DIR, "**", "*.sav"), recursive=True),
        key=os.path.getmtime,
    )
    return saves[-1] if saves else None


def main() -> int:
    path = newest_save()
    if not path:
        print("未找到任何存档。")
        return 1

    print(f"最新存档: {os.path.basename(path)}")
    print(f"路径:     {path}")
    print(f"修改时间: {os.path.getmtime(path):.0f}")
    print()

    with zipfile.ZipFile(path) as z:
        meta = z.read("meta").decode("utf-8", "replace")
        mods_on = "overmind" in meta.lower()
        print(f"[1] 存档 mod 列表含 Overmind: {mods_on}")
        if not mods_on:
            print("    -> 这局是在启用 mod 之前开的，mod 对它无效。请开新档。")

        gamestate = z.read("gamestate").decode("utf-8", "replace")

    print(f"[2] gamestate 大小: {len(gamestate) / 1e6:.1f} MB")
    print()

    hits = {m: len(re.findall(m, gamestate)) for m in MOD_MARKERS}
    total = sum(hits.values())

    print("[3] Overmind 痕迹扫描:")
    for marker, count in hits.items():
        flag = "OK " if count else "-- "
        print(f"    {flag}{marker:<32} {count}")

    print()
    if total == 0:
        print("结论: 游戏里还没有任何 Overmind 效果。")
        print("      可能原因: 指令还没注入 / 注入后还没到下一次月度存档 / 这局没加载 mod。")
        return 2

    print(f"结论: 生效中 —— 共 {total} 处 Overmind 痕迹。")
    print("      在游戏里把鼠标悬停到该帝国图标上，能看到带 'Overmind:' 前缀的修正条目。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
