"""Overmind launcher — boot Stellaris straight into the newest save, console-free.

Why this exists
---------------
Stellaris has **no external input channel**: Clausewitz cannot read files, and
the only way to push decisions into a *running* session is the debug console.
That is why an earlier design needed `scripts/inject_console.py` typing commands
with a keyboard emulator — clumsy, and it steals the user's keyboard.

This script removes the console entirely by using the engine's **own**
command-line switches, verified against the string pool of the installed
`stellaris.exe` (see `--flags`):

    -continuelastsave   boot straight into the newest save   (frontendidler.cpp)
    -game_paused false  do not sit paused after loading

**Why `-human_ai` is NOT a default (defect D-3).**  The engine does support it
(the binary documents `playme` as "human_ai and ai_ignore_was_human for the
price of one"), and an earlier revision enabled it here.  It was removed
because handing the empire to the *built-in* AI is exactly the outcome this
project exists to avoid: the built-in AI is weak, and — decisively — using it
makes the run ineligible for achievements, which is the whole reason a
large-model governor was wanted in the first place.  The switch is still
reachable via ``--human-ai`` for experiments, never by default.

Governance is done by the **in-game autonomy layer** (the compiled Lex), which
boots with the save; the launcher's only job is to get the game running with the
newest Lex loaded.

Usage
-----
    py -3.12 scripts/overmind_takeover.py --flags          # list engine switches
    py -3.12 scripts/overmind_takeover.py --verify         # check + dry run
    py -3.12 scripts/overmind_takeover.py --launch         # boot the game
    py -3.12 scripts/overmind_takeover.py --watch 30       # prove the AI is playing
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONFIG_PATH = REPO_ROOT / "config.toml"

# Flags we want. Each is verified against the exe before use, so a wrong
# spelling fails loudly instead of silently doing nothing.
DEFAULT_FLAGS = ["-continuelastsave"]

# Extra switches we know about but do not enable by default.
OPTIONAL_FLAGS = {
    "unpause": ["-game_paused", "false"],
    "berserk": ["-berserk_ai"],
    "overnight": ["-overnight"],
    "console": ["-console"],
    "quick": ["-quick"],
}

# Anchor for the engine's data-directory table; the CLI switch table follows it.
_TABLE_ANCHOR = b"common/resource_converters\x00"
_TABLE_END = b"\x00main task\x00"

#: Switch names are lowercase words; the region also holds data-directory
#: entries (``Stellaris``, ``common/resource_converters``) and profiler section
#: labels (``RunGame``, ``augustus``).  Filtering to lowercase, slash-free,
#: dash-free words drops those without having to know where the boundary is.
_FLAG_NAME = re.compile(r"[a-z][a-z_0-9]*\Z")

#: The launcher's registered switches are **not** all in one table.
#: ``-human_ai`` sits in the data-directory table, but ``-continuelastsave`` —
#: the one switch this project actually depends on — lives in the string pool
#: used by the front-end idle handler.  A single-table sweep therefore
#: "verified" ``-human_ai`` while silently failing to verify the flag that
#: matters, which made the whole check worthless.  So verification also sweeps
#: the entire binary for switch-shaped tokens.
_ANY_FLAG = re.compile(rb"(?<![A-Za-z0-9_])-([a-z][a-z_0-9]{2,29})(?![A-Za-z0-9_])")


def _load_toml() -> dict:
    with open(CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def install_dir() -> Path:
    cfg = _load_toml()
    raw = cfg.get("stellaris", {}).get("install_dir", "")
    if not raw:
        raise SystemExit("config.toml 缺少 [stellaris] install_dir")
    return Path(raw)


def save_dir() -> Path:
    cfg = _load_toml()
    raw = cfg.get("bridge", {}).get("save_dir") or cfg.get("stellaris", {}).get(
        "user_data_dir", ""
    )
    return Path(raw)


# ----------------------------------------------------------------------
# Engine switch discovery (read the exe, do not guess)
# ----------------------------------------------------------------------
def _normalize_flag_token(tok: str) -> str:
    """Reduce a raw table entry to its bare switch name.

    The engine's switch table mixes several shapes::

        berserk_ai            bare switch
        humanai               bare switch (legacy spelling, also present)
        game_paused false     switch + default value
        threads=              switch taking an inline value
        -editor               switch written with its leading dash

    Comparing a requested ``-flag`` against the raw entry only works for the
    first shape.  ``game_paused false`` in particular made ``--unpause`` report
    the engine's own documented switch as unregistered:

        [00:21:...] 引擎不识别: -game_paused

    even though the binary documents it as "Toggles/Sets the game paused
    state".  A verifier that cries wolf is worse than no verifier, so the
    entry is split into name + default value here.
    """
    tok = tok.strip().rstrip("=")
    if " " in tok:
        tok = tok.split(" ", 1)[0]
    return tok.lstrip("-")


def discover_cli_flags(exe: Path | None = None, *, whole_file: bool = True) -> list[str]:
    """Extract the launcher's registered command-line switches from the binary.

    Two passes: the structured data-directory/switches table, then — because
    some switches are stored elsewhere in the string pool — a whole-binary sweep
    for ``-switch`` shaped tokens.  Returns bare names (no dash, no default
    value), so callers can compare them with what they intend to pass.

    The table is a NUL-separated list, so it is **split**, not regex-matched.
    An earlier revision matched ``\\x00name\\x00`` with a non-overlapping regex,
    which consumes both delimiters and therefore skipped every other entry:
    ``userdir``, ``berserk_ai``, ``overnight`` and ``gamestatetimer`` were all
    invisible, and any of them would have been reported as unregistered.
    """
    exe = exe or (install_dir() / "stellaris.exe")
    data = exe.read_bytes()
    out: list[str] = []

    start = data.find(_TABLE_ANCHOR)
    if start >= 0:
        end = data.find(_TABLE_END, start)
        if end < 0:
            end = start + 4096
        for raw in data[start:end].split(b"\x00"):
            text = raw.decode("ascii", "ignore").strip()
            if "/" in text or "\\" in text:  # data-directory entries
                continue
            tok = _normalize_flag_token(text)
            if not _FLAG_NAME.match(tok):
                continue
            if tok not in out:
                out.append(tok)

    if whole_file:
        for m in _ANY_FLAG.finditer(data):
            tok = m.group(1).decode("ascii", "ignore")
            if tok not in out:
                out.append(tok)
    return out


def known_switch(name: str) -> str | None:
    """Return the exact registered spelling for a switch, or None."""
    flags = discover_cli_flags()
    bare = name.lstrip("-").replace("_", "")
    for f in flags:
        if f.replace("_", "") == bare:
            return f
    return None


# ----------------------------------------------------------------------
# Launch
# ----------------------------------------------------------------------
def build_flags(args: argparse.Namespace) -> list[str]:
    flags: list[str] = []
    if args.no_continue:
        pass
    else:
        flags.append("-continuelastsave")
    if getattr(args, "human_ai", False):
        flags.append("-human_ai")
    for key in ("unpause", "berserk", "overnight", "console", "quick"):
        if getattr(args, key, False):
            flags.extend(OPTIONAL_FLAGS[key])
    flags.extend(args.extra)
    return flags


def verify_flags(flags: list[str]) -> tuple[list[str], list[str]]:
    """Split flags into (verified, unverified) using the engine's own table."""
    table = discover_cli_flags()
    if not table:
        return [], list(flags)
    joined = {t.replace("_", "") for t in table}
    ok, bad = [], []
    for f in flags:
        if not f.startswith("-"):
            continue
        bare = f.lstrip("-").replace("_", "")
        (ok if bare in joined else bad).append(f)
    return ok, bad


