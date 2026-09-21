#!/usr/bin/env python3
"""Overmind hard-control executor.

Unlike ``auto_execute.py`` (which types a full ``event ...`` command per
directive), this executor batches everything into ONE script file and runs it
with the game's native ``run`` console command:

    run overmind_run.txt          <- 20 keystrokes total, regardless of payload

The ``run`` command is documented in the game binary as
"Runs the specified file with list of commands", so the whole batch is parsed
by Clausewitz itself — no console length limit, no fragile long typing.

Two input flavours are accepted from the watch directory:
  *.json     -> {"actions": [...]} compiled through engine.action_compiler
  *.command  -> already-final console line (legacy), validated then batched

Usage:
    py -3.12 scripts/overmind_run.py --foreground-only
    py -3.12 scripts/overmind_run.py --once
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.action_compiler import compile_actions  # noqa: E402

# Reuse the proven window/keystroke plumbing from the legacy injector.
from auto_execute import (  # noqa: E402
    SCAN_GRAVE,
    SCAN_RETURN,
    VK_OEM_3,
    VK_RETURN,
    activate_window,
    clear_console_line,
    find_stellaris_window,
    force_foreground,
    idle_seconds,
    send_key,
    send_scancode,
    send_text,
    send_text_unicode,
)

log = logging.getLogger("overmind.run")

GAME_DIR = Path(r"D:/SteamLibrary/steamapps/common/Stellaris")
RUN_FILE_NAME = "overmind_run.txt"

_DEFAULT_WATCH = Path(
    r"C:/Users/<user>/Documents/Paradox Interactive/Stellaris/mod/stellaris_overmind/ai_bridge"
)

# Lines we refuse to pass through even in a legacy *.command file.
_FORBIDDEN = ("add_resource", "research_all_technologies", "activate_all_traditions",
              "kill_pop", "destroy_colony", "destroy_country", "remove_planet")


def _script_is_safe(line: str) -> bool:
    low = line.lower()
    return not any(bad in low for bad in _FORBIDDEN)


def collect_scripts(watch_dir: Path) -> tuple[list[str], list[Path], list[str]]:
    """Harvest every pending directive and compile it into console lines.

    Returns (script_lines, consumed_paths, errors).
    """
    scripts: list[str] = []
    consumed: list[Path] = []
    errors: list[str] = []

    for path in sorted(watch_dir.glob("*.json")):
        if path.name.startswith("directive_"):
            continue  # legacy AI-pose directives: handled by auto_execute.py
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: unreadable ({exc})")
            consumed.append(path)
            continue
        actions = payload.get("actions") if isinstance(payload, dict) else None
        if not actions:
            errors.append(f"{path.name}: no 'actions'")
            consumed.append(path)
            continue
        lines, errs = compile_actions(actions)
        scripts.extend(lines)
        errors.extend(f"{path.name}: {e}" for e in errs)
        consumed.append(path)

    for path in sorted(watch_dir.glob("*.command")):
        try:
            line = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not line:
            consumed.append(path)
            continue
        if _script_is_safe(line):
            scripts.append(line)
        else:
            errors.append(f"{path.name}: rejected unsafe command")
        consumed.append(path)

    # De-duplicate while keeping order (repeated identical policy sets are noise).
    seen: set[str] = set()
    unique = []
    for s in scripts:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique, consumed, errors


def write_run_file(scripts: list[str]) -> Path:
    target = GAME_DIR / RUN_FILE_NAME
    # NO trailing newline: `run` feeds each line as a console command, so a
    # trailing "\n" becomes one empty command and prints a spurious
    # "-> Unknown command" after an otherwise-successful batch.
    target.write_text("\n".join(scripts), encoding="utf-8")
    return target


def run_script_file(hwnd: int, console_open: bool = False) -> bool:
    """Type ``run overmind_run.txt`` into the console and execute it.

    Verified working recipe (2026-09-14, Stellaris 4.4.6):
      * bring the game to the foreground and RE-CHECK, else send nothing;
      * inject the command text as unicode (a Chinese IME otherwise rewrites
        ``run`` into 润 and the console reports an unknown command);
      * send Return as a hardware scancode.

    A *successful* ``run`` may still print "-> Unknown command"; that is a
    quirk of the console command itself, not a failure.
    """
    if not force_foreground(hwnd):
        log.warning("Game window would not take focus — sending no keys")
        return False
    time.sleep(0.25)
    if not console_open:
        send_scancode(SCAN_GRAVE)
        time.sleep(0.45)
    clear_console_line()          # wipe any IME leftovers in the input box
    send_text_unicode(f"run {RUN_FILE_NAME}")
    time.sleep(0.2)
    send_scancode(SCAN_RETURN)
    time.sleep(0.4)
    if not console_open:
        send_scancode(SCAN_GRAVE)
    return True


def watch(
    watch_dir: Path,
    poll_interval: float = 2.0,
    idle_gate_s: float = 3.0,
    foreground_only: bool = False,
    once: bool = False,
) -> int:
    log.info("Watching %s", watch_dir)
    log.info("Run file: %s", GAME_DIR / RUN_FILE_NAME)
    log.info("Idle gate %.1fs | foreground-only=%s", idle_gate_s, foreground_only)
    executed_total = 0

    while True:
        scripts, consumed, errors = collect_scripts(watch_dir)
        for e in errors:
            log.warning("%s", e)

        if scripts:
            if idle_seconds() < idle_gate_s:
                log.debug("User active, %d scripts pending — waiting", len(scripts))
                time.sleep(poll_interval)
                if not once:
                    continue
            else:
                hwnd = find_stellaris_window()
                if hwnd == 0:
                    if foreground_only:
                        log.debug("Game not in foreground — skipping")
                    else:
                        log.warning("Stellaris window not found")
                    if once:
                        return executed_total
                    time.sleep(poll_interval)
                    continue

                target = write_run_file(scripts)
                if run_script_file(hwnd):
                    for p in consumed:
                        try:
                            p.unlink()
                        except OSError:
                            pass
                    executed_total += len(scripts)
                    log.info("Ran %d command(s) via %s", len(scripts), target.name)
                    for s in scripts:
                        log.info("  %s", s)
                else:
                    log.warning("Injection failed — will retry")

        if once:
            return executed_total
        time.sleep(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Overmind hard-control executor")
    parser.add_argument("--watch-dir", type=Path, default=_DEFAULT_WATCH)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--idle-gate", type=float, default=3.0)
    parser.add_argument("--foreground-only", action="store_true",
                        help="only inject when the game window has focus")
    parser.add_argument("--once", action="store_true",
                        help="flush the pending queue once, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="compile and print without touching the game")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.dry_run:
        scripts, _, errors = collect_scripts(args.watch_dir)
        for e in errors:
            print("ERR ", e)
        print(f"--- {len(scripts)} command(s) would be written to {RUN_FILE_NAME} ---")
        for s in scripts:
            print("  ", s)
        return 0

    watch(
        args.watch_dir,
        poll_interval=args.poll,
        idle_gate_s=args.idle_gate,
        foreground_only=args.foreground_only,
        once=args.once,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
