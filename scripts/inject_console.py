#!/usr/bin/env python3
"""Inject ONE console line into the running Stellaris game.

Why this exists
---------------
The keyboard path is fully automated now, and the recipe is non-obvious, so it
lives in exactly one place:

  * the game window must REALLY be foreground (re-checked, never assumed) —
    the agent host steals focus between tool calls, so activation and typing
    must happen inside the same call;
  * the command text is injected as KEYEVENTF_UNICODE events — a Chinese IME
    otherwise rewrites ``run`` into 润 and the console says "unknown command";
  * Return is sent as a hardware SCANCODE, which is what SDL2 listens for.

Usage
-----
    py -3.12 scripts/inject_console.py --line "effect_file om_effects.txt"
    py -3.12 scripts/inject_console.py --line "run om.txt" --toggle-console
    py -3.12 scripts/inject_console.py --line "print_flags" --shot out.png
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auto_execute import (  # noqa: E402
    SCAN_GRAVE,
    SCAN_RETURN,
    clear_console_line,
    find_stellaris_window,
    force_foreground,
    idle_seconds,
    send_scancode,
    send_text_unicode,
)

_u32 = ctypes.WinDLL("user32", use_last_error=True)


def inject(
    line: str,
    toggle_console: bool = False,
    reopen_console: bool = False,
    settle: float = 1.2,
) -> bool:
    hwnd = find_stellaris_window()
    if hwnd == 0:
        print("ABORT: Stellaris window not found")
        return False

    if not force_foreground(hwnd):
        print("ABORT: game window would not take focus — sending no keys")
        return False
    # Re-check: activation is asynchronous and can be reverted by the host.
    fg = _u32.GetForegroundWindow()
    if fg != hwnd:
        print(f"ABORT: foreground is {fg}, expected {hwnd}")
        return False
    if idle_seconds() < 1.0:
        print("ABORT: user is typing right now")
        return False

    time.sleep(0.25)
    if reopen_console:
        # The console panel can stay open while its INPUT LINE loses keyboard
        # focus (a popup, or a click on the map, is enough).  Closing and
        # reopening guarantees the text field is focused again.
        send_scancode(SCAN_GRAVE)
        time.sleep(0.5)
        send_scancode(SCAN_GRAVE)
        time.sleep(0.6)
    if toggle_console:
        send_scancode(SCAN_GRAVE)
        time.sleep(0.45)

    clear_console_line()
    time.sleep(0.2)
    send_text_unicode(line)
    time.sleep(0.25)
    send_scancode(SCAN_RETURN)
    time.sleep(settle)

    if toggle_console:
        send_scancode(SCAN_GRAVE)
    print(f"INJECTED: {line}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="Inject one console line into Stellaris")
    p.add_argument("--line", required=True, help="the console command to type")
    p.add_argument("--toggle-console", action="store_true",
                   help="press the backtick key before/after (console currently closed)")
    p.add_argument("--reopen", action="store_true",
                   help="close+reopen the console first to restore input focus")
    p.add_argument("--shot", type=Path, default=None, help="save a screenshot afterwards")
    p.add_argument("--settle", type=float, default=1.2)
    args = p.parse_args()

    ok = inject(args.line, args.toggle_console, args.reopen, args.settle)
    if args.shot is not None:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(args.shot)
        print(f"shot -> {args.shot}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
