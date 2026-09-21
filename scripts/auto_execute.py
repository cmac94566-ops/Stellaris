"""Inject constrained Overmind country events into Stellaris.

The bridge writes one ``overmind_directive_<country_id>.command`` file per AI
directive. Each file must contain exactly one allowlisted ``event overmind.*``
command. This script rejects arbitrary console text and never switches player
control or executes direct build/resource effects.

Usage:
    python scripts/auto_execute.py

    # With custom Stellaris data directory
    python scripts/auto_execute.py --stellaris-dir "C:/Users/.../Paradox Interactive/Stellaris"

Requirements:
    - Stellaris must be running (non-Ironman, non-multiplayer)
    - The game console must be accessible (` key)
    - Windows only (uses ctypes for window activation)

Note: This is optional. AI-mode directives remain pending until this injector
or another compatible transport is running.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import logging
import time
from pathlib import Path

from engine.bridge import is_ai_event_command

log = logging.getLogger(__name__)

# Windows API constants
SW_RESTORE = 9
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_OEM_3 = 0xC0  # backtick/tilde key (console toggle)
VK_MENU = 0x12  # Alt key — used to release the Windows foreground lock


def find_stellaris_window() -> int:
    """Find the Stellaris game window handle."""
    user32 = ctypes.windll.user32

    hwnd = user32.FindWindowW(None, "Stellaris")
    if hwnd:
        return hwnd

    # Try alternate titles
    for title in ("Stellaris ", "stellaris"):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd

    return 0


def activate_window(hwnd: int) -> bool:
    """Bring Stellaris window to foreground.

    Windows refuses ``SetForegroundWindow`` from a background process unless we
    first nudge the input state (Alt tap) or attach to the foreground thread.
    Without this the injector silently fails several directives per pass.
    """
    user32 = ctypes.windll.user32

    # Restore if minimized
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)

    # Alt tap clears the foreground lock so our SetForegroundWindow is honoured.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)

    try:
        curr_thread = user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), None
        )
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        if curr_thread != target_thread:
            user32.AttachThreadInput(curr_thread, target_thread, True)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(curr_thread, target_thread, False)
        else:
            user32.SetForegroundWindow(hwnd)
    except Exception:  # pragma: no cover - defensive against API edge cases
        user32.SetForegroundWindow(hwnd)

    time.sleep(0.3)

    return user32.GetForegroundWindow() == hwnd


def force_foreground(hwnd: int) -> bool:
    """Aggressively bring *hwnd* to the foreground and confirm it worked.

    ``activate_window`` is not enough when the agent's own host window (or any
    other app) holds the foreground lock.  This variant attaches to BOTH the
    current foreground thread and the target thread, then uses
    ``BringWindowToTop`` + ``SetForegroundWindow`` + ``SetFocus``.

    Returns True only if the target really owns the foreground afterwards —
    callers MUST treat False as "do not send any keys".
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)

    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True

    # Alt tap releases the foreground lock.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)

    our_thread = kernel32.GetCurrentThreadId()
    game_thread = user32.GetWindowThreadProcessId(hwnd, None)
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    attached: list[tuple[int, int]] = []
    for other in {game_thread, fg_thread}:
        if other and other != our_thread:
            if user32.AttachThreadInput(our_thread, other, True):
                attached.append((our_thread, other))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        for a, b in attached:
            user32.AttachThreadInput(a, b, False)

    time.sleep(0.35)
    return user32.GetForegroundWindow() == hwnd


def send_key(vk: int, delay: float = 0.05) -> None:
    """Send a single key press + release."""
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(delay)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(delay)


def send_text(text: str, delay: float = 0.03) -> None:
    """Type a string using SendInput.

    WARNING: this goes through the active keyboard layout AND the system IME.
    With a Chinese IME active, the letters ``run`` are interpreted as pinyin
    and committed as the character 润, so the console never sees the command.
    Prefer :func:`send_text_unicode` for anything that must arrive verbatim.
    """
    user32 = ctypes.windll.user32
    for char in text:
        # Use VkKeyScan to get virtual key for each character
        vk_result = user32.VkKeyScanW(ord(char))
        vk = vk_result & 0xFF
        shift = (vk_result >> 8) & 1

        if shift:
            user32.keybd_event(0x10, 0, 0, 0)  # Shift down

        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(delay)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

        if shift:
            user32.keybd_event(0x10, 0, KEYEVENTF_KEYUP, 0)

        time.sleep(delay)


