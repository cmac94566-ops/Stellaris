"""生成文件的括号必须平衡、且每个块都要闭合。

为什么需要单独一个文件
----------------------
``tests/test_script_audit.py`` 关心的是"用了不存在的 key"这类语义问题，
括号平衡它不管。而 Clausewitz 对**文件末尾少一个 ``}``** 的处理相当宽容 ——
引擎会照常加载，不一定报错，于是错误表现为难查的行为异常而不是启动失败。

这个 bug 真实发生过（2026-09-15）：``build_tradition_charge`` 里
``afford_lines`` 收了尾（``"]}"``），``charge_lines`` 漏了同一行，
结果 ``overmind_charge_tradition`` 永远没有右括号，整个
``overmind_charges.txt`` 在块中间就结束了。

计数要用 ``strip_comments`` 之后的文本 —— 本项目的注释里合法地出现过
``{per_tree}``、``{count}`` 这类占位符，直接数字符会误报。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.script_audit import strip_comments  # noqa: E402

MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"
SCRIPT_DIRS = (
    MOD_DIR / "common/scripted_effects",
    MOD_DIR / "common/scripted_triggers",
    MOD_DIR / "common/scripted_variables",
    MOD_DIR / "common/on_actions",
    MOD_DIR / "common/edicts",
    MOD_DIR / "events",
)


def _script_files() -> list[Path]:
    out: list[Path] = []
    for d in SCRIPT_DIRS:
        if d.exists():
            out.extend(sorted(d.rglob("*.txt")))
    return out


def test_script_files_exist() -> None:
    """守卫本测试自身：目录挪了要立刻发现，而不是静默零覆盖。"""
    files = _script_files()
    assert len(files) > 10, f"只找到 {len(files)} 个脚本文件，目录结构可能变了"


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_braces_balance(path: Path) -> None:
    """去掉注释后，每个文件的 ``{`` 与 ``}`` 必须配平且末深度为 0。"""
    text = strip_comments(path.read_text(encoding="utf-8"))

    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        assert depth >= 0, (
            f"{path.name} 出现了多余的右括号 —— 在某个位置深度降到负，"
            "说明闭合括号比开括号多"
        )

    opens, closes = text.count("{"), text.count("}")
    assert opens == closes, (
        f"{path.name}: 开括号 × {opens}，闭括号 × {closes}，差 {opens - closes}。"
        "生成器少写了收尾括号？见 build_tradition_charge 的注释。"
    )
    assert depth == 0, (
        f"{path.name} 末尾嵌套深度是 {depth} 而不是 0 —— "
        f"有 {depth} 个块没闭合"
    )


def test_generated_files_close_every_top_level_block() -> None:
    """所有生成文件里，每个顶层 effect/trigger 都必须被闭合。

    这条比单纯的计数更贴近症状：``overmind_charge_tradition`` 那次是
    **块没闭合**，而文件总计数只差 1，光看总数不容易联想到"哪个块漏了"。
    这里逐块报告名字，让失败信息直接指向责任块。
    """
    generated = (
        MOD_DIR / "common/scripted_effects/overmind_charges.txt",
        MOD_DIR / "common/scripted_effects/overmind_progression.txt",
        MOD_DIR / "common/scripted_triggers/overmind_afford.txt",
    )
    for path in generated:
        text = strip_comments(path.read_text(encoding="utf-8"))
        lines = text.splitlines()

        depth = 0
        unclosed: list[str] = []
        for line in lines:
            stripped = line.strip()
            # 顶层块：深度为 0 时以 'name = {' 开头的行
            if depth == 0 and stripped.endswith("{") and "=" in stripped:
                unclosed.append(stripped)
            depth += line.count("{") - line.count("}")
            if depth == 0 and unclosed:
                unclosed.pop()

        assert depth == 0, (
            f"{path.name} 末尾深度 {depth}，未闭合的块: {unclosed}"
        )
