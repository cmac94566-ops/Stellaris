"""Tests for the cost model and the generated autonomy tables.

The point of these tests is that the Overmind can never *make up* a price.  If
the game rebalances and the resolved cost changes, the integration test fails
and someone has to look — that is the intended behaviour, because a stale price
means the empire pays the wrong amount for every build, silently.
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

from engine.cost_table import (  # noqa: E402
    RESOURCE_NAMES,
    collect_costs,
    iter_txt,
    resolve_variables,
    strip_comments,
)
from engine.costs import (  # noqa: E402
    CHARGED_ITEMS,
    CHARGES_RELPATH,
    PROGRESSION_RELPATH,
    TABLE_RELPATH,
    build_tradition_cost_model,
    collect_ascension_perks,
    collect_tradition_trees,
    generate,
    read_defines,
)
from engine.script_audit import game_dir_from_config  # noqa: E402


def _game_dir() -> Path:
    try:
        return game_dir_from_config()
    except Exception:  # pragma: no cover
        return Path("__no_game__")


GAME_DIR = _game_dir()
MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"

requires_game = pytest.mark.skipif(
    not (GAME_DIR / "common").is_dir(), reason="需要已安装的 Stellaris 数据"
)


# ---------------------------------------------------------------------------
# Scripted-variable evaluation
# ---------------------------------------------------------------------------
def test_resolve_variables_follows_references() -> None:
    resolved = resolve_variables(
        {"base": "100", "double": "@base * 2", "chain": "@double + 1", "bad": "@nope + 1"}
    )
    assert resolved["base"] == 100
    assert resolved["double"] == 200
    assert resolved["chain"] == 201
    assert "bad" not in resolved


def test_resolve_variables_handles_negatives() -> None:
    assert resolve_variables({"cost": "-9000"})["cost"] == -9000


def test_strip_comments_preserves_line_numbers() -> None:
    assert strip_comments("a = 1 # x\nb = 2\n").splitlines() == ["a = 1 ", "b = 2"]


# ---------------------------------------------------------------------------
# Real cost resolution
# ---------------------------------------------------------------------------
@requires_game
def test_district_and_zone_and_building_costs() -> None:
    """Prices must come out of the game's own data files.

    The values are pinned deliberately: the previous hand-written table said
    120/150/300/350, i.e. 2.5–4x too cheap, which meant every ``add_district``
    quietly enriched the empire.
    """
    costs = collect_costs(GAME_DIR)
    assert costs["district_generator"].costs == {"minerals": 300}
    assert costs["district_mining"].costs == {"minerals": 300}
    assert costs["district_farming"].costs == {"minerals": 300}
    assert costs["district_city"].costs == {"minerals": 500}
    assert costs["zone_industrial"].costs == {"minerals": 1000}
    assert costs["building_holo_theatres"].costs == {"minerals": 400}
    assert costs["starbase_outpost"].costs == {"alloys": 100}
    assert costs["assault_army"].costs == {"minerals": 100}


@requires_game
def test_every_charged_item_has_a_real_price() -> None:
    costs = collect_costs(GAME_DIR)
    for kind, ids in CHARGED_ITEMS.items():
        for game_id in ids:
            entry = costs.get(game_id)
            assert entry is not None, f"{kind}: {game_id} 不在游戏造价表中"


@requires_game
def test_costs_only_use_real_resource_names() -> None:
    costs = collect_costs(GAME_DIR)
    for entry in costs.values():
        assert set(entry.costs) <= RESOURCE_NAMES


@requires_game
def test_no_unresolved_scripted_variables() -> None:
    costs = collect_costs(GAME_DIR)
    bad = {k: e.note for k, e in costs.items() if e.note}
    assert bad == {}, f"有变量的表达式没解开：{bad}"


# ---------------------------------------------------------------------------
# Tradition price model
# ---------------------------------------------------------------------------
@requires_game
def test_tradition_model_reads_game_defines() -> None:
    model = build_tradition_cost_model(GAME_DIR)
    defines = read_defines(GAME_DIR)
    assert model.base == int(defines["TRADITION_COST_AMOUNTS"])
    assert model.categories_max == int(defines["TRADITION_CATEGORIES_MAX"])
    assert model.linear == defines["TRADITION_COST_TRADITION"]
    assert model.exponent == defines["TRADITION_COST_TRADITION_EXP"]


@requires_game
def test_tradition_brackets_are_monotonic_and_never_free() -> None:
    model = build_tradition_cost_model(GAME_DIR)
    brackets = model.brackets()
    prices = [price for _limit, price in brackets]
    assert prices == sorted(prices), "分档价格必须单调不减"
    assert prices[0] == model.base
    assert all(p > 0 for p in prices)
    limits = [limit for limit, _price in brackets]
    assert limits == sorted(limits)


# ---------------------------------------------------------------------------
# Progression data
# ---------------------------------------------------------------------------
@requires_game
def test_tradition_trees_have_adopt_and_nodes() -> None:
    trees = collect_tradition_trees(GAME_DIR)
    for tree in ("discovery", "prosperity", "supremacy", "expansion", "mercantile"):
        assert trees[tree]["adopt"], f"{tree} 缺 adopt 节点"
        assert len(trees[tree]["nodes"]) >= 4, f"{tree} 节点太少：{trees[tree]['nodes']}"


@requires_game
def test_ascension_perk_list_is_populated() -> None:
    perks = collect_ascension_perks(GAME_DIR)
    assert len(perks) > 30
    assert "ap_technological_ascendancy" in perks


# ---------------------------------------------------------------------------
# Generated artefacts
# ---------------------------------------------------------------------------
def test_generated_files_exist() -> None:
    for rel in (CHARGES_RELPATH, PROGRESSION_RELPATH, TABLE_RELPATH):
        assert (MOD_DIR / rel).exists(), f"缺少生成文件 {rel}（先跑 engine/costs.py）"


def test_generated_files_are_marked_as_generated() -> None:
    for rel in (CHARGES_RELPATH, PROGRESSION_RELPATH):
        head = (MOD_DIR / rel).read_text(encoding="utf-8")[:400]
        assert "生成" in head and "请勿手改" in head, f"{rel} 缺少'勿手改'抬头"


@requires_game
def test_regenerating_is_deterministic(tmp_path: Path) -> None:
    """Generation must be a pure function of game data, or diffs are noise."""
    generate(GAME_DIR, tmp_path)
    for rel in (CHARGES_RELPATH, PROGRESSION_RELPATH):
        assert (tmp_path / rel).read_text(encoding="utf-8") == (
            MOD_DIR / rel
        ).read_text(encoding="utf-8"), f"{rel} 与重新生成的结果不一致，说明它已过期"


@requires_game
def test_sidecar_table_matches_generated_charges() -> None:
    sidecar = json.loads((MOD_DIR / TABLE_RELPATH).read_text(encoding="utf-8"))
    charges = (MOD_DIR / CHARGES_RELPATH).read_text(encoding="utf-8")
    for game_id, amounts in sidecar["charges"].items():
        if not amounts:
            continue
        for resource, amount in amounts.items():
            assert re.search(
                rf"add_resource\s*=\s*\{{\s*{resource}\s*=\s*-{amount}\s*\}}", charges
            ), f"{game_id} 的扣费语句在生成文件里找不到：{resource} -{amount}"


@requires_game
def test_costs_file_mentions_the_tradition_tree_cap() -> None:
    """文档文件里要写明传统树上限，但**不得**定义任何生效变量。

    历史：这里原先把上限写成 ``@om_max_tradition_trees = 7``。
    那是本 mod 自造的名字，与执行层无引用关系，却被引擎注册，
    实机报 ``Variable name max_tradition_trees is already taken``。
    现在只留注释。
    """
    text = (MOD_DIR / "common/scripted_variables/overmind_costs.txt").read_text("utf-8")
    model = build_tradition_cost_model(GAME_DIR)
    assert str(model.categories_max) in text

    active = [
        ln
        for ln in strip_comments(text).splitlines()
        if ln.strip()
    ]
    assert active == [], (
        "overmind_costs.txt 应当是纯文档，但去注释后还有生效语句："
        f"{active[:5]}"
    )


@requires_game
def test_max_tradition_trees_is_defined_by_the_game() -> None:
    """The executor references ``@max_tradition_trees``; the game must define it.

    Clause 4 of the plan's hard constraints: never depend on a name the game
    does not actually define.  Scripted variables are invisible to the auditor,
    so they get their own test.

    同时检查**本 mod 不要重复定义它**。scripted_variables 是被引擎全量注册的，
    重名会报 ``Variable name ... is already taken`` —— 实机在
    ``07_scripted_variables_machine_age.txt line: 20`` 上就撞过这一次。
    """
    found_value: int | None = None
    for path in iter_txt(GAME_DIR / "common/scripted_variables"):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        m = re.search(r"^@max_tradition_trees\s*=\s*(\d+)", text, re.M)
        if m:
            found_value = int(m.group(1))
            break
    assert found_value is not None, (
        "游戏没有定义 @max_tradition_trees —— 执行层会解析失败"
    )

    # 本 mod 不得再造一个同名变量。
    for path in iter_txt(MOD_DIR):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        assert not re.search(r"^@max_tradition_trees\s*=", text, re.M), (
            f"{path.name} 重复定义了 @max_tradition_trees，"
            "会和游戏抢注册表。执行层应当直接引用游戏变量。"
        )


@requires_game
def test_generated_cost_vars_does_not_redeclare_game_names() -> None:
    """生成文档里出现的 ``@名字`` 必须都是游戏自己的名字，不能是本 mod 自造的。

    判据很简单也很硬：**凡是本文件写过 ``@名字``，游戏就必须在
    ``common/scripted_variables`` 里定义过 ``@名字``**。

    这条测试是 2026-09-15 ``Variable name max_tradition_trees is already taken``
    那次实机报错的直接守卫：当时生成器造了 ``@om_neg_*`` /
    ``@om_max_tradition_trees`` 一堆私有名字，只为"好看"，代价是注册表污染。
    """
    text = (MOD_DIR / "common/scripted_variables/overmind_costs.txt").read_text("utf-8")

    game_names: set[str] = set()
    for path in iter_txt(GAME_DIR / "common/scripted_variables"):
        body = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        game_names.update(re.findall(r"^@(\w+)\s*=", body, re.M))

    declared = set(re.findall(r"^@(\w+)\s*=", strip_comments(text), re.M))
    invented = sorted(declared - game_names)
    assert not invented, (
        f"overmind_costs.txt 定义了游戏没有的名字：{invented}。"
        "文档文件不该注册任何变量；要写就用注释，要查就用 overmind_cost_table.json。"
    )