# --------------------------------------------------------------------------
# IME-proof text injection
#
# ``keybd_event``/``VkKeyScan`` produce *virtual key* events, which the system
# IME happily converts (run -> 润).  ``SendInput`` with KEYEVENTF_UNICODE injects
# the literal character instead, bypassing both layout and IME.
# --------------------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # Must be as large as MOUSEINPUT (32 bytes on x64) — otherwise INPUT is
    # 32 bytes instead of the required 40 and SendInput fails with
    # ERROR_INVALID_PARAMETER (87) while returning 0.
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _INPUTUNION)]


# Fail loudly at import time if the layout is ever wrong again.
if ctypes.sizeof(_INPUT) != 40:
    raise RuntimeError(
        f"INPUT struct is {ctypes.sizeof(_INPUT)} bytes, expected 40 on x64"
    )


def _send_unicode_unit(ch: str) -> None:
    """Inject one character as a WM_CHAR-direct unicode key event."""
    user32 = ctypes.windll.user32
    for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
        inp = _INPUT(
            type=INPUT_KEYBOARD,
            u=_INPUTUNION(ki=_KEYBDINPUT(0, ord(ch), flags, 0, None)),
        )
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if n != 1:
            log.warning("SendInput unicode %r failed (err %d)", ch,
                        ctypes.get_last_error())


KEYEVENTF_SCANCODE = 0x0008


def send_scancode(scan: int, delay: float = 0.05) -> bool:
    """Press + release a key by *hardware scancode*.

    SDL2 (which Stellaris uses) resolves the console toggle from the scan code,
    so a scancode event is more faithful than a virtual-key event.  Returns
    whether the OS accepted the injection.
    """
    user32 = ctypes.windll.user32
    oks = []
    for flags in (KEYEVENTF_SCANCODE, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP):
        inp = _INPUT(
            type=INPUT_KEYBOARD,
            u=_INPUTUNION(ki=_KEYBDINPUT(0, scan, flags, 0, None)),
        )
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        oks.append(n == 1)
        time.sleep(delay)
    return all(oks)


SCAN_GRAVE = 0x29     # ` / ~  (console toggle)
SCAN_RETURN = 0x1C
SCAN_BACKSPACE = 0x0E


def send_text_unicode(text: str, delay: float = 0.012) -> None:
    """Type *text* character-by-character without IME interference."""
    for ch in text:
        _send_unicode_unit(ch)
        time.sleep(delay)


def send_text_unicode(text: str, delay: float = 0.012) -> None:
    """Type *text* character-by-character without IME interference."""
    for ch in text:
        _send_unicode_unit(ch)
        time.sleep(delay)


def clear_console_line(max_backspaces: int = 120) -> None:
    """Send a run of Backspaces to empty whatever is in the console input."""
    user32 = ctypes.windll.user32
    VK_BACK = 0x08
    for _ in range(max_backspaces):
        user32.keybd_event(VK_BACK, 0, 0, 0)
        user32.keybd_event(VK_BACK, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.004)


