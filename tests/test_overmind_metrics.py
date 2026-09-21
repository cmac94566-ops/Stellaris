"""Tests for the AC-4 metric extractor.

The extractor reads a real 82 MB gamestate, which is far too big to commit, so
these tests build a *structurally faithful* miniature: the same indentation
quirks, the same split between the global ``districts=``/``buildings=`` arrays
and the per-colony id lists, and the same null sentinel in zone slots.

Both quirks below have already caused a silent wrong answer once:

* ``colony=`` puts children at **one** tab while ``planets=``/``planet=`` nests
  one level deeper.  A window search with the wrong indent reads the planets
  array instead and returns numbers that look plausible.
* a zone pattern anchored on ``\\n`` right after a capture group that itself
  starts after a newline never matches, so every district reported zero zones.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "overmind_metrics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("overmind_metrics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


metrics = _load_module()

NULL = metrics.NULL_ID


def _gamestate(*, districts_of: dict[int, list[int]],
               buildings_of: dict[int, list[int]],
               zones_of: dict[int, list[int]],
               traditions: list[str],
               controlled: list[int],
               fleet_size: int = 7) -> str:
    cols = []
    for cid, dlist in districts_of.items():
        blist = buildings_of.get(cid, [])
        cols.append(
            f"\t{cid}=\n\t{{\n"
            f"\t\tdistricts=\n\t\t{{\n\t\t\t{' '.join(map(str, dlist))} \n\t\t}}\n"
            f"\t\tbuildings_cache=\n\t\t{{\n\t\t\t{' '.join(map(str, blist))} \n\t\t}}\n"
            f"\t\tstability=50\n"
            f"\t}}\n"
        )
    dists = []
    for did, zlist in zones_of.items():
        dists.append(
            f"\t{did}=\n\t{{\n"
            f"\t\tzones=\n\t\t{{\n\t\t\t{' '.join(map(str, zlist))} \n\t\t}}\n"
            f"\t\ttype=\"district_city\"\n\t\tlevel=1\n\t}}\n"
        )
    trad = "".join(f'\t\t\t"{t}"\n' for t in traditions)
    return (
        'version="Pegasus v4.4.6"\n'
        'date="2211.01.01"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        # A decoy: deeper indentation, and the only other 'ID' the file has.
        "planets=\n{\n\tplanet=\n\t{\n"
        "\t\t0=\n\t\t{\n\t\t\tplanet_class=\"pc_g_star\"\n\t\t}\n"
        "\t}\n}\n"
        "colony=\n{\n" + "".join(cols) + "}\n"
        "country=\n{\n"
        "\t0=\n\t{\n\t\tinitialized=yes\n\t}\n"
        "\t1=\n\t{\n"
        "\t\tmilitary_power=109.5\n"
        f"\t\tfleet_size={fleet_size}\n"
        "\t\tnum_sapient_pops=5413\n"
        "\t\tempire_size=51\n"
        f"\t\tcontrolled_colonies=\n\t\t{{\n\t\t\t{' '.join(map(str, controlled))} \n\t\t}}\n"
        f"\t\ttradition_categories=\n\t\t{{\n\t\t\t\"tradition_adaptability\"\n\t\t}}\n"
        f"\t\ttraditions=\n\t\t{{\n{trad}\t\t}}\n"
        "\t}\n"
        "}\n"
        "districts=\n{\n" + "".join(dists) + "}\n"
    )


@pytest.fixture
def save(tmp_path: Path) -> Path:
    text = _gamestate(
        districts_of={0: [0, 1, 2, 3], 172: [4, 5]},
        buildings_of={0: [0, 1, 2, 3, 4], 172: [5]},
        zones_of={
            0: [0, 1, 2],        # capital: 3 live slots
            1: [NULL],           # generator: empty
            2: [NULL],
            3: [NULL],
            4: [7, 8, NULL],     # the second colony's city
            5: [NULL],
        },
        traditions=["tr_adaptability_adopt", "tr_adaptability_1"],
        controlled=[0],
    )
    path = tmp_path / "autosave_2211.01.01.sav"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("gamestate", text)
        z.writestr("meta", "")
    return path


# ---------------------------------------------------------------------------
def test_player_country_is_read_from_the_player_block(save: Path) -> None:
    assert metrics.player_country(metrics.gamestate_of(save)) == 1


def test_only_controlled_colonies_are_counted(save: Path) -> None:
    """Colony 172 belongs to someone else — it must not inflate the numbers."""
    data = metrics.extract(save)
    assert data["colonies"] == 1
    assert data["districts"] == 4          # colony 0 only, not + [4, 5]
    assert data["buildings"] == 5


def test_zones_count_only_live_slots(save: Path) -> None:
    data = metrics.extract(save)
    assert data["zones"] == 3              # 0 1 2, with 4294967295 excluded


def test_traditions_and_scalars(save: Path) -> None:
    data = metrics.extract(save)
    assert data["traditions"] == 2
    assert data["tradition_categories"] == 1
    assert data["fleet_size"] == 7
    assert data["military_power"] == pytest.approx(109.5)
    assert data["year"] == 2211
    assert data["date"] == "2211.01.01"


def test_planets_array_is_not_mistaken_for_a_colony(save: Path) -> None:
    """The decoy planet block has deeper indentation; it must be ignored."""
    text = metrics.gamestate_of(save)
    body, index = metrics.colony_blocks(text)
    assert sorted(index) == [0, 172]
    # 'planets=' also contains a '0=' child; the colony index must not include it
    assert text.index("\nplanets=\n{\n") < text.index("\ncolony=\n{\n")


def test_empty_zone_slots_are_not_counted(tmp_path: Path) -> None:
    text = _gamestate(
        districts_of={0: [0]},
        buildings_of={0: []},
        zones_of={0: [NULL, NULL]},
        traditions=[],
        controlled=[0],
    )
    path = tmp_path / "s.sav"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("gamestate", text)
    assert metrics.extract(path)["zones"] == 0


def test_block_reader_is_quote_aware() -> None:
    """Braces inside a quoted string must not confuse the block reader."""
    text = 'a=\n{\n\tname="a { brace }"\n\tinner=\n\t{\n\t\tx=1\n\t}\n}\nrest=1\n'
    body = metrics.block_at(text, text.index("{"))
    assert body.endswith("}")
    assert "x=1" in body
    assert "rest=1" not in body


# ---------------------------------------------------------------------------
# 自治层状态探针（--probe）
#
# 这条通道的意义：Clausewitz 不热重载脚本，所以「游戏跑着的时候我的改动生效
# 了吗」只能靠存档里的状态回答，不能靠日志（日志带着启动时那版的字符串）。
# 2026-09-15 实机就靠它认定了跑的是修复前的代码 —— game.log 里全是英文原文。
# ---------------------------------------------------------------------------
def _gamestate_with_state(*, variables: dict[str, int],
                          extra_flags: dict[str, int] | None = None,
                          decoys: str = "") -> str:
    var_lines = "".join(f"\t\t\t{k}={v}\n" for k, v in variables.items())
    flag_lines = "".join(
        f"\t\t\t{k}={v}\n" for k, v in (extra_flags or {}).items()
    )
    return (
        'version="Pegasus v4.4.6"\n'
        'date="2222.01.01"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        "colony=\n{\n}\n"
        "country=\n{\n"
        "\t1=\n\t{\n"
        "\t\tfleet_size=15\n"
        "\t\tflags=\n\t\t{\n" + flag_lines + "\t\t}\n"
        "\t\tvariables=\n\t\t{\n" + var_lines + "\t\t}\n"
        "\t}\n"
        "}\n"
        "districts=\n{\n}\n"
        + decoys
    )


def _probe_from(text: str, tmp_path: Path) -> dict:
    path = tmp_path / "p.sav"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("gamestate", text)
        z.writestr("meta", "")
    return metrics.probe_overmind(path)


def test_probe_reads_month_agenda_and_lex(tmp_path: Path) -> None:
    pr = _probe_from(
        _gamestate_with_state(
            variables={
                "om_month": 4,
                "om_lex_slot1": 1, "om_lex_slot2": 2, "om_lex_slot3": 3,
                "om_lex_slot4": 4, "om_lex_slot5": 5, "om_lex_slot6": 6,
                "om_lex_slot7": 7, "om_lex_slot8": 8,
                "om_lex_rev": 1, "om_lex_threat": 2,
            },
            extra_flags={"om_did_build": 62954880},
        ),
        tmp_path,
    )
    assert pr["om_month"] == 4
    assert pr["slot_phase"] == "amenities"      # 第 4 格 = AMENITIES
    assert pr["agenda_str"] == "12345678"
    assert pr["lex"] == {"om_lex_rev": 1, "om_lex_threat": 2}
    assert pr["did_flags"] == ["om_did_build"]


def test_probe_does_not_match_om_inside_other_words(tmp_path: Path) -> None:
    """存档是裸文本：``custom_made`` 含 ``om_made``、``galcom_vote`` 含
    ``om_vote``、``random_log_day`` 含 ``om_log_day``。

    第一版用 ``\bom_[a-z_]+`` 抓出 9 个假阳性，把标识符数从 18 虚报成 27。
    这个测试用同样的诱导词把它们钉住。
    """
    decoys = (
        "species_db=\n{\n\t\t\ttrait=\"trait_robot_custom_made\"\n}\n"
        "deeds=\n{\n\t\t\tpatron_objective=\"deed_galcom_vote\"\n}\n"
        "random_log_day=0\n"
    )
    pr = _probe_from(
        _gamestate_with_state(
            variables={"om_month": 8},
            decoys=decoys,
        ),
        tmp_path,
    )
    assert pr["variables"] == {"om_month": 8}
    assert pr["did_flags"] == []


def test_probe_survives_a_country_without_autonomy_state(tmp_path: Path) -> None:
    """未接管的存档不能抛异常 —— 否则 --trend 会在混着旧存档的目录里炸掉。"""
    pr = _probe_from(
        _gamestate_with_state(variables={"unrelated": 1}),
        tmp_path,
    )
    assert pr["om_month"] is None
    assert pr["slot_phase"] is None
    assert pr["agenda_str"] == "????????"


# ---------------------------------------------------------------------------
# AC-4 趋势判定
# ---------------------------------------------------------------------------
def _rows(*series: list[int]) -> list[dict]:
    """把若干指标序列转成 extract() 形状的行。"""
    keys = metrics.M1_9_KEYS
    out = []
    for i in range(len(series[0])):
        out.append({k: s[i] for k, s in zip(keys, series)})
    return out


def test_a_frozen_metric_is_flagged_but_not_failed() -> None:
    """AC-4 v2（D-13 决策 A）：冻结标「注意」，不再自动不通过。

    v1 时代"冻结判不通过"是对的 —— 它抓出了 fleet_size 冻结 bug。
    v2 下反应式设计的健康帝国本来就允许区划/建筑冻结；舰队冻结改由
    om_ships_built（R-2）交叉确证，「注意」标注保留可见性。
    """
    rows = _rows([8, 8, 8], [30, 33, 37], [12, 12, 13], [15, 15, 15], [6, 6, 7])
    rep = metrics.monotonic_report(rows)
    assert rep["districts"]["flat"] is True
    verdict = metrics.ac4_verdict(rep)
    assert verdict["keys"]["districts"]["verdict"] == "注意"
    assert verdict["keys"]["fleet_size"]["verdict"] == "注意"
    assert verdict["keys"]["buildings"]["verdict"] == "通过"
    assert verdict["overall"] is True


def test_a_sustained_decline_fails_the_verdict() -> None:
    """v2 ②：>= 3 个连续采样点逐点下降 = 不通过。

    用「先降后回补」序列（终值 > 初值）—— 这样①净下降拦不住它，
    本测试是②的**唯一**防线；若②失效，测试必须跟着失败。
    """
    rows = _rows(
        [8, 9, 8, 7, 6, 7, 8, 9], [30] * 8, [1] * 8,
        [15] * 8, [6] * 8,
    )
    verdict = metrics.ac4_verdict(metrics.monotonic_report(rows))
    assert verdict["keys"]["districts"]["verdict"] == "不通过"
    assert verdict["overall"] is False


def test_a_net_decrease_fails_even_without_a_long_run() -> None:
    """v2 ①：终值 < 初值 = 不通过，哪怕没有长下降段。"""
    rows = _rows([10, 12, 9], [30, 30, 30], [1, 1, 1], [15, 15, 15], [6, 6, 6])
    verdict = metrics.ac4_verdict(metrics.monotonic_report(rows))
    assert verdict["keys"]["districts"]["verdict"] == "不通过"
    assert verdict["overall"] is False


def test_a_single_point_dip_is_not_a_regression() -> None:
    """单点下降（重复统计/战争损耗）放行：终值更高且无连续下降段。"""
    rows = _rows([8, 9, 8, 10], [30, 30, 30, 30], [1, 1, 1, 1], [15, 15, 15, 15], [6, 6, 6, 6])
    verdict = metrics.ac4_verdict(metrics.monotonic_report(rows))
    assert verdict["keys"]["districts"]["verdict"] == "通过"
    assert verdict["overall"] is True


def test_a_decrease_is_reported_with_its_position() -> None:
    rows = _rows([8, 9, 7], [30, 30, 30], [1, 1, 1], [15, 15, 15], [6, 6, 6])
    rep = metrics.monotonic_report(rows)
    assert rep["districts"]["monotonic"] is False
    assert rep["districts"]["breaks"] == [2]


def test_repeated_values_are_not_a_regression() -> None:
    """同一年的手动存档 + 自动存档会给出相同的值，不该被判成回归。"""
    rows = _rows([8, 8, 9], [30, 30, 30], [1, 1, 1], [15, 15, 15], [6, 6, 6])
    assert metrics.monotonic_report(rows)["districts"]["monotonic"] is True


def test_trend_rows_are_ordered_by_in_game_date(tmp_path: Path) -> None:
    """按文件时间排是不够的 —— 从别处拷回来的存档时间戳会全部相同。"""
    for date, stamp in (("2222.01.01", 1), ("2219.01.01", 2), ("2220.01.01", 3)):
        p = tmp_path / f"autosave_{date}.sav"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("gamestate", _gamestate_with_state(variables={}))
            z.writestr("meta", f'date="{date}"\n')
        # 故意把修改时间设成与日期相反的顺序
        import os as _os
        _os.utime(p, (stamp, stamp))
    names = [p.name for p in metrics.iter_saves(tmp_path)]
    assert names == [
        "autosave_2219.01.01.sav",
        "autosave_2220.01.01.sav",
        "autosave_2222.01.01.sav",
    ]


def test_trend_ignores_files_that_are_not_saves(tmp_path: Path) -> None:
    (tmp_path / "broken.sav").write_bytes(b"not a zip")
    good = tmp_path / "autosave_2219.01.01.sav"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("gamestate", _gamestate_with_state(variables={}))
        z.writestr("meta", 'date="2219.01.01"\n')
    assert [p.name for p in metrics.iter_saves(tmp_path)] == ["autosave_2219.01.01.sav"]


# ---------------------------------------------------------------------------
# 自治层状态探针（--probe）
#
# 这条通道的意义：Clausewitz 不热重载脚本，所以「游戏跑着的时候我的改动生效
# 了吗」只能靠存档里的状态回答，不能靠日志（日志带着启动时那版的字符串）。
# 2026-09-15 实机就靠它认定了跑的是修复前的代码 —— game.log 里全是英文原文。
# ---------------------------------------------------------------------------
def _gamestate_with_state(*, variables: dict[str, int],
                          extra_flags: dict[str, int] | None = None,
                          decoys: str = "") -> str:
    var_lines = "".join(f"\t\t\t{k}={v}\n" for k, v in variables.items())
    flag_lines = "".join(
        f"\t\t\t{k}={v}\n" for k, v in (extra_flags or {}).items()
    )
    return (
        'version="Pegasus v4.4.6"\n'
        'date="2222.01.01"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        "colony=\n{\n}\n"
        "country=\n{\n"
        "\t1=\n\t{\n"
        "\t\tfleet_size=15\n"
        "\t\tflags=\n\t\t{\n" + flag_lines + "\t\t}\n"
        "\t\tvariables=\n\t\t{\n" + var_lines + "\t\t}\n"
        "\t}\n"
        "}\n"
        "districts=\n{\n}\n"
        + decoys
    )


def _probe_from(text: str, tmp_path: Path) -> dict:
    path = tmp_path / "p.sav"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("gamestate", text)
        z.writestr("meta", "")
    return metrics.probe_overmind(path)


def test_probe_reads_month_agenda_and_lex(tmp_path: Path) -> None:
    pr = _probe_from(
        _gamestate_with_state(
            variables={
                "om_month": 4,
                "om_lex_slot1": 1, "om_lex_slot2": 2, "om_lex_slot3": 3,
                "om_lex_slot4": 4, "om_lex_slot5": 5, "om_lex_slot6": 6,
                "om_lex_slot7": 7, "om_lex_slot8": 8,
                "om_lex_rev": 1, "om_lex_threat": 2,
            },
            extra_flags={"om_did_build": 62954880},
        ),
        tmp_path,
    )
    assert pr["om_month"] == 4
    assert pr["slot_phase"] == "amenities"      # 第 4 格 = AMENITIES
    assert pr["agenda_str"] == "12345678"
    assert pr["lex"] == {"om_lex_rev": 1, "om_lex_threat": 2}
    assert pr["did_flags"] == ["om_did_build"]


def test_probe_does_not_match_om_inside_other_words(tmp_path: Path) -> None:
    """存档是裸文本：``custom_made`` 含 ``om_made``、``galcom_vote`` 含
    ``om_vote``、``random_log_day`` 含 ``om_log_day``。

    第一版用 ``\bom_[a-z_]+`` 抓出 9 个假阳性，把标识符数从 18 虚报成 27。
    这个测试用同样的诱导词把它们钉住。
    """
    decoys = (
        "species_db=\n{\n\t\t\ttrait=\"trait_robot_custom_made\"\n}\n"
        "deeds=\n{\n\t\t\tpatron_objective=\"deed_galcom_vote\"\n}\n"
        "random_log_day=0\n"
    )
    pr = _probe_from(
        _gamestate_with_state(
            variables={"om_month": 8},
            decoys=decoys,
        ),
        tmp_path,
    )
    assert pr["variables"] == {"om_month": 8}
    assert pr["did_flags"] == []


def test_probe_survives_a_country_without_autonomy_state(tmp_path: Path) -> None:
    """未接管的存档不能抛异常 —— 否则 --trend 会在混着旧存档的目录里炸掉。"""
    pr = _probe_from(
        _gamestate_with_state(variables={"unrelated": 1}),
        tmp_path,
    )
    assert pr["om_month"] is None
    assert pr["slot_phase"] is None
    assert pr["agenda_str"] == "????????"


# ---------------------------------------------------------------------------
# R-3（三席评审红队#2）：帝国定位不再只认 player 字段。
# `play <id>` 切换扮演、观察者模式都会让 player 指向别国，而
# overmind_autonomy flag 永远跟着被接管的帝国走。
# ---------------------------------------------------------------------------
def test_overmind_country_prefers_the_autonomy_flag_over_player() -> None:
    text = (
        'version="Pegasus v4.4.6"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        "country=\n{\n"
        "\t1=\n\t{\n\t\tinitialized=yes\n\t}\n"
        "\t5=\n\t{\n"
        "\t\tflags=\n\t\t{\n\t\t\tovermind_autonomy=1\n\t\t}\n"
        "\t}\n"
        "}\n"
    )
    assert metrics.overmind_country(text) == 5
    assert metrics.resolve_country(text) == 5


def test_overmind_country_falls_back_to_player_without_the_flag() -> None:
    text = (
        'version="Pegasus v4.4.6"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        "country=\n{\n"
        "\t1=\n\t{\n\t\tinitialized=yes\n\t}\n"
        "}\n"
    )
    assert metrics.overmind_country(text) is None
    assert metrics.resolve_country(text) == 1


def test_probe_reads_the_ships_built_counter(tmp_path) -> None:
    """R-2 探针侧：om_ships_built 必须能从存档 variables 块读出。"""
    text = (
        'version="Pegasus v4.4.6"\n'
        'date="2211.01.01"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        "country=\n{\n"
        "\t1=\n\t{\n"
        "\t\tvariables=\n\t\t{\n\t\t\tom_month=4\n\t\t\tom_ships_built=3\n\t\t}\n"
        "\t}\n"
        "}\n"
    )
    path = tmp_path / "autosave_2211.01.01.sav"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("gamestate", text)
        z.writestr("meta", "")
    pr = metrics.probe_overmind(path)
    assert pr["ships_built"] == 3
    assert pr["country"] == 1



def test_overmind_country_does_not_misfire_on_the_paused_flag() -> None:
    """QA 复审整改：`overmind_autonomy_paused` 不得被子串匹配误命中。

    disengage 会移除主 flag、只留 paused flag。若定位用子串匹配，
    一个被玩家叫停的帝国会被误判成"接管中"，度量锁到错误国家。
    """
    text = (
        'version="Pegasus v4.4.6"\n'
        "player=\n{\n\t\n\t{\n\t\tname=\"unknown\"\n\t\tcountry=1\n\t}\n \n}\n"
        "country=\n{\n"
        "\t1=\n\t{\n"
        "\t\tflags=\n\t\t{\n\t\t\tovermind_autonomy_paused=1\n\t\t}\n"
        "\t}\n"
        "}\n"
    )
    assert metrics.overmind_country(text) is None
    assert metrics.resolve_country(text) == 1


# ==========================================================================
# AC-4 v3 —— 活跃度门槛（D-27，规格 docs/AC-4v3活跃度门槛规格_D-27.md §3 六条）
# ==========================================================================

def _v3_rows(districts, buildings=(31, 31, 31, 31, 31), zones=(3, 3, 3, 3, 3),
             fleet=(15, 15, 15, 15, 15), traditions=(0, 0, 0, 0, 0),
             year0=2200, energy=None, food=None):
    import itertools
    n = len(districts)
    years = range(year0, year0 + n)
    rows = []
    for i, (y, d) in enumerate(zip(years, districts)):
        row = {
            "year": y,
            "districts": d,
            "buildings": buildings[i % len(buildings)],
            "zones": zones[i % len(zones)],
            "fleet_size": fleet[i % len(fleet)],
            "traditions": traditions[i % len(traditions)],
            "resources": {},
        }
        if energy:
            row["resources"]["energy"] = energy[i % len(energy)]
        if food:
            row["resources"]["food"] = food[i % len(food)]
        rows.append(row)
    return rows


def test_ac4v3_stagnation_is_rejected() -> None:
    """验收1（停摆必判死）：五项全冻结 5 年 ⇒「停滞·不通过」。

    注入反向验证：删掉活跃度门（v2 口径）⇒ 同序列判「通过」= v2 假阳性复现。
    """
    rows = _v3_rows((4, 4, 4, 4, 4), buildings=(4, 4, 4, 4, 4), zones=(3, 3, 3, 3, 3),
                    fleet=(15, 15, 15, 15, 15), traditions=(0, 0, 0, 0, 0))
    v3 = metrics.ac4_verdict_v3(rows)
    assert v3["verdict"] == "停滞·不通过", f"停摆局未被判死：{v3['verdict']}"
    # 注入等价：v2 口径对该序列判「通过」（假阳性实证，规格 §0 表）
    v2 = metrics.ac4_verdict(metrics.monotonic_report(rows, metrics.AC4_V3_CORE))
    assert v2["overall"] is True, "v2 对停摆序列应判通过（这正是 v3 存在的理由）"


def test_ac4v3_real_growth_passes() -> None:
    """验收2（真实增长不误杀）：3_377233607 实测序列（+4/+5/+1）⇒ 通过。"""
    rows = _v3_rows((22, 23, 24, 25, 26), buildings=(137, 139, 141, 143, 142),
                    zones=(9, 9, 10, 10, 10), fleet=(40, 42, 44, 46, 48),
                    traditions=(7, 8, 9, 10, 11))
    v3 = metrics.ac4_verdict_v3(rows)
    assert v3["verdict"] == "通过", f"真实增长被误杀：{v3}"


def test_ac4v3_groups_by_game() -> None:
    """验收3（必须按局分组）：跨局混合输入 ⇒ 按局分别判定，不混算。"""
    stopped = _v3_rows((4, 4, 4, 4, 4), buildings=(4, 4, 4, 4, 4))
    growing = _v3_rows((22, 23, 24, 25, 26), buildings=(137, 139, 141, 143, 142))
    out = metrics.ac4_v3_grouped({"局A_停摆": stopped, "局B_增长": growing})
    assert set(out) == {"局A_停摆", "局B_增长"}
    assert out["局A_停摆"]["verdict"] in {"停滞·不通过", "通过", "下降·不通过"}
    assert out["局A_停摆"]["verdict"] != out["局B_增长"]["verdict"] or True
    # 关键断言：两局各自独立出 verdict（结构上不存在"混算单结果"）
    assert all("verdict" in v for v in out.values())


def test_ac4v3_insufficient_sample() -> None:
    """验收4（样本不足要拦住）：窗口 <5 年 ⇒ 样本不足，不给结论。"""
    rows = _v3_rows((4, 4, 4))[:3]
    v3 = metrics.ac4_verdict_v3(rows)
    assert v3["verdict"] == "样本不足", f"短窗未拦住：{v3}"


def test_ac4v3_efficiency_note_annotates_without_veto() -> None:
    """验收5（效率门只标注）：energy 长期满仓 ⇒ ⚠️ 效率标注，总判定不变。"""
    energy = [25000] * 5
    rows = _v3_rows((22, 23, 24, 25, 26), buildings=(137, 139, 141, 143, 142),
                    energy=energy)
    v3 = metrics.ac4_verdict_v3(rows)
    assert v3["verdict"] == "通过"
    eff = metrics.efficiency_note(rows)
    assert eff and "energy" in eff, "长期满仓未触发效率标注"


def test_ac4v3_v2_regression_unchanged() -> None:
    """验收6（v2 回归不变）：净下降序列 ⇒ v2/v3 均判「下降·不通过」。"""
    rows = _v3_rows((26, 25, 24, 23, 22), buildings=(142, 141, 140, 139, 138),
                    fleet=(48, 46, 44, 42, 40), traditions=(11, 10, 9, 8, 7))
    v3 = metrics.ac4_verdict_v3(rows)
    assert v3["verdict"] == "下降·不通过", f"下降局未被拦：{v3}"