def launch(flags: list[str], exe: Path | None = None) -> int:
    """Start the game and return immediately.

    The child is detached on purpose.  A plain ``Popen`` leaves the game in the
    *caller's* process group, so when the launching shell (or the agent session
    that owns it) goes away, the game is torn down with it — observed as
    "error.log written, then the process vanishes mid-load".  A long unattended
    run needs the game to outlive whatever started it, hence
    ``DETACHED_PROCESS``.
    """
    exe = exe or (install_dir() / "stellaris.exe")
    if not exe.exists():
        print(f"找不到 {exe}")
        return 1
    cmd = [str(exe), *flags]
    print("启动: " + " ".join(cmd))
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    subprocess.Popen(
        cmd,
        cwd=str(exe.parent),
        close_fds=True,
        creationflags=creationflags,
    )
    return 0


# ----------------------------------------------------------------------
# Proof-of-play watcher
# ----------------------------------------------------------------------
def newest_save(directory: Path) -> Path | None:
    saves = sorted(
        glob.glob(str(directory / "**" / "*.sav"), recursive=True),
        key=os.path.getmtime,
    )
    return Path(saves[-1]) if saves else None


_COUNTERS = {
    "区划": rb"\bdistrict\b",
    "建筑": rb"\n\t\t\tbuilding=",
    "舰队": rb"\n\t\tfleet=",
    "科技": rb"\n\t\t\ttechnology=",
}