def execute_console_command(hwnd: int, command: str) -> bool:
    """Open Stellaris console, type command, execute, close console."""
    if not activate_window(hwnd):
        log.warning("Could not activate Stellaris window")
        return False

    time.sleep(0.2)

    # Open console (backtick key)
    send_key(VK_OEM_3, delay=0.1)
    time.sleep(0.3)

    # Type command
    send_text(command)
    time.sleep(0.1)

    # Press Enter
    send_key(VK_RETURN, delay=0.1)
    time.sleep(0.3)

    # Close console (backtick key again)
    send_key(VK_OEM_3, delay=0.1)

    return True


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input anywhere on the desktop.

    Used as a consent gate: while the human is typing or clicking we must not
    send keystrokes, or we hijack their input.
    """
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")
    elapsed_ms = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    # GetTickCount wraps after ~49 days; clamp negatives to zero.
    return max(elapsed_ms, 0) / 1000.0


def execute_console_batch(hwnd: int, commands: list[str]) -> int:
    """Run several allowlisted commands in one console session.

    Batch opening/closing the console keeps the on-screen flicker to a single
    open/close per pass instead of one per directive.
    """
    if not activate_window(hwnd):
        log.warning("Could not activate Stellaris window")
        return 0

    time.sleep(0.2)
    send_key(VK_OEM_3, delay=0.1)
    time.sleep(0.3)

    executed = 0
    for command in commands:
        send_text(command)
        time.sleep(0.05)
        send_key(VK_RETURN, delay=0.1)
        time.sleep(0.15)
        executed += 1

    send_key(VK_OEM_3, delay=0.1)
    return executed


def watch_and_execute(
    stellaris_dir: Path,
    poll_interval: float = 2.0,
    idle_gate_s: float = 3.0,
    batch_size: int = 8,
    foreground_only: bool = False,
    once: bool = False,
) -> None:
    """Inject each pending, allowlisted AI directive event exactly once.

    Injection is gated on user idle time. Sending keystrokes while the human is
    typing steals their input, so by default we only run when nobody has touched
    the keyboard or mouse for ``idle_gate_s`` seconds.

    ``foreground_only`` never steals focus at all — it injects only while
    Stellaris is already the active window, so working in another app is safe.
    """
    log.info("Auto-execute watching: %s", stellaris_dir / "overmind_directive_*.command")
    log.info("Make sure Stellaris is running and the console is accessible")
    log.info("Idle gate: %.1fs (input is left alone while you type/click)", idle_gate_s)
    if foreground_only:
        log.info("Foreground-only: will never steal focus from another app")
    if once:
        log.info("Once mode: flush pending directives, then exit")
    log.info("Press Ctrl+C to stop")

    while True:
        try:
            pending: list[tuple[Path, str]] = []
            for command_path in sorted(stellaris_dir.glob("overmind_directive_*.command")):
                command = command_path.read_text(encoding="utf-8").strip()
                if not is_ai_event_command(command):
                    rejected_path = command_path.with_suffix(".rejected")
                    command_path.replace(rejected_path)
                    log.error("Rejected unsafe directive command: %s", command_path.name)
                    continue
                pending.append((command_path, command))

            if not pending:
                if once:
                    log.info("No pending directives — done")
                    return
                time.sleep(poll_interval)
                continue

            # Consent gate — never type while the human is using the machine.
            if idle_seconds() < idle_gate_s:
                log.debug("User active (%d pending) — waiting", len(pending))
                time.sleep(poll_interval)
                continue

            hwnd = find_stellaris_window()
            if hwnd == 0:
                if once:
                    log.warning("Stellaris window not found — done")
                    return
                log.warning("Stellaris window not found — is the game running?")
                time.sleep(poll_interval)
                continue

            if foreground_only and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                log.debug("Stellaris not focused — skipping, not stealing focus")
                time.sleep(poll_interval)
                continue

            batch = pending[:batch_size]
            executed = execute_console_batch(hwnd, [cmd for _, cmd in batch])
            for command_path, command in batch[:executed]:
                command_path.unlink()
                log.info("Injected directive: %s", command)

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            log.info("Shutting down auto-execute")
            break
        except Exception:
            log.exception("Error in auto-execute loop")
            time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-execute Overmind directives in Stellaris console",
    )
    parser.add_argument(
        "--stellaris-dir", type=Path,
        default=None,
        help="Stellaris user data directory (auto-detected if not set)",
    )
    parser.add_argument(
        "--poll", type=float, default=2.0,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--idle-gate", type=float, default=3.0,
        help="Only inject after this many seconds of no keyboard/mouse input",
    )
    parser.add_argument(
        "--batch", type=int, default=8,
        help="Max directives to run per console session",
    )
    parser.add_argument(
        "--foreground-only", action="store_true",
        help="Never steal focus: inject only while Stellaris is the active window",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Flush all pending directives in one burst, then exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [auto-exec] %(message)s",
        datefmt="%H:%M:%S",
    )

    stellaris_dir = args.stellaris_dir
    if stellaris_dir is None:
        # Auto-detect common Stellaris user data paths
        for candidate in [
            Path.home() / "OneDrive/Documents/Paradox Interactive/Stellaris",
            Path.home() / "Documents/Paradox Interactive/Stellaris",
        ]:
            if candidate.exists():
                stellaris_dir = candidate
                break
        if stellaris_dir is None:
            log.error("Cannot find Stellaris directory. Use --stellaris-dir.")
            raise SystemExit(1)

    if not stellaris_dir.exists():
        log.error("Stellaris directory not found: %s", stellaris_dir)
        raise SystemExit(1)

    watch_and_execute(
        stellaris_dir,
        args.poll,
        idle_gate_s=args.idle_gate,
        batch_size=args.batch,
        foreground_only=args.foreground_only,
        once=args.once,
    )


if __name__ == "__main__":
    main()
