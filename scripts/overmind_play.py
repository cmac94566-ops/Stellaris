"""Overmind play — one command from save to governed empire (M2-1).

读档 → 模型 → 编译 → 审计 → 启动 的全链路管道：

    py -3.12 scripts/overmind_play.py                 # 全链路（含启动游戏）
    py -3.12 scripts/overmind_play.py --skip-launch   # 编译换版但不拉起游戏
    py -3.12 scripts/overmind_play.py --skip-model    # 跳过 LLM，用 code 评估
    py -3.12 scripts/overmind_play.py --dry-run       # 只编译验证，不落盘

各步骤：

1. 读档   定位最新存档（复用 overmind_takeover 的 save_dir/newest_save），
          只取「存档存在性 + 年份线索」，深度解析归 M2-5 的存档回流。
2. 模型   StrategicPlanner 产出战略上下文；任何失败（模型离线/超时/配置缺失）
          一律降级 ``assess_code`` 的确定性评估 —— 模型是增强，不是依赖。
3. 编译   ``lex_from_context`` → ``validate_payload`` → ``write_lex``。
          非法载荷在落盘前被拒（PipelineError）。
4. 审计   ``script_audit.audit_mod`` 对整个 mod 目录过审计门；有违规即失败，
          不写 sidecar、不启动游戏。
5. sidecar ``strategy_lex.save_record`` 落 FR-04 三要素：原始输出
          （raw_output）、编译结果（compiled：rev + 全部 om_lex_* 变量表）、
          时间戳（timestamp），外加审计结论与存档出处。
6. 启动   子进程调 ``overmind_takeover --launch``（-continuelastsave 进最新档）。

rev 规则：取「现役法案文件内 rev / sidecar 记录 rev」的 max + 1 —— 游戏靠
om_lex_rev 判断是否换版（AC-5），同 rev 重写等于不换版，所以必须严格递增。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import script_audit, strategy_lex as lex  # noqa: E402
from engine.strategic_planner import assess_code  # noqa: E402
from overmind_takeover import newest_save, save_dir  # noqa: E402

DEFAULT_MOD_DIR = _ROOT / "mod" / "stellaris_overmind"
TAKEOVER = _ROOT / "scripts" / "overmind_takeover.py"


class PipelineError(RuntimeError):
    """管道被门拦下（载荷非法 / 审计违规 / 缺少启动前提）。"""


# ---------------------------------------------------------------------------
# S-20 步骤 1.5：复盘采集（结构化小表，≤1500 字符，绝不塞原始日志）
# ---------------------------------------------------------------------------
REVIEW_INPUT_LIMIT = 1500


def collect_review_input(mod_dir: Path) -> dict:
    """采集上一版跑得怎么样的结构化小表（S-20 规格 §2①）。

    来源全部轻量：sidecar（rev/收益/审计）+ game.log 尾部（求值/熔断/止战
    计数）+ error.log 的 mod 行计数。不做深存档解析（那归 --trend）。
    """
    rev, span = 0, ""
    record = lex.load_record(mod_dir)
    if record:
        rev = int(record.get("rev", 0) or 0)
        span = str(record.get("save", ""))
    log_dir = Path.home() / "Documents/Paradox Interactive/Stellaris/logs"
    hits: dict[str, int] = {}
    gains: dict[str, int] = {}
    fused: list[str] = []
    game_log = log_dir / "game.log"
    if game_log.exists():
        gl = game_log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"OVERMIND: 【求值】规则 #(\d+) 命中", gl):
            hits[f"rule{m.group(1)}"] = hits.get(f"rule{m.group(1)}", 0) + 1
        fused = [
            l[l.find("OVERMIND"):]
            for l in gl.splitlines() if "【熔断】" in l
        ][-3:]
    errs = 0
    err_log = log_dir / "error.log"
    if err_log.exists():
        el = err_log.read_text(encoding="utf-8", errors="replace")
        errs = sum(1 for l in el.splitlines() if "overmind" in l.lower())

    # S-20 下版素材（ZCMSG-061 后追加）：逐规则 → 相位映射 + 相位熔断态/收益。
    # 规则→相位来自侧车记录（编译期已确证）；streak/收益来自最新存档 probe。
    # 组合后 LLM 能看见「哪条规则高频命中却零收益」—— 熔断噪音/饿死的直接证据。
    rule_acts: dict[str, int] = {}
    rules_list = record.get("rules") if isinstance(record, dict) else None
    if isinstance(rules_list, list):
        for idx, r in enumerate(rules_list, start=1):
            if isinstance(r, dict) and r.get("act"):
                rule_acts[f"rule{idx}"] = int(r["act"])

    phase_state: dict[str, dict[str, int]] = {}
    try:
        import glob as _glob
        import os as _os
        import subprocess as _sp
        pats = [
            "C:/Program Files (x86)/Steam/userdata/*/281990/remote/save games/*/*.sav",
            "C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games/*/*.sav",
        ]
        saves: list[str] = []
        for pat in pats:
            saves += _glob.glob(pat)
        if saves:
            newest = max(saves, key=_os.path.getmtime)
            pr = _sp.run(
                [sys.executable, str(Path(__file__).resolve().parent / "overmind_metrics.py"), "--probe", newest],
                capture_output=True, text=True, timeout=180,
            )
            for m in re.finditer(r"(om_lex_gain_p\d(?:_streak)?)\s+(-?\d+)", pr.stdout or ""):
                ph = phase_state.setdefault(f"p{m.group(1)[12:]}" if "_streak" in m.group(1) else m.group(1).replace("om_lex_gain_", "p"), {})
                ph["streak" if "_streak" in m.group(1) else "gain"] = int(m.group(2))
    except Exception:
        phase_state = {}

    return {
        "rev": rev,
        "span": span,
        "rule_hits": hits,
        "rule_acts": rule_acts,
        "phase_state": phase_state,
        "fused_tail": fused,
        "mod_errors": errs,
    }


def compress_review(review: dict, limit: int = REVIEW_INPUT_LIMIT) -> str:
    """把复盘结构压成 ≤limit 字符的文本块（超限逐字段截断）。"""
    body = json.dumps(review, ensure_ascii=False)
    if len(body) <= limit:
        return body
    trimmed = dict(review)
    for key in ("fused_tail", "span"):
        if key in trimmed and len(json.dumps(trimmed, ensure_ascii=False)) > limit:
            trimmed[key] = "…"
    body = json.dumps(trimmed, ensure_ascii=False)
    return body[:limit]



def model_context(state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """问一次战略模型，返回 ``(ctx_dict, raw_output)``。

    任何异常都由调用方兜底降级 —— 这里不捕获，是为了让调用方把降级
    决策和记录写在一处。
    """
    from engine.main import _build_provider  # 重导入；失败即降级

    cfg = load_config_quiet()
    provider = _build_provider(cfg)
    ruleset: dict[str, Any] = {}
    harness = _ROOT / "harness.config.json"
    if harness.exists():
        try:
            ruleset = json.loads(harness.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ruleset = {}
    from engine.strategic_planner import StrategicPlanner

    planner = StrategicPlanner(provider, ruleset, {})
    ctx = planner.plan(state)
    data = ctx.to_dict()
    data.setdefault("source", "llm")
    return data, json.dumps(data, ensure_ascii=False)


def load_config_quiet():
    from engine.config import load_config

    return load_config(None)


def code_context(state: dict[str, Any]) -> dict[str, Any]:
    """确定性评估（无 LLM）。永远成功，是管道的地板。"""
    data = assess_code(dict(state), {}).to_dict()
    data.setdefault("source", "code")
    data["year_generated"] = int(state.get("year", 2200) or 2200)
    return data


# ---------------------------------------------------------------------------
# 步骤 3：编译（带越界门）
# ---------------------------------------------------------------------------
def compile_lex(payload: lex.LexPayload, mod_dir: Path):
    """校验并写出法案文件，返回 ``(payload, vars_text, effects_text)``。"""
    problems = lex.validate_payload(payload)
    if problems:
        raise PipelineError("非法载荷被拒绝：" + "; ".join(problems))
    vars_text, effects_text = lex.render_lex_files(payload)
    lex.write_lex(payload, mod_dir)
    return payload, vars_text, effects_text


# ---------------------------------------------------------------------------
# 管道
# ---------------------------------------------------------------------------
def run_pipeline(
    mod_dir: Path = DEFAULT_MOD_DIR,
    *,
    skip_model: bool = False,
    skip_launch: bool = False,
    dry_run: bool = False,
    config_path: Path | None = None,
    audit_kw: dict[str, Any] | None = None,
    extra_args: list[str] | None = None,
    collect_review: bool = True,
    review_verbose: bool = False,
) -> dict[str, Any]:
    """执行 读档→模型→编译→审计→sidecar→启动，返回摘要 dict。

    S-20（规格 docs/LLM复盘迭代闭环规格.md）：默认采集上一版表现摘要并注入
    模型上下文；``--review``（review_verbose）额外把摘要打印出来供人工判读
    （复盘迭代版换版：``overmind_play --review --skip-launch``）；
    ``--no-review`` 关闭注入作对照实验。
    """
    extra_args = list(extra_args or [])
    audit_kw = dict(audit_kw) if audit_kw else {"check_keys": True}

    # --- 1. 读档（定位最新存档；只取存在性与出处，深度解析归 M2-5） ---
    save_path: Path | None = None
    try:
        save_path = newest_save(save_dir())
    except Exception:
        save_path = None
    if save_path is None and not (skip_launch or dry_run):
        raise PipelineError(f"找不到任何存档于 {save_dir()}，无法启动")

    # --- 2. 模型（失败降级 code）：复盘数据并入 state（S-20 步骤①→②）---
    record = lex.load_record(mod_dir) or {}
    year = int(extra_args and _arg_year(extra_args) or record.get("year", 2200) or 2200)
    state = {"year": year, "year_generated": year, "colonies": [], "fleets": []}
    review = collect_review_input(mod_dir) if collect_review else None
    if review is not None:
        state["review"] = compress_review(review)
        if review_verbose:
            print("[overmind_play] --review 复盘摘要（注入模型的原文）：\n" + state["review"])
    else:
        print("[overmind_play] --no-review：本轮不采集/注入复盘数据（对照）")
    raw_output = ""
    if skip_model:
        ctx = code_context(state)
    else:
        try:
            ctx, raw_output = model_context(state)
        except Exception as exc:  # 模型离线/配置缺失/提示词异常 —— 全部降级
            print(f"[overmind_play] 模型步骤降级为 code 评估：{exc}")
            ctx = code_context(state)

    # --- 3. 编译（越界门在落盘前生效） ---
    prev_rev = max(
        _file_rev(mod_dir),
        int(record.get("rev", 0) or 0),
    )
    payload = lex.lex_from_context(ctx, rev=prev_rev + 1)
    if dry_run:
        problems = lex.validate_payload(payload)
        if problems:
            raise PipelineError("非法载荷被拒绝：" + "; ".join(problems))
        return {"ok": True, "rev": payload.rev, "dry_run": True}
    payload, vars_text, effects_text = compile_lex(payload, mod_dir)

    # --- 4. 审计门（违规即失败，不写 sidecar 不启动） ---
    game_dir = script_audit.game_dir_from_config(config_path)
    violations = script_audit.audit_mod(mod_dir, game_dir, **audit_kw)
    if violations:
        names = "; ".join(f"{v.file}:{v.line} {v.kind} {v.token}" for v in violations[:5])
        raise PipelineError(f"审计未通过（{len(violations)} 条）：{names}")

    # --- 5. sidecar（FR-04：原始输出 + 编译结果 + 时间戳） ---
    lex.save_record(
        payload,
        mod_dir,
        extra={
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "raw_output": raw_output,
            "compiled": {
                "rev": payload.rev,
                "vars": {"om_lex_rev": lex.parse_lex_vars(vars_text)},
                "effects": lex.parse_lex_effects(effects_text),
            },
            "audit": {"violations": 0, "check_keys": audit_kw.get("check_keys", True)},
            "save": str(save_path) if save_path else "",
            "review": review,
        },
    )

    # --- 6. 启动 ---
    launched: dict[str, Any] = {"attempted": False}
    if not skip_launch:
        launched = {"attempted": True}
        proc = subprocess.run(
            [sys.executable, str(TAKEOVER), "--launch"],
            check=False,
            capture_output=True,
            text=True,
        )
        launched["returncode"] = proc.returncode
        if proc.returncode != 0:
            launched["stderr"] = (proc.stderr or "")[-400:]

    return {
        "ok": True,
        "rev": payload.rev,
        "source": payload.source,
        "agenda": payload.agenda_names(),
        "raw_output_chars": len(raw_output),
        "save": str(save_path) if save_path else "",
        "launched": launched,
    }


def _file_rev(mod_dir: Path) -> int:
    """读现役法案文件里的 rev；文件缺失/损坏按 0 处理。"""
    path = mod_dir / lex.LEX_VARS_RELPATH
    if not path.exists():
        return 0
    return lex.parse_lex_vars(path.read_text(encoding="utf-8")) or 0


def _arg_year(extra_args: list[str]) -> int:
    if "--year" in extra_args:
        i = extra_args.index("--year")
        if i + 1 < len(extra_args):
            return int(extra_args[i + 1])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="overmind_play",
        description="读档→模型→编译→审计→启动 单条命令全链路（M2-1）",
    )
    parser.add_argument("--mod-dir", type=Path, default=DEFAULT_MOD_DIR)
    parser.add_argument("--skip-model", action="store_true", help="跳过 LLM，用 code 评估")
    parser.add_argument("--skip-launch", action="store_true", help="编译换版但不启动游戏")
    parser.add_argument("--dry-run", action="store_true", help="只编译验证，不落盘")
    parser.add_argument("--fast-audit", action="store_true", help="审计跳过键名全量扫（仅 ID 扫）")
    parser.add_argument("--year", type=int, default=None, help="覆盖战略上下文年份")
    parser.add_argument("--config", type=Path, default=None)
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument("--review", action="store_true",
                              help="复盘迭代版：注入上一版表现摘要并打印供人工判读（配 --skip-launch）")
    review_group.add_argument("--no-review", action="store_true",
                              help="本轮不采集/注入复盘数据（对照实验）")
    args = parser.parse_args(argv)

    extra = ["--year", str(args.year)] if args.year is not None else []
    try:
        result = run_pipeline(
            mod_dir=args.mod_dir,
            skip_model=args.skip_model,
            skip_launch=args.skip_launch,
            dry_run=args.dry_run,
            config_path=args.config,
            audit_kw={"check_keys": not args.fast_audit},
            extra_args=extra,
            collect_review=not args.no_review,
            review_verbose=args.review,
        )
    except PipelineError as exc:
        print(f"[overmind_play] 失败：{exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
