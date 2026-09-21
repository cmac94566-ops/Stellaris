#!/usr/bin/env python3
"""Live verification for the `run` console channel.

This is the ONE unverified link in the chain.  Everything else (save parsing,
LLM decisions, ID validation, event transport) has been exercised; the `run`
command itself has never been fired at a real game.

Usage:
    py -3.12 scripts/verify_run_channel.py --prepare
    py -3.12 scripts/verify_run_channel.py --fire          # console already OPEN
    py -3.12 scripts/verify_run_channel.py --fire --toggle # let the script open it
    py -3.12 scripts/verify_run_channel.py --check

Payload design
--------------
The file consumed by `run` holds *console commands* (the binary describes it as
"Runs the specified file with lsit of commands"), NOT raw script effects.  The
native console command for flags is:

    setflag    Set a flag on a specific target. <country/planet/global> <flag> <target id>
    clearflag  Clear a flag on a specific target.

Earlier payloads used `set_country_flag = <x>`, which is an *effect* and would
be rejected.  Evidence comes from two places: the saved gamestate (authoritative)
and the game's own error.log (immediate).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GAME_DIR = Path(r"D:/SteamLibrary/steamapps/common/Stellaris")
USER_DATA_DIR = Path(r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris")
LOG_DIR = USER_DATA_DIR / "logs"
SAVE_DIR = USER_DATA_DIR / "save games"

RUN_FILE_NAME = "overmind_verify.txt"
RUN_FILE_ALIAS = "ov.txt"          # short name, fewer keystrokes for manual entry
TEST_FLAG = "overmind_run_channel_ok"
# Flags used for the un-ambiguous 2026-09-14 test (names never shown to the
# operator, so their appearance in a save can only come from the run file).
SECRET_FLAGS = ("om_rc_a1", "om_rc_b2")

# Native console command, confirmed against the 4.4.6 binary's command table:
#   setflag  "Set a flag on a specific target. <country/planet/global> <flag> <target id>"
# No comment lines and NO trailing newline — `run` feeds each line as a console
# command, so a trailing "\n" yields one empty command and a spurious
# "-> Unknown command".
TEST_PAYLOAD = f"setflag country {TEST_FLAG}"


def prepare() -> list[Path]:
    """Write the payload wherever `run` might resolve a relative path."""
    written: list[Path] = []
    for base in (GAME_DIR, USER_DATA_DIR):
        if not base.is_dir():
            continue
        for name in (RUN_FILE_ALIAS, RUN_FILE_NAME):
            target = base / name
            try:
                target.write_text(TEST_PAYLOAD, encoding="utf-8")
                written.append(target)
            except OSError as exc:
                print(f"  could not write {target}: {exc}")
    if not written:
        raise SystemExit("Neither game dir nor user data dir is writable")
    return written


def fire(toggle_console: bool = False) -> bool:
    """Type `run overmind_verify.txt` into the running game.

    By default the console is assumed to be ALREADY OPEN; pass
    toggle_console=True to let the script press the backtick key itself.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from overmind_run import find_stellaris_window, run_script_file  # noqa: E402

    hwnd = find_stellaris_window()
    if hwnd == 0:
        print("Stellaris window not found — is the game running?")
        return False

    print(f"Stellaris window hwnd={hwnd}")
    print(f"Typing (unicode, IME-safe): run {RUN_FILE_NAME}")
    ok = run_script_file(hwnd, console_open=not toggle_console)
    print("Command sent." if ok else "Injection failed.")
    return ok


def _log_marks() -> dict[str, tuple[int, float]]:
    """Snapshot log sizes so we can spot new lines after firing."""
    marks = {}
    for name in ("error.log", "game.log"):
        p = LOG_DIR / name
        marks[name] = (p.stat().st_size if p.exists() else 0, time.time())
    return marks


def tail_new_logs(since: dict[str, tuple[int, float]], lines: int = 25) -> None:
    """Print log lines appended since *since* (byte-offset based)."""
    for name, (offset, _) in since.items():
        p = LOG_DIR / name
        if not p.exists():
            continue
        size = p.stat().st_size
        if size <= offset:
            print(f"  {name}: no new lines")
            continue
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            fresh = fh.read()
        fresh_lines = [l for l in fresh.splitlines() if l.strip()]
        print(f"  {name}: {len(fresh_lines)} new line(s)")
        for l in fresh_lines[-lines:]:
            print(f"     {l}")


def check() -> int:
    """Scan the newest save (and the logs) for the verification flag."""
    saves = sorted(
        SAVE_DIR.rglob("*.sav"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    found_in_save = False
    if saves:
        newest = saves[0]
        age = time.time() - newest.stat().st_mtime
        print(f"Newest save: {newest.relative_to(SAVE_DIR)}  (age {age/60:.1f} min)")
        if age > 30 * 60:
            print("  [!] over 30 minutes old — save again for a fresh signal")
        import zipfile

        try:
            with zipfile.ZipFile(newest) as zf:
                names = [n for n in zf.namelist() if "gamestate" in n]
                if names:
                    needles = [TEST_FLAG.encode()] + [f.encode() for f in SECRET_FLAGS]
                    found_flags: set[bytes] = set()
                    with zf.open(names[0]) as fh:
                        tail = b""
                        while True:
                            chunk = fh.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            blob = tail + chunk
                            for nd in needles:
                                if nd in blob:
                                    found_flags.add(nd)
                            if len(found_flags) == len(needles):
                                break
                            tail = chunk[-64:]
                    found_in_save = TEST_FLAG.encode() in found_flags
                    for f in sorted(found_flags):
                        print(f"  flag present: {f.decode()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  could not read save: {exc}")
    else:
        print(f"No saves under {SAVE_DIR}")

    # Log-side evidence.
    err = LOG_DIR / "error.log"
    log_mentions = []
    if err.exists():
        text = err.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            low = line.lower()
            if "run overmind_verify" in low or TEST_FLAG in low or (
                "unknown command" in low and "run" in low
            ):
                log_mentions.append(line)

    print()
    if found_in_save:
        print(f"[PASS] `{TEST_FLAG}` found in the saved gamestate.")
        print("       The `run` channel works end to end.")
        return 0

    if log_mentions:
        print("[LOG] Relevant error.log entries:")
        for l in log_mentions[-10:]:
            print("   ", l)

    print(f"[FAIL] `{TEST_FLAG}` not found in the newest save.")
    print("       Check, in order:")
    print("         1. Did --fire actually type into the console?")
    print("         2. Is the run file in the directory `run` resolves against?")
    print("         3. Did the game tick a month and save AFTER firing?")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the `run` console channel")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--fire", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("--toggle", action="store_true",
                        help="with --fire: press ` to open the console yourself")
    args = parser.parse_args()

    if args.prepare:
        for p in prepare():
            print(f"Wrote {p}")
        print(TEST_PAYLOAD)
        return 0

    if args.fire:
        mark = _log_marks()
        if not fire(toggle_console=args.toggle):
            return 1
        time.sleep(2.0)
        print("\n--- new log output ---")
        tail_new_logs(mark)
        return 0

    return check()


if __name__ == "__main__":
    raise SystemExit(main())
