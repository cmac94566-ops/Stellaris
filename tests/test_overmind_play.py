"""Pipeline guards for ``scripts/overmind_play.py`` (M2-1 + M2-3) — zcode line.

M2-1  读档→模型→编译→审计→启动 单条命令全链路。
M2-3  法案 sidecar 审计记录（FR-04：原始输出 + 编译结果 + 时间戳，字段完整）。

契约（测试锁定，注入反向验证过）：
* 管道在模型不可用时必须降级到 code 评估继续跑（模型是增强不是依赖）；
* 审计门：审计不过 → 管道失败且不落任何文件；
* rev 严格单调：每次成功运行产出更高的法案版本（读现有文件/记录取 max+1）；
* sidecar 必含 FR-04 三要素，缺一即为回归。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MOD_DIR = REPO_ROOT / "mod" / "stellaris_overmind"
SCRIPTS_DIR = REPO_ROOT / "scripts"

#: 审计走 ID 扫（快），键名全量扫由 test_script_audit 在真实 mod 上常态化覆盖。
PIPELINE_KW = {"check_keys": False}


def _load_play():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "overmind_play", SCRIPTS_DIR / "overmind_play.py"
    )
    assert spec and spec.loader, "找不到 scripts/overmind_play.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def play():
    return _load_play()


@pytest.fixture()
def mod_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "stellaris_overmind"
    shutil.copytree(MOD_DIR, dest)
    # sidecar 是运行时产物（M2-4 等真实运行会写入）：测试一律从空白态开始
    rec = dest / "overmind_lex_record.json"
    if rec.exists():
        rec.unlink()
    return dest


def _run(play, mod_dir: Path, *extra: str):
    return play.run_pipeline(
        mod_dir=mod_dir,
        skip_model=True,
        skip_launch=True,
        audit_kw=PIPELINE_KW,
        extra_args=list(extra),
    )


def test_pipeline_writes_lex_and_fr04_sidecar(play, mod_copy: Path) -> None:
    """M2-1/M2-3：全链路产出法案文件 + FR-04 三要素齐全的 sidecar。"""
    result = _run(play, mod_copy)
    assert result["ok"], f"管道失败：{result}"
    effects = mod_copy / "common/scripted_effects/overmind_lex.txt"
    varsf = mod_copy / "common/scripted_variables/overmind_lex_vars.txt"
    assert effects.exists() and varsf.exists(), "管道未产出法案文件"
    record_path = mod_copy / "overmind_lex_record.json"
    assert record_path.exists(), "sidecar 不存在（FR-04 验收失败）"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    # FR-04：原始输出 + 编译结果 + 时间戳
    assert isinstance(record.get("raw_output"), str), "sidecar 缺原始输出字段"
    compiled = record.get("compiled") or {}
    assert compiled.get("rev") == record.get("rev"), "sidecar 编译结果缺 rev 对账"
    assert isinstance(compiled.get("effects"), dict) and compiled["effects"], (
        "sidecar 编译结果缺 effects 变量表"
    )
    ts = str(record.get("timestamp", ""))
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts), (
        f"sidecar 时间戳缺失或格式非法：{ts!r}"
    )
    assert record.get("audit", {}).get("violations") == 0, "sidecar 未记录审计结论"


def test_pipeline_rev_is_monotonic_across_runs(play, mod_copy: Path) -> None:
    """M2-1：连续运行 rev 严格递增（取「文件内 rev / sidecar rev」的 max+1）。"""
    r1 = _run(play, mod_copy)
    r2 = _run(play, mod_copy)
    assert r1["ok"] and r2["ok"]
    assert r2["rev"] > r1["rev"], (
        f"rev 未单调递增：{r1['rev']} -> {r2['rev']}（换版戳切换 AC-5 的前提）"
    )


def test_pipeline_aborts_on_illegal_payload_without_writing(play, mod_copy: Path) -> None:
    """M2-1：非法载荷必须被门拒绝，且不落地任何文件（越界拒绝在管道层复生效）。"""
    from engine.strategy_lex import LexPayload

    before = (mod_copy / "common/scripted_effects/overmind_lex.txt").read_text(
        encoding="utf-8"
    )
    bad = LexPayload(rev=99, slots=[1, 2, 3, 4, 5, 6, 7, 99], threat=2)
    with pytest.raises(play.PipelineError):
        play.compile_lex(bad, mod_copy)
    after = (mod_copy / "common/scripted_effects/overmind_lex.txt").read_text(
        encoding="utf-8"
    )
    assert before == after, "非法载荷被拒后 mod 文件仍被改动"


def test_model_failure_degrades_to_code_source(play, mod_copy: Path, monkeypatch) -> None:
    """M2-1：模型步骤失败 → 降级 code 评估，管道照样成功（模型不是依赖）。"""
    def _boom(*a, **k):
        raise ConnectionError("model offline")

    monkeypatch.setattr(play, "model_context", _boom)
    result = _run(play, mod_copy)
    assert result["ok"], f"模型失败后管道未降级：{result}"
    record = json.loads(
        (mod_copy / "overmind_lex_record.json").read_text(encoding="utf-8")
    )
    assert record["raw_output"] == "", "降级时原始输出应为空串而非垃圾数据"
    assert record.get("source") == "code", f"降级来源未标注：{record.get('source')!r}"


def test_pipeline_refuses_to_ship_when_audit_fails(play, mod_copy: Path) -> None:
    """M2-1：审计门有违规 → 管道失败，且 sidecar 不得落地（先审计后落盘）。"""
    bogus = mod_copy / "common/scripted_effects/zzz_bogus_injection.txt"
    bogus.write_text(
        'overmind_bogus_effect = {\n\ttotally_invented_effect_12345 = yes\n}\n',
        encoding="utf-8",
    )
    with pytest.raises(play.PipelineError) as excinfo:
        play.run_pipeline(
            mod_dir=mod_copy,
            skip_model=True,
            skip_launch=True,
            audit_kw={"check_keys": True},
        )
    assert "审计未通过" in str(excinfo.value)
    assert not (mod_copy / "overmind_lex_record.json").exists(), (
        "审计违规时 sidecar 仍然落地 —— 审计门形同虚设"
    )


# ==========================================================================
# S-20 —— LLM 复盘闭环（规格 docs/LLM复盘迭代闭环规格.md §4 验收 1/2 + §6.4 触发器）
# ==========================================================================

def test_s20_review_collectible_and_bounded(play, mod_copy: Path) -> None:
    """验收 1：ReviewInput 可离线采集，五字段齐备，压缩后 ≤1500 字符。"""
    review = play.collect_review_input(mod_copy)
    for key in ("rev", "span", "rule_hits", "rule_acts", "phase_state", "fused_tail", "mod_errors"):
        assert key in review, f"复盘字段缺失：{key}"
    compressed = play.compress_review(review)
    assert 0 < len(compressed) <= play.REVIEW_INPUT_LIMIT


def test_s20_review_injected_into_model_state(play, mod_copy: Path, monkeypatch) -> None:
    """验收 2：复盘数据确实进入模型上下文（摘掉注入 → 本守卫失败）。"""
    captured: dict = {}
    real = play.code_context

    def _capture(state):
        captured.update(state)
        return real(state)

    monkeypatch.setattr(play, "code_context", _capture)
    result = _run(play, mod_copy)
    assert result["ok"]
    assert "review" in captured and captured["review"], "复盘段未注入模型 state"


def test_s20_no_review_flag_omits_injection(play, mod_copy: Path, monkeypatch) -> None:
    """§6.4 触发器：--no-review（collect_review=False）时不注入，供对照实验。"""
    captured: dict = {}
    real = play.code_context

    def _capture(state):
        captured.update(state)
        return real(state)

    monkeypatch.setattr(play, "code_context", _capture)
    result = play.run_pipeline(
        mod_dir=mod_copy,
        skip_model=True,
        skip_launch=True,
        audit_kw=PIPELINE_KW,
        collect_review=False,
    )
    assert result["ok"]
    assert "review" not in captured, "collect_review=False 仍注入了复盘段"
