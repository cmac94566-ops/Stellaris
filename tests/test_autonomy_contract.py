"""Contract tests between the hand-written executor and the generated tables.

The autonomy layer is split in two on purpose:

* ``overmind_autonomy.txt`` — hand-written, holds the *policy* (what to do).
* ``overmind_charges.txt`` / ``overmind_progression.txt`` — generated from game
  data, hold the *facts* (prices, legal IDs), because a hand-typed price or ID
  is a silent failure waiting to happen.

That split only works if something checks the seam.  These tests do:

* every ``AFFORD =`` / ``CHARGE =`` name the executor calls must exist in the
  generated file, and must come in matching pairs — a charge with no matching
  affordability gate would conjure resources out of an empty treasury;
* every ``om_lex_*`` variable the executor reads must be one the Lex compiler
  actually writes — an unset variable reads as 0 and silently skips a whole
  phase;
* every phase the compiler can schedule must have an executor branch, and vice
  versa.

None of this needs the game installed: the generated files are committed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine import strategy_lex as lex  # noqa: E402
from engine.costs import (  # noqa: E402
    AFFORD_RELPATH,
    CHARGES_RELPATH,
    PROGRESSION_RELPATH,
    TABLE_RELPATH,
)
from engine.script_audit import strip_comments  # noqa: E402

MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"
AUTONOMY = MOD_DIR / "common/scripted_effects/overmind_autonomy.txt"
TRIGGERS = MOD_DIR / "common/scripted_triggers/overmind_autonomy_triggers.txt"
#: The generated affordability gates live here, **not** next to the charges.
#: Clausewitz keeps two registries: a top-level block in ``scripted_triggers/``
#: is registered as a trigger, the same block in ``scripted_effects/`` is not.
#: Mixing them (as an earlier revision did) made every ``overmind_afford_*``
#: silently unregister, which the game reported at runtime as
#: ``Error in scripted trigger, cannot find: overmind_afford_...``.
AFFORD_FILE = MOD_DIR / AFFORD_RELPATH


def _read(path: Path) -> str:
    return strip_comments(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def executor() -> str:
    return _read(AUTONOMY)


@pytest.fixture(scope="module")
def charges() -> str:
    return _read(MOD_DIR / CHARGES_RELPATH)


@pytest.fixture(scope="module")
def afford() -> str:
    """The generated affordability **triggers** (separate registry)."""
    return _read(AFFORD_FILE)


@pytest.fixture(scope="module")
def generated_names() -> set[str]:
    """Every name the generator defines, across both registries."""
    names: set[str] = set()
    for rel in (AFFORD_RELPATH, CHARGES_RELPATH, PROGRESSION_RELPATH):
        names |= _defined_names(_read(MOD_DIR / rel))
    return names


@pytest.fixture(scope="module")
def sidecar() -> dict:
    return json.loads((MOD_DIR / TABLE_RELPATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generated_text() -> str:
    """Everything the generator owns, concatenated.

    Some generated effects (the tradition and perk chains) read ``om_lex_*``
    variables themselves, so the variable contract has to span both sides of
    the seam, not just the hand-written file.
    """
    return "\n".join(
        (MOD_DIR / rel).read_text(encoding="utf-8")
        for rel in (AFFORD_RELPATH, CHARGES_RELPATH, PROGRESSION_RELPATH)
    )


def _defined_names(text: str) -> set[str]:
    return set(re.findall(r"^([a-z_0-9]+)\s*=\s*\{", text, re.M))


def _referenced_names(text: str, names: list[str]) -> set[str]:
    """Which of ``names`` appear anywhere in ``text``.

    A membership test, not a `KEY = name` match: the executor calls generated
    effects both as parameters (``CHARGE = x``) and directly (``x = yes``), and
    an earlier version of this helper only understood the first form — which
    made it report used entries as unused.
    """
    return {n for n in names if re.search(rf"\b{re.escape(n)}\b", text)}


def _scripted_effect_bodies(sources: list[str]) -> dict[str, str]:
    """把若干脚本原文里的顶层 ``name = { ... }`` 全部取出，name -> body。"""
    bodies: dict[str, str] = {}
    for text in sources:
        for m in re.finditer(r"^([a-z_0-9]+)\s*=\s*\{", text, re.M):
            name = m.group(1)
            depth, i = 1, m.end()
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            bodies[name] = text[m.end() : i - 1]
    return bodies


def _max_scripted_effect_depth(entry: str, sources: list[str]) -> int:
    """从 ``entry`` 出发的最长 scripted effect 调用链层数。

    只统计「调用另一个 scripted effect」这一种边——scope 切换
    (``random_owned_planet`` / ``owner``) 和普通语句不占引擎的 effect 嵌套预算，
    记进去会把账算错，这正是一次误判的来源。

    实现上用显式栈做 DFS，路径上带环检测（A->B->A 直接停）。注意不要把
    "已经访问过" 当成 "可以剪枝"：同一条链里不同位置到达同一个 effect，
    对最长路径的贡献是不一样的。第一版就是这么写错的，结果新旧结构都算成同一个
    数字，看起来"通过"其实什么也没测。
    """
    bodies = _scripted_effect_bodies(sources)

    def calls_in(name: str) -> list[str]:
        body = bodies.get(name, "")
        return [
            c
            for c in re.findall(r"\b([a-z_0-9]+)\s*=\s*(?:yes|\{)", body)
            if c in bodies and c != name
        ]

    if entry not in bodies:
        raise AssertionError(f"找不到入口脚本效果：{entry}")

    longest = 1
    # 栈元素：(当前 effect, 已走过的路径)，路径用于环检测
    stack: list[tuple[str, tuple[str, ...]]] = [(entry, ())]
    while stack:
        name, path = stack.pop()
        here = len(path) + 1
        longest = max(longest, here)
        new_path = path + (name,)
        for child in calls_in(name):
            if child in new_path:  # 环，停
                continue
            stack.append((child, new_path))
    return longest


# ---------------------------------------------------------------------------
# affordability / charge pairing
# ---------------------------------------------------------------------------
def _charges_used(executor: str) -> set[str]:
    """执行层里出现的所有扣费效果名。

    历史上有两种写法，内联压平（见 test_progression_nesting_stays_under_the_cap）
    之后只剩后者，但两种都认，避免测试比实现更脆：
      * 参数传递：``CHARGE = overmind_charge_x``
      * 直接调用：``overmind_charge_x = yes``
    """
    used = set(re.findall(r"CHARGE = (\S+)", executor))
    used |= set(re.findall(r"\b(overmind_charge_[a-z_0-9]+)\s*=\s*yes", executor))
    return used


def _affords_used(executor: str) -> set[str]:
    """执行层里出现的所有 affordability 触发器名（同上，两种写法都认）。"""
    used = set(re.findall(r"AFFORD = (\S+)", executor))
    used |= set(re.findall(r"\b(overmind_afford_[a-z_0-9]+)\s*=\s*yes", executor))
    return used


def test_every_charge_has_an_afford_gate(executor: str) -> None:
    charges = _charges_used(executor)
    affords = _affords_used(executor)
    assert charges, "执行层里没有找到任何 CHARGE 调用"
    for charge in sorted(charges):
        expected = charge.replace("overmind_charge_", "overmind_afford_", 1)
        assert expected in affords, (
            f"{charge} 没有配套的 affordability 触发器 ({expected})"
            "—— 那会在国库为空时凭空建出东西"
        )


def test_charge_and_afford_names_are_defined(
    executor: str, generated_names: set[str]
) -> None:
    used = set(re.findall(r"(?:AFFORD|CHARGE) = (\S+)", executor))
    missing = sorted(used - generated_names)
    assert missing == [], f"执行层调用了生成文件里不存在的名字：{missing}"


def test_afford_triggers_are_in_the_trigger_registry(afford: str) -> None:
    """The affordability gates must be *triggers*, in the triggers directory.

    Regression guard for a real defect: the gates were originally emitted into
    the same file as the charges, under ``scripted_effects/``.  Effects
    registered fine, so nothing looked wrong — but every gate was silently
    dropped, and the game logged ``Error in scripted trigger, cannot find:
    overmind_afford_district`` (15 of them, on real hardware 2026-09-15).

    A name being *defined somewhere* is not enough; it has to be defined in the
    registry that will be consulted.
    """
    defined = _defined_names(afford)
    afford_names = {n for n in defined if n.startswith("overmind_afford_")}
    assert afford_names, "触发器文件里没有任何 overmind_afford_* 定义"
    # And the effects side must NOT define them, or the split has regressed.
    charges = _read(MOD_DIR / CHARGES_RELPATH)
    leaked = {n for n in _defined_names(charges) if n.startswith("overmind_afford_")}
    assert leaked == set(), (
        f"这些 affordability 触发器被放进了 effects 文件，不会被注册：{sorted(leaked)}"
    )


def test_generated_names_match_the_sidecar(
    generated_names: set[str], sidecar: dict
) -> None:
    for name in sidecar["charge_effect_names"] + sidecar["afford_trigger_names"]:
        assert name in generated_names, f"sidecar 声明了 {name}，生成文件里却没有定义"


def test_every_generated_charge_is_actually_used(sidecar: dict, executor: str) -> None:
    """A generated charge nobody calls is a lost call site, not just dead weight."""
    names = sidecar["charge_effect_names"] + sidecar["afford_trigger_names"]
    used = _referenced_names(executor, names)
    unused = sorted(set(names) - used)
    assert unused == [], f"生成了但执行层没用到的扣费/触发器：{unused}"


# ---------------------------------------------------------------------------
# Lex variable contract
# ---------------------------------------------------------------------------
def test_executor_only_reads_variables_the_compiler_writes(
    executor: str, generated_text: str
) -> None:
    written = set(lex.parse_lex_effects(lex.render_lex_files(lex.LexPayload(rev=1))[1]))
    read = set(
        re.findall(r"which = (om_lex_[a-z_0-9]+)", executor + "\n" + generated_text)
    )
    assert read, "执行层没有读取任何法案变量"
    unknown = sorted(read - written)
    assert unknown == [], f"执行层读了编译器不会写的变量（永远是 0）：{unknown}"


def test_compiler_writes_no_variable_nobody_reads(
    executor: str, generated_text: str
) -> None:
    """A knob the compiler sets but nothing reads is a lie in the interface.

    This is how ``om_lex_threat`` was caught: it was written every revision and
    read by nobody, so the LLM's threat assessment had no effect at all.
    """
    written = set(lex.parse_lex_effects(lex.render_lex_files(lex.LexPayload(rev=1))[1]))
    read = set(
        re.findall(r"which = (om_lex_[a-z_0-9]+)", executor + "\n" + generated_text)
    )
    ignored = sorted(written - read - {"om_lex_rev"})
    assert ignored == [], f"编译器写了但执行层从不使用的变量：{ignored}"


def test_mod_lex_file_is_in_sync_with_the_renderer() -> None:
    """mod 法案与编译器的**变量接口**必须一致（S-18/M2-4 起允许 rev 与旋钮值不同）。

    旧口径是字节级全等（rev 固定 1 的时代）；换版管道（M2-4）与 LLM 出案
    （source=llm）都会合法地改变 rev 与旋钮值，故改为：磁盘法案所 set 的
    om_lex_* 变量**集合**必须与编译器渲染的集合一致 —— 接口漂移即红，
    值的演进放行。
    """
    rendered = lex.render_lex_files(lex.LexPayload(rev=1, source="default"))[1]
    on_disk = (MOD_DIR / lex.LEX_EFFECTS_RELPATH).read_text(encoding="utf-8")
    disk_keys = set(lex.parse_lex_effects(on_disk))
    render_keys = set(lex.parse_lex_effects(rendered))
    assert disk_keys == render_keys, (
        f"mod 法案变量接口与编译器漂移："
        f"多 {sorted(disk_keys - render_keys)} / 少 {sorted(render_keys - disk_keys)}"
        " —— 先跑 engine/strategy_lex.py 重新生成，或同步更新渲染器"
    )


# ---------------------------------------------------------------------------
# Phase coverage
# ---------------------------------------------------------------------------
def test_every_phase_has_an_executor_effect(executor: str) -> None:
    defined = _defined_names(executor)
    for code, name in lex.PHASE_NAMES.items():
        assert f"overmind_autonomy_phase_{name}" in defined, (
            f"相位 {code}({name}) 没有执行体"
        )


def test_run_slot_dispatches_every_phase(executor: str) -> None:
    body = re.search(
        r"^overmind_autonomy_run_slot\s*=\s*\{(.*?)^\}", executor, re.M | re.S
    )
    assert body, "找不到 overmind_autonomy_run_slot"
    dispatched = {int(n) for n in re.findall(r"which = \$SLOT\$ value = (\d+)", body.group(1))}
    assert dispatched == set(lex.PHASE_NAMES), (
        f"日程分发器漏了相位：{sorted(set(lex.PHASE_NAMES) - dispatched)}"
    )


def test_agenda_has_one_slot_per_phase() -> None:
    assert lex.AGENDA_SLOTS == len(lex.PHASE_NAMES) == 8
    assert len(lex.DEFAULT_AGENDA) == lex.AGENDA_SLOTS
    assert sorted(lex.DEFAULT_AGENDA) == sorted(lex.PHASE_NAMES)


def test_tick_resets_every_monthly_quota(executor: str) -> None:
    """Each per-month quota flag must be cleared, or a phase dies after month 1."""
    body = re.search(r"^overmind_autonomy_tick\s*=\s*\{(.*?)^\}", executor, re.M | re.S)
    assert body
    tick = body.group(1)
    set_flags = set(re.findall(r"set_country_flag = (om_did_[a-z_0-9]+)", executor))
    cleared = set(re.findall(r"remove_country_flag = (om_did_[a-z_0-9]+)", tick))
    assert set_flags - cleared == set(), (
        f"这些限额标记从未被清零，对应相位只会触发一次：{sorted(set_flags - cleared)}"
    )


# ---------------------------------------------------------------------------
# Compiler validation
# ---------------------------------------------------------------------------
def test_payload_defaults_are_valid() -> None:
    assert lex.validate_payload(lex.LexPayload(rev=1)) == []


@pytest.mark.parametrize(
    "payload,needle",
    [
        (lex.LexPayload(rev=1, slots=[1, 2, 3, 4, 5, 6, 7]), "slots"),
        (lex.LexPayload(rev=1, slots=[1, 2, 3, 4, 5, 6, 7, 9]), "phase code"),
        (lex.LexPayload(rev=1, threat=9), "threat"),
        (lex.LexPayload(rev=1, bottleneck=0), "bottleneck"),
        (lex.LexPayload(rev=1, stance=7), "stance"),
        (lex.LexPayload(rev=1, focus_zone=6), "focus zone"),
        (lex.LexPayload(rev=1, tradition_mode=5), "tradition mode"),
        (lex.LexPayload(rev=1, ap_mode=0), "perk mode"),
        (lex.LexPayload(rev=1, bombardment=4), "bombardment"),
        (lex.LexPayload(rev=0), "revision"),
    ],
)
def test_illegal_payloads_are_rejected(payload: lex.LexPayload, needle: str) -> None:
    problems = lex.validate_payload(payload)
    assert problems, f"非法输入 {needle} 竟然通过了校验"
    assert any(needle in p for p in problems), problems


def test_render_round_trips_through_the_parser() -> None:
    payload = lex.LexPayload(
        rev=42, tradition_mode=3, ap_mode=4, bombardment=3, focus_zone=2, stance=1
    )
    _vars_text, effects = lex.render_lex_files(payload)
    parsed = lex.parse_lex_effects(effects)
    assert parsed["om_lex_rev"] == 42
    assert parsed["om_lex_tradition_mode"] == 3
    assert parsed["om_lex_ap_mode"] == 4
    assert parsed["om_lex_bombardment"] == 3
    assert parsed["om_lex_focus_zone"] == 2
    assert parsed["om_lex_stance"] == 1
    for index, phase in enumerate(payload.slots, start=1):
        assert parsed[f"om_lex_slot{index}"] == phase


def test_rendered_free_text_cannot_break_the_script() -> None:
    """The LLM never emits script, but its *focus* text is echoed into a comment
    and its sanitised form into a log line.  Nothing user-controlled may reach
    the script layer unescaped — brackets used to produce a real
    "Invalid macro entry" error in this project."""
    focus = 'bad "quotes" [data_function] $macro$ ' + chr(92) + " backslash"
    payload = lex.LexPayload(rev=1, focus=focus, rationale="newline\nand { braces }")
    _vars_text, effects = lex.render_lex_files(payload)

    # Comments are inert to the parser; only live script matters here.
    live = "\n".join(
        line for line in effects.splitlines() if not line.lstrip().startswith("#")
    )
    assert live.count('"') == 2, f"log 行之外出现了引号：{live}"
    assert "[" not in live and "]" not in live and chr(92) not in live
    assert "\n" not in payload.focus


def test_hygiene_all_new_effect_names_are_mod_local() -> None:
    """NFR-04: every key the mod introduces is namespaced."""
    for path_text in (AUTONOMY.read_text(encoding="utf-8"), TRIGGERS.read_text(encoding="utf-8")):
        for name in re.findall(r"^([a-z][a-z_0-9]+)\s*=\s*\{", path_text, re.M):
            assert name.startswith(("overmind", "om_")), f"未加命名空间的键：{name}"


def test_progression_file_covers_every_mode() -> None:
    """Every mode's chain must be reachable from the file.

    The chains are **inlined** into three entry effects rather than split into
    per-mode effects (see ``test_progression_nesting_stays_under_the_cap`` for
    why), so the check is that each chain's traditions actually appear inside
    its entry effect, not that a separate effect exists per mode.
    """
    text = _read(MOD_DIR / PROGRESSION_RELPATH)
    from engine.costs import PERK_MODES, TRADITION_MODES

    assert "overmind_advance_tradition_tree = {" in text
    assert "overmind_advance_tradition_node = {" in text
    assert "overmind_grant_ascension_perk = {" in text

    # Every tree and every perk named in the mode tables must be granted
    # somewhere in the file — a chain that names a tree the generator silently
    # dropped would make that mode a no-op.
    for mode, (_name, chain) in TRADITION_MODES.items():
        for tree in chain:
            assert re.search(rf"add_tradition = tr_{re.escape(tree)}_", text), (
                f"传统模式 {mode} 的树 {tree} 在生成文件里没有任何授予语句"
            )
    for mode, (_name, chain) in PERK_MODES.items():
        for perk in chain:
            assert f"add_ascension_perk = {perk}" in text, (
                f"飞升模式 {mode} 的天赋 {perk} 在生成文件里没有授予语句"
            )


def test_progression_nesting_stays_under_the_cap() -> None:
    """No generated chain may exceed the engine's scripted-effect nesting cap.

    Regression guard for a defect that only shows up on real hardware.  The
    engine caps nesting at 5::

        CRITICAL: Max effects post init recursive depth of 5 reached ...

    When the cap is hit the engine abandons the rest of that branch's
    initialisation, and the *symptom* is misleading — ``add_tradition`` starts
    reporting "could not find tradition with key: tr_discovery_adopt" for keys
    that plainly exist in ``common/traditions/``.  It reads like bad IDs; it is
    actually a depth problem.

    The offending shape was::

        tick -> run_slot -> phase_ascension -> advance_tradition_tree
             -> adopt_tree_tech -> add_tradition                     = 6

    One dispatcher layer was removed by inlining the per-mode bodies.  This
    test walks the generated effect graph and fails if any path from an entry
    effect to a granted tradition/perk reaches 6.
    """
    # Longest path from the monthly tick to a leaf, counted in scripted-effect
    # frames.  The engine's cap is 5.
    MAX_FRAMES = 5

    generated = _read(MOD_DIR / PROGRESSION_RELPATH) + _read(MOD_DIR / CHARGES_RELPATH)
    hand_written = _read(AUTONOMY)

    # The dispatch effects on the generated side must still exist.
    for name in (
        "advance_tradition_tree",
        "advance_tradition_node",
        "grant_ascension_perk",
    ):
        assert f"overmind_{name} = {{" in generated, f"生成文件里找不到 overmind_{name}"

    # --- 执行层（autonomy）也必须守同一条上限 ---
    #
    # 同样的坑在自治心跳里出现过第二次：``tick -> run_slot -> phase_relief
    # -> do_district -> （内部语句）`` 恰好是 5 层，引擎把该分支后续初始化全部
    # 放弃（实测 ``at least 272 effects left``）。症状同样是误导性的：不会报
    # "太深"，只会静默地少建东西。
    #
    # 现在 ``do_*`` 中间层已被内联进各 phase，链降到 4 层。下面这几条断言就是
    # 防止它被拆回去。
    for name in ("do_district", "do_zone", "do_building"):
        assert f"overmind_autonomy_{name} = {{" not in hand_written, (
            f"执行层的 {name} 中间层又被拆出来了——那会让 tick 到内部语句的链"
            "回到 5 层顶格，引擎会放弃该分支剩余初始化"
        )
    # 内联之后，相位必须自己持有行星选择，而不是转手调一个 helper。
    assert "random_owned_planet = {" in hand_written, (
        "相位里找不到 random_owned_planet——内联可能被撤销了"
    )
    # 而且扣费/门禁必须仍然在相位体内（不是被挪去了别处）。
    assert "overmind_afford_district_district_generator = yes" in hand_written, (
        "相位里找不到内联后的 affordability 门禁"
    )

    # --- 生成侧：内联的入口效果必须直接授予，不再转手 ---
    assert "overmind_adopt_tree_tech = yes" not in generated, (
        "传统链又变回两层分派了——那会让链深度回到 6，触发引擎的 5 层上限"
    )
    assert "overmind_perk_tech = yes" not in generated, (
        "飞升链又变回两层分派了——同上"
    )
    assert "overmind_node_discovery = yes" not in generated, (
        "传统节点链又变回两层分派了——同上"
    )
    # Sanity: the depth budget we assume must hold.
    assert MAX_FRAMES == 5

    # --- 真正的深度校验：走一遍脚本效果调用图 ---
    #
    # 注意把 event 的 immediate 块算作第 1 层——引擎就是这么数的。实测日志里那条
    # 超限链列出的 scripted effect 恰好 4 个（do_district / phase_relief /
    # run_slot / tick），而引擎报的是 "depth of 5"，差的那 1 层就是
    # ``events/overmind_autonomy_events.txt`` 里 ``immediate = { ... }`` 这个调用者。
    # 漏掉它会把预算算错一层，从而误判改动是否有效。
    depth_from_tick = _max_scripted_effect_depth(
        entry="overmind_autonomy_tick",
        sources=[hand_written, generated],
    )
    depth_total = depth_from_tick + 1  # +1 = event immediate 那一层
    assert depth_total <= MAX_FRAMES, (
        f"从 overmind.300 的 immediate 到最内层共有 {depth_total} 层 scripted effect，"
        f"超过引擎上限 {MAX_FRAMES}；超限分支会被引擎静默放弃（症状是少建东西、"
        "或报出并不存在的 key 错误）。预算：event(1) -> tick(2) -> run_slot(3) "
        "-> phase(4) -> 相位体内的调用(5)。"
    )


def test_no_stray_files_in_script_dirs() -> None:
    """脚本目录里不允许出现非 ``.txt`` 文件。

    回归守卫，针对一个非常隐蔽的坑：改结构时把原文件备份成
    ``overmind_autonomy.txt.pre_inline`` 就放在**同目录**，以为多了个后缀引擎不会认。
    实际上引擎会**加载任何文件名**——实测 error.log 立刻冒出 15 条

        Object with key: overmind_autonomy_phase_relief already exists,
        using the one at file: .../overmind_autonomy.txt.pre_inline

    并且因为两份定义冲突，"胜出"的是备份里的**旧结构**，于是刚修好的深度问题
    原样复现（``Max effects ... depth of 5 reached ... 272 effects left``），
    看日志会以为是修复无效，方向完全跑偏。

    备份请放到 mod 之外（例如仓库根的 ``_backup/``）。
    """
    script_dirs = [
        MOD_DIR / "common/scripted_effects",
        MOD_DIR / "common/scripted_triggers",
        MOD_DIR / "events",
        MOD_DIR / "common/edicts",
    ]
    offenders: list[str] = []
    for d in script_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix != ".txt":
                offenders.append(str(p.relative_to(MOD_DIR)))
    assert offenders == [], (
        "这些文件在脚本目录里，引擎会把它们当脚本加载，"
        "和正式定义冲突并可能用旧版本覆盖新版本：\n  " + "\n  ".join(offenders)
    )


def test_verify_boost_does_not_inline_ticks() -> None:
    """验收加速器不得用 while/for 循环体包住 tick。

    回归守卫，两条教训，代价是 681 条报错。

    教训一：``while`` 是**静态展开**的。第一版加速器写成::

        while = { limit = {...} overmind_autonomy_tick = yes ... }

    引擎解析期按迭代次数铺开循环体，24 轮 tick 各自展开整棵相位树，静态嵌套
    远超 5 层。引擎抛弃该分支初始化后，``add_tradition`` 在未初始化状态下被
    预检，于是**全部 54 个传统 key 都报 "could not find"**——包括
    tr_discovery_adopt 这种铁定存在的，极易误导成 ID 表错误。伴随一条
    ``at least 3552 effects left``。

    教训二：加速器不得成为 tick 前面的一层。tick 已占满 5 层预算
    （event1/tick2/run_slot3/phase4/内部5），前面套任何包装 effect 都会超限。
    """
    events = _read(MOD_DIR / "events/overmind_autonomy_events.txt")

    # 先确认 303 还在，否则这守卫是空转的
    assert "id = overmind.303" in events, "on_actions 挂了 overmind.303，但事件没了"

    # 教训一：事件文件里任何 while/for 都不许碰 tick
    for m in re.finditer(r"\b(while|for)\s*=\s*\{", events):
        depth, i = 1, m.end()
        while i < len(events) and depth > 0:
            if events[i] == "{":
                depth += 1
            elif events[i] == "}":
                depth -= 1
            i += 1
        body = events[m.end() : i - 1]
        assert "overmind_autonomy_tick" not in body, (
            f"{m.group(1)} 循环体里出现了 overmind_autonomy_tick。循环是静态展开的，"
            "会把整棵相位树重复铺开、瞬间超过引擎 5 层嵌套上限，症状是全部传统 key "
            "都报 could not find。请改用 country_event 自递归。"
        )

    # 教训二：tick 必须**直接**写在 303 的 immediate 里，不能经中间 effect 转手
    m303 = re.search(r"id = overmind\.303(.*?)\n\}\n", events, re.S)
    assert m303, "解析不出 overmind.303 的正文"
    body303 = m303.group(1)
    assert re.search(r"^\s*overmind_autonomy_tick = yes", body303, re.M), (
        "overmind.303 的 immediate 里没有直接调用 overmind_autonomy_tick。"
        "若改成经由某个包装 effect 调用，就在 tick 前多占一层，链会变 6 层从而被"
        "引擎静默放弃。"
    )
    before_tick = body303.split("overmind_autonomy_tick = yes")[0]
    interim = set(
        re.findall(r"^\s*(overmind_[a-z_0-9]+)\s*=", before_tick, re.M)
    )
    assert not interim, (
        f"303 在调用 tick 之前先调了这些 effect：{sorted(interim)}。"
        "那会给调用链多加一层，超过引擎 5 层上限。"
    )


# ---------------------------------------------------------------------------
# effect 容器型语句的深度守卫
# ---------------------------------------------------------------------------
#: 这些 effect 带一个 ``effect = { }`` 子容器，**该容器占一层嵌套预算**，
#: 而 ``_max_scripted_effect_depth`` 看不到它（它只走 scripted effect 调用边）。
#:
#: 这不是理论风险。本项目**三次**撞上 5 层上限，第三次正是 ``create_fleet``：
#:
#:     event.immediate(1) -> tick(2) -> run_slot(3) -> phase_military(4)
#:         -> create_military_fleet(5) -> effect(6)            = 6，超限
#:
#: ``create_military_fleet`` 的官方签名抄自 exe 自带的 effect 文档表::
#:
#:     Creates a military fleet with the designs of a specified country.
#:     create_military_fleet = { owner = <target> scaled_size = 1.0 effect = { } }
#:
#: 注意 ``if`` / ``limit`` **不占**预算（否则原实现早就超限了），
#: 只有真正开子作用域的容器才占。所以这里有针对性地只查容器型 effect。
EFFECT_CONTAINER_EFFECTS = ("create_fleet", "create_military_fleet")

#: 自治执行层的 5 层预算，从上往下数：
#:   1 event.immediate  2 tick  3 run_slot  4 phase_*  （5 = 相位体内的调用）
AUTONOMY_FRAMES = 5


def _block_body(text: str, header_re: str) -> str:
    """取 ``header_re`` 匹配到的块的大括号内容（不含首尾括号）。

    用法示例：``_block_body(text, r"overmind_autonomy_tick\\s*=\\s*\\{")``。
    找不到时抛 ``AssertionError`` —— 让调用方的断言失败信息更直白。
    """
    m = re.search(header_re, text)
    assert m, f"找不到块：{header_re}"
    d, i = 1, m.end()
    while i < len(text) and d > 0:
        if text[i] == "{":
            d += 1
        elif text[i] == "}":
            d -= 1
        i += 1
    return text[m.end() : i - 1]


def _container_depth_violations(text: str) -> list[tuple[str, int, str]]:
    """找出「已在深调用链里、还套了 effect 容器」的位置。

    做法：对每个顶层 effect 定义，先算它的 scripted-effect 链深度 ``base``；
    再在它的函数体里找容器型 effect。容器每多一层，就需要 ``base + 1`` 个
    预算位。返回值是 ``(effect 名, base, 违规的调用名)``。

    这里刻意**不做**完整的作用域树重建 —— 那套逻辑容易写错，而本项目已经
    因为"测试比实现更脆"吃过亏。我们只需要抓住"危险形状"：
    phase_* 这一层（base == 4）里不许出现 effect 容器。
    """
    bodies = _scripted_effect_bodies([text])
    out: list[tuple[str, int, str]] = []
    if "overmind_autonomy_tick" not in bodies:
        return out

    # 算每个 effect 相对 tick 的链深度
    def depth_from_tick(target: str) -> int | None:
        stack: list[tuple[str, int]] = [("overmind_autonomy_tick", 1)]
        seen: set[tuple[str, int]] = set()
        best: int | None = None
        while stack:
            name, d = stack.pop()
            if (name, d) in seen:
                continue
            seen.add((name, d))
            if name == target:
                best = d if best is None else max(best, d)
                continue
            body = bodies.get(name, "")
            for c in re.findall(r"\b([a-z_0-9]+)\s*=\s*(?:yes|\{)", body):
                if c in bodies and c != name:
                    stack.append((c, d + 1))
        return best

    for name, body in bodies.items():
        base = depth_from_tick(name)
        if base is None:
            continue  # 不在 tick 调用图里
        for eff in EFFECT_CONTAINER_EFFECTS:
            if re.search(rf"\b{eff}\s*=\s*\{{", body):
                out.append((name, base, eff))
    return out


def test_effect_containers_stay_within_the_nesting_budget() -> None:
    """带 ``effect`` 容器的语句不得出现在快要顶格的那一层。

    回归守卫，针对本项目第三次撞 5 层上限（2026-09-15 实机）::

        Script Error, cannot add ship to fleet! ...
        @ overmind_autonomy.txt:535 (overmind_autonomy_phase_military)

    修法是把这个调用**提到 tick 层**（见 ``overmind_autonomy_build_ship``
    顶部注释），链从 6 层降到 4 层。

    之所以要单独加这条守卫：``_max_scripted_effect_depth`` 只统计 scripted
    effect 之间的调用边，看不到 ``create_military_fleet`` / ``create_fleet``
    自带的 ``effect = { }`` 容器。于是**第一次修完之后测试全绿、实机仍会超限**。
    """
    text = _read(AUTONOMY)
    violations = _container_depth_violations(text)
    offending = [v for v in violations if v[1] + 1 > AUTONOMY_FRAMES]
    assert not offending, (
        "这些 effect 已经处在深链上，却还套了带子作用域的容器型 effect：\n  "
        + "\n  ".join(
            f"{name}（相对 tick 深度 {base}）里调用了 {eff}，"
            f"容器会让链达到 {base + 1} 层"
            for name, base, eff in offending
        )
        + f"\n引擎上限是 {AUTONOMY_FRAMES} 层，超限分支会被静默放弃。"
        "\n请把该调用提到更浅的一层（例如直接挂在 tick 下），"
        "或改用不带 effect 容器的等价写法。"
    )


def test_ship_building_happens_at_tick_level() -> None:
    """建舰必须挂在 tick 层，不得退回 phase_military。

    这条守卫锁住上一条测试的**修法**，而不是只锁症状：即使将来有人"
    聪明地"把建舰塞回 phase 里、又恰好没触发上一条的检测，这里也会失败。

    另外确认它用的是 ``create_military_fleet`` 而不是 ``every_owned_fleet``
    + ``create_ship`` —— 后者会把军舰塞进民用舰队，实机报
    「无法将民用舰船与其他舰船混合编制」。
    """
    text = _read(AUTONOMY)

    # tick 里必须有直接调用
    tick = re.search(r"overmind_autonomy_tick\s*=\s*\{", text)
    assert tick, "找不到 overmind_autonomy_tick"
    d, i = 1, tick.end()
    while i < len(text) and d > 0:
        if text[i] == "{":
            d += 1
        elif text[i] == "}":
            d -= 1
        i += 1
    tick_body = text[tick.end() : i - 1]
    assert re.search(
        r"^\s*overmind_autonomy_build_ship\s*=\s*yes", tick_body, re.M
    ), "tick 里没有直接调用 overmind_autonomy_build_ship"

    # phase_military 里不许再有建舰
    pm = re.search(r"overmind_autonomy_phase_military\s*=\s*\{", text)
    assert pm, "找不到 overmind_autonomy_phase_military"
    d, i = 1, pm.end()
    while i < len(text) and d > 0:
        if text[i] == "{":
            d += 1
        elif text[i] == "}":
            d -= 1
        i += 1
    pm_body = text[pm.end() : i - 1]
    assert not re.search(r"\bcreate_(military_)?fleet\s*=", pm_body), (
        "phase_military 里又出现建舰了。那会让链达到 6 层（engine cap 5），"
        "见 test_effect_containers_stay_within_the_nesting_budget。"
    )

    # 不许用 every_owned_fleet + create_ship 的老写法
    assert not re.search(
        r"every_owned_fleet\s*=\s*\{[^}]*create_ship\s*=", text, re.S
    ), (
        "又回到 every_owned_fleet + create_ship 了。"
        "every_owned_fleet 会遍历所有舰队，is_mobile 对民用船同样成立，"
        "把军舰塞进民用编制会被引擎拒绝（实机 error.log 有原文）。"
    )


def test_tradition_gate_writes_a_visible_marker_when_it_cannot_act() -> None:
    """传统推进的「按兵不动」必须有日志，否则整条传统相位是黑箱。

    背景（2026-09-15 实机 game.log 取证）：

        [2200.1.1] engage      [2201.1.1] annual    [2202.1.1] annual
        [2202.5.1] amenities   [2203.1.1] annual

    三年里 **一条传统日志都没有**，而 ``set_country_flag =
    om_did_tradition`` 恰恰落在相位体内部 —— 也就是说相位**根本没被轮到**
    （初版日程前五格是 RELIEF/HOUSING/JOBS/AMENITIES/SCIENCE，ASCENSION 在
    第 7 格，一年只走到第 5 格），而不是「轮到了但因为付不起而空转」。

    这个区分很关键。Clausewitz 的 ``if = { limit = ... }`` 在条件不成立时
    **静默跳过**，所以「没日志」同时对应两种完全不同的原因：

        a) 相位没排进日程          —— 法案问题
        b) 相位跑了但钱不够        —— 经济问题

    在任何一种情况下，玩家看到的都是一片安静。这个测试把 (b) 从 (a) 里
    拆出来：真实动作（add_tradition / add_ascension_perk）的每个分支都要配
    一条对应的 ``log``，这样「跑了但没干活」会在日志里留下证据。

    (a) 由 ``engine/strategy_lex.py`` 的日程编译负责，不在本测试范围内。
    """
    prog = _read(MOD_DIR / "common/scripted_effects/overmind_progression.txt")

    real_actions = re.findall(
        r"^\s*(add_tradition|add_ascension_perk)\s*=", prog, re.M
    )
    logs = re.findall(r"^\s*log\s*=", prog, re.M)

    assert real_actions, "progression 文件里一个真实动作都没有？"
    assert logs, (
        "overmind_progression.txt 里没有任何 log。"
        "传统/飞升动作的每个分支都要留一行日志 —— 否则相位空转时"
        "玩家和验收方都无法区分「没轮到」和「钱不够」。"
        "见本测试 docstring 里的实机时间线。"
    )
    # 每个传统树 / 每个节点分支都该有日志，数量级上不该只有零星几条。
    assert len(logs) >= len(real_actions) // 12, (
        f"日志密度太低：{len(logs)} 条日志对 {len(real_actions)} 个动作。"
        "生成器（engine/costs.py）里给每条链补上 log。"
    )


def test_tradition_category_cap_matches_the_game_define() -> None:
    """传统树上限必须来自游戏定义的 ``@max_tradition_trees``，不能是手抄的数字。

    ``overmind_autonomy_phase_ascension`` 用的是 ``@max_tradition_trees``，
    由**游戏**在 ``common/scripted_variables/07_scripted_variables_machine_age.txt``
    定义： ``@max_tradition_trees = 7``。

    为什么必须走游戏变量：mod 若写死 7，而玩家装了抬高上限的 mod（该变量注释
    原文就是 "In case modders increase the limit"），本 mod 会在别人能继续
    采纳第 8、9 棵树时提前停手 —— 表现为「传统相位莫名其妙不再有动作」。

    反面教训（2026-09-15 实机）：本 mod 一度自己定义 ``@om_max_tradition_trees``，
    与游戏变量并存在同一个注册表里，引擎报
    ``Variable name max_tradition_trees is already taken``。
    生成器现在不再注册任何变量，只留注释。
    """
    # 执行层引用的是游戏变量，不是本 mod 自造的镜像名。
    assert "@max_tradition_trees" in _read(AUTONOMY), (
        "执行层没有引用 @max_tradition_trees —— 树上限被写死了"
    )

    game = Path("D:/SteamLibrary/steamapps/common/Stellaris")
    if not game.exists():
        pytest.skip("游戏未安装，无法核对 defines")
    found = None
    for path in (game / "common/scripted_variables").glob("*.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = re.search(r"^@max_tradition_trees\s*=\s*(\d+)", text, re.M)
        if hit:
            found = int(hit.group(1))
            break
    assert found is not None, (
        "游戏没有定义 @max_tradition_trees —— 执行层会解析失败"
    )


#: Trigger 名字里最容易写错的那几个 —— 都是**实机踩过**的。
#: 每条的格式是 ``(错误写法, 正确写法, 出错后果)``。
#:
#: 为什么值得单列一张表：Clausewitz 对未知 trigger 的处理是**静默把你的条件
#: 当假**（有些位置甚至直接忽略），既不报错也不写日志。于是一个拼错的
#: 名字表现为「某个相位永远不动」，而你在 error.log 里找不到它 —— 除非
#: 恰好撞上 "Variable ... is not set" 这种半报错。
#:
#: ⚠️ 第 1 条是「反向」陷阱，值得单独记住：``num_ascension_perk``（单数）
#: **不存在**，正确写法是**复数** ``num_ascension_perks``（游戏语料 200+ 处）。
#: 同族的其它 trigger 五花八门（``num_tradition_categories`` 用复数、
#: ``num_ascension_perk_slots`` 用单数却未实现），所以凭「哪个更规范」的
#: 直觉猜必错。判据只有一个：**在游戏脚本语料里数出现次数**。
ENGINE_NAME_TRAPS = (
    (
        "num_ascension_perk",
        "num_ascension_perks",
        "单数形式在游戏语料里 0 处 —— 条件恒假，飞升天赋永远发不出来",
    ),
    (
        "num_ascension_perk_slots",
        "num_ascension_perks",
        "该 trigger 引擎未实现，读数恒为「未设置」并污染 error.log",
    ),
)


def test_no_engine_trigger_name_traps() -> None:
    """禁止使用已知会静默失效的 trigger 名。

    本测试只看**生效代码**（``strip_comments`` 之后的文本），
    所以解释这些坑的注释不会把自己判违规。
    """
    text = _read(AUTONOMY)
    for wrong, right, why in ENGINE_NAME_TRAPS:
        # 用词边界匹配：\b 在 '_' 处不成立，所以 num_ascension_perk 不会被
        # num_ascension_perk_slots 误伤，num_ascension_perks 同理。
        hits = re.findall(rf"\b{re.escape(wrong)}\b", text)
        assert not hits, (
            f"生效代码里出现了 {wrong}（{len(hits)} 处）。"
            f"正确名字是 {right} —— {why}。"
            "详见 overmind_autonomy_phase_ascension 里的注释。"
        )


def test_ascension_gate_uses_a_wide_upper_bound() -> None:
    """飞升槽位的粗筛必须是宽松上界，而不是尝试复刻引擎的精确槽位数。

    槽位上限这件事没有可靠的脚本侧读法（``num_ascension_perk_slots`` 不可用）。
    真正把关的是 ``add_ascension_perk`` 自己：槽满了它会拒绝。

    所以这里的正确策略是**筛宽**——偶尔白试一次无所谓，筛窄/筛错才会
    让整条链停止推进。这个测试锁住这个策略，防止将来有人"顺手"把它改成
    一个精确但不可靠的表达式。

    上界曾经就是 20，理由是"量级为个位数"—— 这个推论默认了原版环境。
    核查用户实际安装的 mod 之后发现：

        vanilla defines        ASCENSION_PERKS_SLOTS = 8
        用户装的 mod 改成了      ASCENSION_PERKS_SLOTS = 32

        （workshop 3359568450 "31 Tradition Slots + 32 AP Slots"，
          它同时把 @max_tradition_trees 从 7 抬到 31）

    也就是说这个数**本来就是 mod 可以自由改的**；再叠加 M4-2 要把飞升池
    从 16 个扩到 49 个 ``ap_*``，20 迟早成为真实瓶颈。
    """
    text = _read(AUTONOMY)
    m = re.search(r"num_ascension_perks\s*<\s*(\d+)", text)
    assert m, (
        "找不到 num_ascension_perks 的槽位粗筛。"
        "若已改为别的写法，请同步更新本测试的意图说明。"
    )
    bound = int(m.group(1))
    assert bound >= 50, (
        f"飞升槽位粗筛的上界只有 {bound}，太窄了。"
        "vanilla 是 8，但 mod 可以把它抬到 32（本机实测 31T+32A 就是 32），"
        "甚至更高。筛窄了会在槽位还有余量时**静默**停手（见 D-7）。"
    )


#: Trigger / scope 名字里最容易写错的那几个 —— 都是**实机踩过**的。
#: 每条的格式是 ``(错误写法, 正确写法, 出错后果)``。
#:
#: 为什么值得单列一张表：Clausewitz 对未知 trigger 的处理是**静默把你的条件
#: 当假**（有些位置甚至直接忽略），既不报错也不写日志。于是一个拼错的
#: 名字表现为「某个相位永远不动」，而你在 error.log 里找不到它 —— 除非
#: 恰好撞上 "Variable ... is not set" 这种半报错。
#:
#: ``num_ascension_perk`` 就是活例子：写成复数 ``num_ascension_perks``
#: 之后，飞升天赋整条链再也发不出来，安静得像从来没写过这段代码。



def _phase_blocks() -> dict[str, str]:
    """``{相位名: 相位体文本}`` —— 已去注释，按括号深度配对。"""
    text = _read(AUTONOMY)
    lines = text.splitlines()
    blocks: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = re.match(r"^overmind_autonomy_phase_(\w+)\s*=\s*\{", line)
        if not m:
            continue
        depth = 1
        j = i + 1
        while j < len(lines) and depth > 0:
            depth += lines[j].count("{") - lines[j].count("}")
            j += 1
        blocks[m.group(1)] = "\n".join(lines[i + 1 : j])
    return blocks


def test_every_phase_logs_its_own_entry() -> None:
    """D-7：每个相位入口必须有**无条件**日志。

    背景（2026-09-15 实机 14 年取证）：自治层从 ``2200.1`` 一路跑到
    ``2214.6``，日志里却只观测到 4 个格位 —— jobs / amenities / science /
    war。另外 4 个（relief / housing / military / ascension）**一次都没有**，
    其中 relief 有 144 行代码、6 条内部日志，依然是零观测。

    根因不是"相位没轮转"，而是 Clausewitz 的 ``if = { limit = ... }``
    在条件不成立时**静默跳过**。于是"没日志"同时对应两种完全不同的原因：

        有入口日志、无动作日志  → 轮到了，但条件不满足（通常是钱不够）
        两者都没有              → 压根没轮到（日程问题）

    我在取证初期正是被这一点误导过 —— 一度以为 ASCENSION 没被排进日程，
    直到把 ``om_month`` 的循环算清楚才排除。修法就是给每个相位体的**第一行**
    放一条不依赖任何条件的 ``log``。本测试锁住这条约定，防止将来重构时
    又把入口日志"顺手"挪进某个 ``if`` 里 —— 那样它就只在成功时打印，
    等于什么都没修。
    """
    blocks = _phase_blocks()
    assert len(blocks) == 8, (
        f"期望 8 个相位，实际找到 {len(blocks)} 个：{sorted(blocks)}"
    )

    missing: list[tuple[str, str]] = []
    for name, body in sorted(blocks.items()):
        first = next((l.strip() for l in body.splitlines() if l.strip()), "")
        if not first.startswith("log"):
            missing.append((name, first[:60]))
    assert not missing, (
        "以下相位的入口没有无条件日志，D-7 会复发："
        + "; ".join(f"{n}（首行是 {f!r}）" for n, f in missing)
        + "。入口日志必须是相位体的第一条生效语句。"
    )


def test_run_slot_has_a_fallback_for_out_of_range_values() -> None:
    """``run_slot`` 的 8 个分支全不匹配时，必须有兜底日志。

    这是 D-7 的第二个沉默点：格位变量若越界（0、9、或压根没设置），
    8 个 ``else_if`` 一个都不进，整个月无声地过去 —— 而且日志侧与
    "相位条件不满足"长得**一模一样**，无从分辨。

    有了 ``else`` 分支，越界会立刻显形，并点名是哪个格位变量。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_run_slot = {")
    assert start != -1, "找不到 overmind_autonomy_run_slot"

    end = text.find("overmind_autonomy_tick = {", start)
    body = text[start : end if end != -1 else len(text)]

    assert re.search(r"\belse\s*=\s*\{", body), (
        "run_slot 缺少 else 兜底分支 —— 格位越界会再次变成静默失败"
    )
    assert "【异常】" in body, (
        "run_slot 的兜底分支里没有可识别的异常日志标记"
    )


# ==========================================================================
# D-17 / D-18 —— 归因守卫与紧急出口（2026-09-15 深夜续三）
#
# 背景：用户执行了控制台 `human_ai on`。接管状态在存档里完全不可观测
# （字节级搜索 human_ai / ai_managed / auto_manage 全 0），而本族所有事件
# 都带 `is_ai = no` 门 —— human_ai 一旦生效，自治心跳静默全停，连开关
# 事件都调不到。于是"没有第二主体"这件事**没有任何证据**，M1-9 的所有
# 「通过」都变成了不可证伪的陈述。
#
# 修法（docs/D-17_归因处置与M1-9重测方案.md）：
#   F1  新增 overmind.304 归因守卫 —— 挂月度脉冲、**不带 is_ai 门**，
#       is_ai = yes 时打 [告警]，把"有没有第二主体"变成日志首行。
#   F2  overmind_autonomy_off 的 potential 去掉 is_ai = no，
#       保证紧急出口在任何状态下都可达。
# ==========================================================================

EDICTS = MOD_DIR / "common/edicts/overmind_autonomy_edicts.txt"
EVENTS = MOD_DIR / "events/overmind_autonomy_events.txt"
ON_ACTIONS = MOD_DIR / "common/on_actions/overmind_on_actions.txt"

#: 事件块的括号配对（events 文件结构简单，无引号内花括号的已知情况）。
def _block_at(text: str, brace: int) -> str:
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    raise AssertionError("括号不配对")


def _event_block(text: str, event_id: str) -> str:
    idx = text.find(f"id = {event_id}")
    assert idx != -1, f"找不到事件 {event_id}"
    start = text.rfind("country_event = {", 0, idx)
    assert start != -1, f"事件 {event_id} 前找不到 country_event = {{"
    return _block_at(text, text.index("{", start))


def _edict_block(text: str, name: str) -> str:
    idx = text.find(f"{name} = {{")
    assert idx != -1, f"找不到法令 {name}"
    return _block_at(text, text.index("{", idx))


def _potential_of(block: str) -> str:
    m = re.search(r"potential\s*=\s*\{", block)
    assert m, "法令缺少 potential 块"
    return _block_at(block, block.index("{", m.start()))


def test_attribution_guard_is_registered_on_the_monthly_pulse() -> None:
    """F1：overmind.304 必须挂上月度脉冲，且排在心跳 300 之前。

    守卫如果排在 300 后面，"本月动作已执行完才告警"会让日志顺序误导人；
    排最前，告警永远先于本月任何动作出现。
    """
    text = _read(ON_ACTIONS)
    m = re.search(r"on_monthly_pulse_country\s*=\s*\{", text)
    assert m, "找不到 on_monthly_pulse_country"
    body = _block_at(text, text.index("{", m.start()))
    assert "overmind.304" in body, "归因守卫 304 未注册到月度脉冲"
    assert body.index("overmind.304") < body.index("overmind.300"), (
        "304 必须排在 300 之前 —— 告警应先于本月动作出现"
    )


def test_attribution_guard_has_no_is_ai_gate() -> None:
    """F1 核心：304 的 trigger 里**绝不能**出现 is_ai。

    本族其它事件全部带 `is_ai = no` 门。守卫若也带门，human_ai 生效的
    那一刻它就和心跳一起哑掉 —— 恰恰在最需要它说话的时候失声。
    """
    block = _event_block(_read(EVENTS), "overmind.304")
    m = re.search(r"trigger\s*=\s*\{", block)
    assert m, "304 缺少 trigger 块"
    trig = _block_at(block, block.index("{", m.start()))
    assert "is_ai" not in trig, (
        "归因守卫 304 的 trigger 带了 is_ai 门 —— human_ai 生效时它会和心跳一起静默"
    )
    assert "overmind_autonomy_engaged" in trig, (
        "304 应以自治层在线为前提，否则会为 136 个 AI 国家刷屏"
    )


def test_attribution_guard_warns_when_country_is_ai_judged() -> None:
    """F1：is_ai = yes 时必须打出带 [告警] 标记的日志。"""
    block = _event_block(_read(EVENTS), "overmind.304")
    assert re.search(r"is_ai\s*=\s*yes", block), "304 缺少 is_ai = yes 分支"
    assert "【告警】" in block, "304 的告警日志缺少 [告警] 标记（日志窗口靠它高亮）"
    assert "不可归因" in block, "告警日志必须写明后果：本月指标不可归因"


def test_off_edict_stays_reachable_without_an_is_ai_gate() -> None:
    """D-18/F2：`off` 法令的 potential 不得带 is_ai 门。

    human_ai 生效会把玩家国家判定为 AI。若 potential 带 is_ai = no，
    两条法令同时从面板消失，而自治层由 flag 驱动照跑不停 —— 模组关不掉。
    `on` 法令**必须**保留 is_ai = no，防止原版 AI 自己打开我们的引擎。
    """
    text = _read(EDICTS)
    off_pot = _potential_of(_edict_block(text, "overmind_autonomy_off"))
    assert "is_ai" not in off_pot, (
        "off 法令的 potential 带了 is_ai 门 —— human_ai 生效时紧急出口会消失"
    )
    assert "has_country_flag = overmind_autonomy" in off_pot, (
        "off 法令必须仍以自治层在线为前提，否则可关一个未启动的层"
    )

    on_pot = _potential_of(_edict_block(text, "overmind_autonomy_on"))
    assert "is_ai = no" in on_pot, "on 法令必须保留 is_ai = no（防原版 AI 自开）"


# ==========================================================================
# R-1 / R-2 —— 三席评审整改（2026-09-15 深夜续五）
#
# 评审结论（docs/验收评审记录_2026-09-15.md）：
#   红队#1  304 只在 is_ai=yes 时告警 → "没看见告警"被误读为干净；
#           开局前 human_ai 已开则全程静默。
#   红队#4  om_did_ship 是月度标记且年度采样遇不到 slot6 ⇒ fleet_size
#           成为唯一且无交叉验证的代理。
# ==========================================================================

def test_attribution_heartbeat_is_proven_by_presence() -> None:
    """R-1：304 必须有 else 分支 —— 归因干净要由 [归因OK] 的**存在**证明。

    缺失性判定（"没看到告警就算干净"）有两个盲区：日志被轮转/窗口没开时
    告警丢了也无从知道；开局前 human_ai 已开则整局静默。改成存在性判定后，
    每条 [归因OK] 都是当月未接管的正面证据；自治层在线却整月零心跳，
    本身就是异常信号。
    """
    block = _event_block(_read(EVENTS), "overmind.304")
    m = re.search(r"\belse\s*=\s*\{", block)
    assert m, "304 缺少 else 分支 —— 归因干净无法由存在性证明"
    else_body = _block_at(block, block.index("{", m.start()))
    assert "【归因OK】" in else_body, "else 分支缺少 [归因OK] 心跳日志"


def test_ships_built_counter_is_independent_evidence() -> None:
    """R-2：建舰必须写累计计数器，engage 必须把它初始化为 0。

    om_did_ship 每月初清零，而年度自动存档只会落在 slot 4/8（D-15 混叠），
    军事相位建舰的证据几乎必然采样不到。om_ships_built 是跨存档单调的
    第二证据源，探针据此独立确证"真的建过舰"，不再只信 fleet_size 标量。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_build_ship = {")
    assert start != -1, "找不到 overmind_autonomy_build_ship"
    end = text.find("overmind_autonomy_phase_military = {", start)
    ship_body = text[start : end if end != -1 else len(text)]
    assert re.search(
        r"change_variable\s*=\s*\{\s*which\s*=\s*om_ships_built", ship_body
    ), "建舰成功后没有累加 om_ships_built —— fleet_size 冻结将无法交叉验证"

    engage_start = text.find("overmind_autonomy_engage = {")
    assert engage_start != -1, "找不到 overmind_autonomy_engage"
    engage_body = _block_at(text, text.index("{", engage_start))
    assert re.search(
        r"set_variable\s*=\s*\{\s*which\s*=\s*om_ships_built\s+value\s*=\s*0",
        engage_body,
    ), "engage 没有把 om_ships_built 初始化为 0 —— 未建舰时探针将读不到该变量"


# ==========================================================================
# D-19 —— log 串里的 ASCII 方括号会被 Clausewitz 当脚本求值块吃掉
# （2026-09-15 深夜续七，实机抓获）
#
# 实机证据：game.log 里 `OVERMIND: [归因OK] 本月未被判定为 AI 控制`
# 打成了 `OVERMIND:  本月…`（双空格），`[相位 1/8] …` 整条变成空的
# `OVERMIND: `。根因：log 字符串中方括号按本地化脚本求值，未知表达式
# 求值为空。标记必须用全角【 】；[This.GetName] 这类求值是故意的，
# 白名单放行。
# ==========================================================================

_ALLOWED_EVAL = re.compile(r"\[(?:This|Root|From|Owner|Prev|FromFrom|RootRoot)\.[A-Za-z0-9_.]*\]")


def _overmind_log_strings(path, text: str) -> list[str]:
    out = []
    for m in re.finditer(r'log = "([^"]*)"', text):
        s = m.group(1)
        if s.startswith("OVERMIND"):
            out.append(s)
    assert out, f"{path} 里找不到任何 OVERMIND 日志串"
    return out


def test_no_ascii_brackets_inside_overmind_log_strings() -> None:
    """D-19：OVERMIND 日志串里禁止裸 ASCII 方括号标记。

    白名单只放行已知求值上下文（[This.GetName] 等）。若再有人往
    日志里写 [告警] 这类标记，实机上它会被求值为空、静默消失，
    日志窗口的高亮和 F3' 的 grep 取证全部失效 —— 所以必须在测试期拦下。
    """
    for path in (AUTONOMY, EVENTS):
        text = _read(path)
        for s in _overmind_log_strings(path, text):
            stripped = _ALLOWED_EVAL.sub("", s)
            assert "[" not in stripped and "]" not in stripped, (
                f"{path}: 日志串含会被求值吃掉的 ASCII 方括号：{s!r}"
            )




def test_ship_building_respects_naval_capacity() -> None:
    """D-20：建舰必须有海军容量上限 —— 容量未满才造。

    实机观察（2026-09-15）：建舰条件只有合金门槛 + 每月一次，没有任何
    规模约束 → 只要合金够就月月造、无限扩军。超容舰队维护惩罚重，
    缺省姿态不应当超容扩军。原版 AI 用 used_naval_capacity_percent
    做同类判断，这里对齐原版写法。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_build_ship = {")
    assert start != -1, "找不到 overmind_autonomy_build_ship"
    end = text.find("overmind_autonomy_phase_military = {", start)
    ship_body = text[start : end if end != -1 else len(text)]
    limit_m = re.search(r"limit\s*=\s*\{", ship_body)
    assert limit_m, "build_ship 缺少 limit 块"
    limit = _block_at(ship_body, limit_m.start() + len("limit = ") if False else ship_body.index("{", limit_m.start()))
    assert re.search(r"used_naval_capacity_percent\s*<", limit), (
        "build_ship 的 limit 缺少海军容量上限 —— 无限扩军会复发"
    )
    # 发展优先（用户指令 2026-09-15）：前 10 年零扩军，威胁豁免。
    assert re.search(r"years_passed\s*>=\s*10", limit), (
        "build_ship 缺少发展优先门（years_passed >= 10）"
    )
    # S-1：能源收入门与合金安全垫统一走 overmind_can_afford（不再内联）。
    assert limit.count("overmind_can_afford") >= 2, (
        "build_ship 未使用统一花销门 overmind_can_afford（S-1 回退）"
    )
    assert "PAD = 300" in limit, (
        "build_ship 的合金安全垫低于 300 —— 造舰不得打穿库存"
    )


# ==========================================================================
# S-1 / S-2 —— 帝国治理策略地基（2026-09-15 深夜续十，总裁授权落地）
#
# S-1  统一花销门 overmind_can_afford（库存垫 + 能源收入正，永不豁免）
# S-2  om_era 年代查表变量 + 年代化舰队容量目标
# ==========================================================================

TRIGGERS = MOD_DIR / "common/scripted_triggers/overmind_autonomy_triggers.txt"


def test_unified_afford_gate_exists_and_is_used() -> None:
    """S-1：overmind_can_afford 触发器存在且建舰走它。

    触发器内必须同时有库存垫（$RESOURCE$/$PAD$ 参数化）与能源收入门；
    能源门放触发器里而不是调用点，保证"任何分支都不豁免"（规格书 P6）。
    """
    text = _read(TRIGGERS)
    m = re.search(r"overmind_can_afford\s*=\s*\{", text)
    assert m, "触发器文件缺少 overmind_can_afford"
    body = _block_at(text, text.index("{", m.start()))
    assert "$RESOURCE$" in body and "$PAD$" in body, (
        "overmind_can_afford 未参数化 —— 无法被不同资源复用"
    )
    assert re.search(
        r"resource_income_compare\s*=\s*\{\s*resource\s*=\s*energy\s+value\s*>\s*0",
        body,
    ), "overmind_can_afford 缺少能源收入门 —— 赤字扩军将失控"


def test_era_variable_ladder_is_recalculated_yearly() -> None:
    """S-2：om_era 必须由年度 tick 重算，engage 归 1。

    年代阈值对应执行手册时代总表：150/75/30/10 → era5..era2，其余 era1。
    若年度重算丢失，om_era 会冻结在 engage 时的 1，舰队永远不扩军。
    """
    text = _read(AUTONOMY)
    # D-23：阶梯已抽成 overmind_autonomy_era_sync（年度调用 + 月度自愈）
    sync_start = text.find("overmind_autonomy_era_sync = {")
    assert sync_start != -1, "找不到 overmind_autonomy_era_sync（S-2 阶梯容器）"
    sync = _block_at(text, text.index("{", sync_start))
    for years, era in (("150", "5"), ("75", "4"), ("30", "3"), ("10", "2")):
        assert re.search(
            rf"years_passed\s*>=\s*{years}[\s\S]*?om_era value = {era}",
            sync,
        ), f"年代阶梯缺少 years_passed >= {years} → om_era = {era}"
    assert re.search(r"else\s*=\s*\{\s*set_variable\s*=\s*\{\s*which\s*=\s*om_era\s+value\s*=\s*1", sync), (
        "年代阶梯缺少 else 兜底（era1）"
    )

    # 年度 tick 必须无条件重算
    annual_start = text.find("overmind_autonomy_annual = {")
    assert annual_start != -1, "找不到 overmind_autonomy_annual"
    annual = _block_at(text, text.index("{", annual_start))
    assert "overmind_autonomy_era_sync = yes" in annual, (
        "年度 tick 未调用 era_sync —— om_era 年度重算回退"
    )

    # D-23：月度 tick 必须对缺失变量自愈（旧存档兼容）
    tick_start = text.find("overmind_autonomy_tick = {")
    assert tick_start != -1, "找不到 overmind_autonomy_tick"
    tick = _block_at(text, text.index("{", tick_start))
    assert re.search(
        r"NOT\s*=\s*\{\s*check_variable\s*=\s*\{\s*which\s*=\s*om_era[\s\S]{0,80}?"
        r"overmind_autonomy_era_sync = yes",
        tick,
    ), "月度 tick 缺少 om_era 缺失自愈 —— 旧存档将永不扩军（D-23 复发）"

    engage_start = text.find("overmind_autonomy_engage = {")
    engage_body = _block_at(text, text.index("{", engage_start))
    assert re.search(
        r"set_variable\s*=\s*\{\s*which\s*=\s*om_era\s+value\s*=\s*1",
        engage_body,
    ), "engage 未初始化 om_era —— 接管当年查不到年代"


def test_fleet_capacity_targets_follow_the_era() -> None:
    """S-2：建舰容量门必须按年代查表（执行手册时代总表）。

    era2 30-60%、era3 60-90%、era4+ 满容量；era1 无分支（发展期禁扩军），
    om_era 缺失时所有分支不成立 → 安全兜底不建舰。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_build_ship = {")
    end = text.find("overmind_autonomy_phase_military = {", start)
    ship_body = text[start : end if end != -1 else len(text)]
    for era, cap in (("= 2", "0.6"), ("= 3", "0.9"), (">= 4", "1.0")):
        assert re.search(
            rf"om_era value {era}[\s\S]*?used_naval_capacity_percent\s*<\s*{cap}",
            ship_body,
        ), f"建舰容量门缺少 era{era} → {cap} 分支（S-2 回退）"


def test_starbase_rebuild_gate_includes_the_energy_gate() -> None:
    """S-1 总裁席整改：哨站重建不得绕过能源收入门。

    总裁席复审（2026-09-15）指出 overmind_afford_starbase_starbase_outpost
    只查合金 ≥100、无能源门，违反规格书 P6「能源门任何姿态不豁免」。
    整改为组合 overmind_can_afford（合金垫 100 + 能源收入为正）。
    """
    path = MOD_DIR / "common/scripted_triggers/overmind_afford.txt"
    text = _read(path)
    m = re.search(r"overmind_afford_starbase_starbase_outpost\s*=\s*\{", text)
    assert m, "找不到 overmind_afford_starbase_starbase_outpost"
    body = _block_at(text, text.index("{", m.start()))
    assert "overmind_can_afford" in body, (
        "哨站重建门未走统一花销门 —— 能源门被绕过（总裁整改项复发）"
    )


def test_expansion_channel_wired_and_gated() -> None:
    """S-4/S-7 扩张通道（多槽并行版）必须挂 tick、门控齐全、配额可复位。

    用户 2026-09-16 指令：多线并进提效率（并行带宽），但注意资源消耗 →
    每个动作由独立助手效果承担、每槽独立过 overmind_can_afford，
    配额由计数器限定（采矿 ×2 / 研究 ×2 / 前哨 ×2 / 殖民 ×1）并在月初清零。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_expand = {")
    assert start != -1, "找不到 overmind_autonomy_expand —— 扩张通道丢失"
    end = text.find("overmind_autonomy_annual = {", start)
    body = text[start : end if end != -1 else len(text)]

    # 并行带宽：助手调用次数（多线并进）
    for helper, times in (
        ("overmind_expand_try_mining", 2),
        ("overmind_expand_try_research", 2),
        ("overmind_expand_try_outpost", 2),
        ("overmind_expand_try_colony", 1),
    ):
        assert body.count(f"{helper} = yes") == times, (
            f"扩张带宽不符：{helper} 期望 {times} 槽，实际 "
            f"{body.count(f'{helper} = yes')} —— 并行效率回退"
        )

    # 每个助手：效果 + 门控 + 计数器上限
    for helper, klass, afford, counter in (
        ("overmind_expand_try_mining", "shipclass_mining_station",
         "RESOURCE = minerals PAD = 200", "om_min_built"),
        ("overmind_expand_try_research", "shipclass_research_station",
         "RESOURCE = minerals PAD = 200", "om_res_built"),
        ("overmind_expand_try_outpost", "starbase_outpost",
         "RESOURCE = alloys PAD = 100", "om_out_built"),
    ):
        s = text.find(f"{helper} = {{")
        assert s != -1, f"缺少助手效果 {helper}"
        e = text.find("\n}\n", s)
        hb = text[s : e if e != -1 else len(text)]
        assert klass in hb and afford in hb, (
            f"{helper} 缺少效果或资源门（{klass} / {afford}）"
        )
        assert re.search(rf"check_variable\s*=\s*\{{\s*which\s*=\s*{counter}\s+value\s*<", hb), (
            f"{helper} 缺少月度配额计数器 {counter}"
        )
        assert re.search(rf"change_variable\s*=\s*\{{\s*which\s*=\s*{counter}", hb), (
            f"{helper} 未累加计数器 {counter}"
        )

    # 殖民块：原版 create_colony + is_colonizable
    cs = text.find("overmind_expand_try_colony = {")
    assert cs != -1, "缺少殖民助手 overmind_expand_try_colony（S-7）"
    colony = text[cs:]
    assert "create_colony" in colony and "is_colonizable = yes" in colony, (
        "殖民块缺少 create_colony 或 is_colonizable（原版验证过的写法）"
    )
    assert re.search(r"influence value >= 100", colony), (
        "殖民缺影响力 >=100 门 —— 殖民必须消耗软货币"
    )

    assert re.search(r"influence value >= 75", text), (
        "前哨拓土缺影响力 >=75 门 —— 拓土必须消耗软货币"
    )

    # 计数器月初清零（否则每月只能跑一次，并行带宽形同虚设）
    tick_start = text.find("overmind_autonomy_tick = {")
    tick = _block_at(text, text.index("{", tick_start))
    for counter in ("om_min_built", "om_res_built", "om_out_built", "om_col_built"):
        assert re.search(rf"set_variable\s*=\s*\{{\s*which\s*=\s*{counter}\s+value\s*=\s*0", tick), (
            f"月度配额 {counter} 未在月初清零 —— 并行带宽失效"
        )

    # 必须真的被 tick 调用，否则是死代码
    assert "overmind_autonomy_expand = yes" in text, (
        "overmind_autonomy_expand 未挂入月度 tick —— 死代码"
    )


def test_whitelist_invest_channel_health_gated() -> None:
    """S-4 白名单主动基建：只在健康（无赤字 + 有冗余）时出手，优先级正确。

    「没发育」的机制根因：此前所有建设都挂在"出问题才补"上，健康帝国什么都不做。
    本通道是解药 —— 但必须钉死三条：① 无能源/食物赤字 + 矿物垫 400；
    ② 优先级 研究区 > 铸造区 > 贸易区；③ 月度计数器限流并月初清零。
    """
    text = _read(AUTONOMY)
    start = text.find("overmind_autonomy_invest = {")
    assert start != -1, "找不到 overmind_autonomy_invest（S-4 健康路径丢失）"
    end = text.find("overmind_autonomy_expand = {", start)
    body = text[start : end if end != -1 else len(text)]

    # ① 健康门槛
    assert re.search(r"NOT\s*=\s*\{\s*overmind_autonomy_deficit_energy", body), (
        "白名单投资缺能源赤字门 —— 赤字时继续投资会加速失血"
    )
    assert re.search(r"NOT\s*=\s*\{\s*overmind_autonomy_deficit_food", body), (
        "白名单投资缺食物赤字门"
    )
    assert "overmind_can_afford = { RESOURCE = minerals PAD = 400 }" in body, (
        "白名单投资缺矿物垫 400（冗余门）"
    )

    # ② 全资源覆盖（2026-09-16 用户要求「所有资源产能都要有，只是分前后期而已」）
    #    每项 = (zone, 必须挂的 district)。district 必须与 zone 的 zone_sets 匹配，
    #    用错（例如把 zone_energy 挂到 district_city）游戏里建不上 —— 这是本项
    #    重构时踩到的真坑：原白名单只碰过 urban 系，所以一直没暴露。
    expected = {
        "zone_food": "district_farming",
        "zone_factory": "district_city",
        "zone_energy": "district_generator",
        "zone_minerals": "district_mining",
        "zone_foundry": "district_city",
        "zone_research": "district_city",
        "zone_unity": "district_city",
        "zone_trade": "district_city",
        "zone_rare_crystals": "district_mining",
        "zone_volatile_motes": "district_mining",
        "zone_exotic_gases": "district_mining",
    }
    for zone, district in expected.items():
        assert zone in body, f"白名单缺 {zone} 分支 —— 全资源覆盖不完整"
        assert re.search(
            rf"add_zone\s*=\s*\{{\s*district\s*=\s*{district}\s+zone\s*=\s*{zone}\s*\}}",
            body,
        ), f"{zone} 挂错了 district（应为 {district}）—— zone_sets 不匹配会建不上"

    # ③ 赤字救火：任一资源净收入为负 → 立即补对应产能（压过常规白名单）
    assert "【救火】" in body, "白名单缺赤字救火分支（消费品赤字就是靠它止血）"
    for res in ("food", "consumer_goods", "energy", "minerals", "alloys"):
        assert f"overmind_autonomy_deficit_{res} = yes" in body, (
            f"救火段缺 {res} 赤字判定"
        )

    # ④ 年代分层（era 条件写在各分支 limit 内，不额外嵌套以保证 5 层预算）
    for cond in ("value <= 2", "value = 3", "value >= 4"):
        assert f"which = om_era {cond}" in body, f"年代白名单缺 era {cond} 分层"

    # ③ 配额计数器
    assert re.search(r"check_variable\s*=\s*\{\s*which\s*=\s*om_wl_built\s+value\s*<", body), (
        "白名单投资缺月度配额计数器 om_wl_built"
    )
    tick_start = text.find("overmind_autonomy_tick = {")
    tick = _block_at(text, text.index("{", tick_start))
    assert re.search(
        r"set_variable\s*=\s*\{\s*which\s*=\s*om_wl_built\s+value\s*=\s*0", tick
    ), "om_wl_built 未在月初清零 —— 投资通道每月只能跑一次"

    # 必须挂在 tick 上
    assert "overmind_autonomy_invest = yes" in text, (
        "overmind_autonomy_invest 未挂入月度 tick —— 死代码"
    )


# ==========================================================================
# S-12 —— 赤字局势管理（2026-09-16）
#
# 依据 docs/玩法模型与策略基线.md §5（机制细节来自本机
# common/situations/02_deficit_situations.txt 确证）：
#   8 个赤字局势刻度一致（initial 15；stage 25/50/75/100，满 100 = 破产）；
#   资源不再赤字时每月 -5 进度 ⇒ 救火分支补平赤字后局势会自己退。
# 本效果把「高危期切削减型应对、脱离后切回默认」做成状态机 ——
# 缺回退分支会让削减永久残留（cut_science 砍 -50% 研究员产出，
# 而科技力占实测分数 99.8%，这是不可接受的长期损失）。
# ==========================================================================

#: 8 个赤字局势 → 高危期应切的应对方式（选择依据见规格书 §5）
DEFICIT_APPROACHES = (
    ("situation_energy_deficit", "deficit_approach_cut_science_investment"),
    ("situation_mineral_deficit", "deficit_approach_cut_investment"),
    ("situation_food_deficit", "deficit_approach_invest_in_farmers"),
    ("situation_consumer_goods_deficit", "deficit_approach_cut_investment"),
    ("situation_alloys_deficit", "deficit_approach_cut_maintenance"),
    ("situation_rare_crystals_deficit", "deficit_approach_recycling"),
    ("situation_volatile_motes_deficit", "deficit_approach_recycling"),
    ("situation_exotic_gases_deficit", "deficit_approach_recycling"),
)


def _situations_source() -> str:
    """返回 S-12 相关源码（全文读取）。

    早期版本用「块定位」（找 `overmind_autonomy_situations = {` 到下一个
    顶层 effect 前）—— 实测在 pytest 环境下块尾会提前命中，导致 food 块
    落在 body 之外、断言假失败。改为全文匹配：8 个局势的 approach 名与
    `situation_progress` 阈值在整个文件里本就唯一，全文匹配同样精确且不脆。
    """
    text = _read(AUTONOMY)
    assert "overmind_autonomy_situations = {" in text, (
        "找不到 overmind_autonomy_situations —— S-12 丢失"
    )
    return text


def test_every_deficit_situation_has_an_escalation_branch() -> None:
    """S-12：8 个赤字局势每个都要有「进度过半 → 切削减型应对」的升级分支。

    漏掉任何一个局势，那一类赤字烧到破产都不会有人管（破产 = 分数重创）。
    """
    body = _situations_source()
    for situation, approach in DEFICIT_APPROACHES:
        assert f"is_situation_type = {situation}" in body, f"{situation} 缺处理块"
        assert approach in body, f"{situation} 缺高危应对方式 {approach}"
    assert body.count("situation_progress > 50") >= 8, (
        "升级阈值数量不足 8 —— 刻度是 25/50/75/100，过半即第 3 阶段"
    )


def test_deficit_situations_fall_back_when_out_of_danger() -> None:
    """S-12：脱离高危必须切回默认 —— 否则削减状态永久残留（白砍研究产出）。"""
    body = _situations_source()
    assert body.count("situation_progress <= 50") >= 8, "缺回退分支的进度判定"
    assert body.count("set_situation_approach = deficit_approach_do_nothing") >= 8, (
        "回退到默认的分支不足 8 个 —— 削减状态会永久残留"
    )


def test_situation_handler_is_wired_into_the_tick() -> None:
    """S-12：必须真的被月度 tick 调用，否则是死代码。"""
    text = _read(AUTONOMY)
    assert "overmind_autonomy_situations = yes" in text, (
        "overmind_autonomy_situations 未挂入月度 tick —— 死代码"
    )