def snapshot(path: Path) -> dict[str, int]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "gamestate" not in names:
            return {}
        raw = z.read("gamestate")
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return {}
    # Slice down to the player country block so the counts describe the empire.
    m = re.search(r"\nplayer=\{[^}]*\}", text)
    if m:
        text = text[m.start() : m.start() + 6_000_000]
    out: dict[str, int] = {"大小(MB)": round(len(raw) / 1e6, 1)}
    for label, pat in _COUNTERS.items():
        out[label] = len(re.findall(pat, text))
    return out


def watch(minutes: float, interval: float = 20.0) -> int:
    directory = save_dir()
    print(f"监视目录: {directory}")
    baseline_path = newest_save(directory)
    if not baseline_path:
        print("没有找到存档。")
        return 1
    base = snapshot(baseline_path)
    print(f"基准存档: {baseline_path.name}")
    print("  " + "  ".join(f"{k}={v}" for k, v in base.items()))
    print()
    print(f"每 {interval:.0f}s 取样一次，共 {minutes:.0f} 分钟。存档月份更新即代表 AI 正在推进。")

    deadline = time.time() + minutes * 60
    last_name = baseline_path.name
    while time.time() < deadline:
        time.sleep(interval)
        cur_path = newest_save(directory)
        if cur_path is None:
            continue
        cur = snapshot(cur_path)
        if cur_path.name != last_name:
            print(f"[{time.strftime('%H:%M:%S')}] 新存档 {cur_path.name}")
            last_name = cur_path.name
        delta = {k: cur.get(k, 0) - base.get(k, 0) for k in base}
        moved = {k: v for k, v in delta.items() if v}
        if moved:
            print(f"[{time.strftime('%H:%M:%S')}] 变化: " + "  ".join(f"{k}{v:+d}" for k, v in moved.items()))
            base = cur
    print()
    print("监视结束。")
    return 0


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Overmind AI 接管启动器（零控制台）")
    ap.add_argument("--flags", action="store_true", help="列出引擎已注册的命令行开关")
    ap.add_argument("--verify", action="store_true", help="校验开关是否被引擎识别（并 dry-run）")
    ap.add_argument("--launch", action="store_true", help="按配置启动游戏")
    ap.add_argument("--watch", type=float, metavar="分钟", help="监视存档增长，证明 AI 在玩")
    ap.add_argument(
        "--human-ai",
        action="store_true",
        help="把玩家国家交给游戏自带 AI（非默认；会失去成就，仅供实验）",
    )
    ap.add_argument("--no-continue", action="store_true", help="不自动续档，进主菜单")
    ap.add_argument("--unpause", action="store_true", help="附加 -game_paused false")
    ap.add_argument("--berserk", action="store_true", help="附加 -berserk_ai")
    ap.add_argument("--overnight", action="store_true", help="附加 -overnight")
    ap.add_argument("--console", action="store_true", help="附加 -console")
    ap.add_argument("--quick", action="store_true", help="附加 -quick")
    ap.add_argument("--extra", action="append", default=[], help="追加原始参数，可多次")
    args = ap.parse_args(argv)

    if args.flags:
        flags = discover_cli_flags()
        print(f"引擎已注册 {len(flags)} 个启动开关：")
        for f in flags:
            print("  " + f)
        return 0

    if args.watch is not None:
        return watch(args.watch)

    flags = build_flags(args)
    ok, bad = verify_flags(flags)
    print("计划参数: " + " ".join(flags))
    print("引擎已识别: " + (" ".join(ok) if ok else "(无)"))
    if bad:
        print("引擎不识别: " + " ".join(bad) + "  <-- 这些会被游戏忽略")

    if args.launch:
        return launch(flags)
    if not args.verify:
        print()
        print("这是 dry-run。加 --launch 真正启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
