"""
Bridge — Communication between Stellaris and the Python engine.

Two modes of operation:

**Mode A — Autosave Reader (recommended):**
  The Python engine watches the Stellaris autosave directory for new ``.sav``
  files, parses them directly with ``save_reader.py``, and extracts game state.
  No Clausewitz exporter mod needed.

**Mode B — JSON File Bridge (legacy):**
  A Clausewitz mod writes ``state_snapshot.json`` to a shared directory.
  The Python engine polls for changes.

**Directive output (both modes):**
The engine writes ``directive.json`` to the bridge directory. For AI mode it also
writes one allowlisted country-event command when ``command_dir`` is configured.
The optional Windows injector consumes that command and targets the mod event.

Flow (Mode A):
  1. Stellaris autosaves → ``save games/<empire>/*.sav``
  2. SaveBridge detects new save, parses it
    3. Engine processes → writes the directive audit record and event command
    4. The optional injector targets the directive event at the AI country

Flow (Mode B — legacy):
  1. Clausewitz mod writes ``state_snapshot.json``
  2. BridgeReader detects change
    3. Engine processes → writes the directive audit record and event command
    4. The optional injector targets the directive event at the AI country
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Fields that should never appear in known_empires at low/no intel
_FOW_SUSPECT_FIELDS = {"economy", "economy_class", "tech_count", "known_fleet_power",
                       "military_power", "fleet_power", "resources"}

AI_EVENT_IDS = {
    "EXPAND": 101,
    "BUILD_FLEET": 102,
    "IMPROVE_ECONOMY": 103,
    "FOCUS_TECH": 104,
    "DIPLOMACY": 105,
    "PREPARE_WAR": 106,
    "DEFEND": 107,
    "CONSOLIDATE": 108,
    "COLONIZE": 109,
    "BUILD_STARBASE": 110,
    "ESPIONAGE": 111,
}
_AI_ACTION_BY_EVENT_ID = {event_id: action for action, event_id in AI_EVENT_IDS.items()}
_AI_EVENT_COMMAND_PATTERN = re.compile(r"event overmind\.(\d+) ([1-9]\d*)\Z")

# ---------------------------------------------------------------------------
# Player-family directive transport.
#
# Mirrors AI_EVENT_IDS but targets the overmind.201-211 events, whose trigger
# is `is_ai = no`.  Used when config.target.mode == "player" so the engine can
# drive the human player's own empire instead of the computer empires.
#
# The player command may omit the country ID: the console's implicit scope is
# already the player's empire, so `event overmind.201` is sufficient and is
# the safest form to type.
# ---------------------------------------------------------------------------
PLAYER_EVENT_IDS: dict[str, int] = {
    "EXPAND": 201,
    "BUILD_FLEET": 202,
    "IMPROVE_ECONOMY": 203,
    "FOCUS_TECH": 204,
    "DIPLOMACY": 205,
    "PREPARE_WAR": 206,
    "DEFEND": 207,
    "CONSOLIDATE": 208,
    "COLONIZE": 209,
    "BUILD_STARBASE": 210,
    "ESPIONAGE": 211,
}
_PLAYER_ACTION_BY_EVENT_ID = {
    event_id: action for action, event_id in PLAYER_EVENT_IDS.items()
}
_PLAYER_EVENT_COMMAND_PATTERN = re.compile(
    r"event overmind\.(\d+)(?: ([1-9]\d*))?\Z"
)


def build_ai_event_command(country_id: int, action: str) -> str:
    """Build the only console command accepted for an AI directive."""
    if isinstance(country_id, bool) or country_id <= 0:
        raise ValueError("AI directive country ID must be a positive integer")
    try:
        event_id = AI_EVENT_IDS[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported AI directive action: {action}") from exc
    return f"event overmind.{event_id} {country_id}"


def build_player_event_command(action: str, country_id: int | None = None) -> str:
    """Build the console command for a player-empire directive.

    The country ID is optional: when omitted the console's implicit scope (the
    player's own empire) is used, which avoids depending on the numeric ID.
    """
    try:
        event_id = PLAYER_EVENT_IDS[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported player directive action: {action}") from exc
    if country_id is None:
        return f"event overmind.{event_id}"
    if isinstance(country_id, bool) or country_id <= 0:
        raise ValueError("Player directive country ID must be a positive integer")
    return f"event overmind.{event_id} {country_id}"


def is_ai_event_command(command: str) -> bool:
    """Return whether *command* is an allowlisted, targeted Overmind event."""
    match = _AI_EVENT_COMMAND_PATTERN.fullmatch(command)
    return match is not None and int(match.group(1)) in _AI_ACTION_BY_EVENT_ID


def is_player_event_command(command: str) -> bool:
    """Return whether *command* is an allowlisted player-family event.

    Strictly rejects AI-family event IDs so the two transports never
    cross-validate: only overmind.201-211 are accepted here.
    """
    match = _PLAYER_EVENT_COMMAND_PATTERN.fullmatch(command)
    if match is None:
        return False
    return int(match.group(1)) in _PLAYER_ACTION_BY_EVENT_ID


def _sanitize_snapshot_fow(data: dict) -> dict:
    """Strip fields from a legacy JSON snapshot that could violate fog-of-war.

    The autosave path (SaveBridge) has proper intel-level filtering, but
    the legacy JSON bridge trusts the Clausewitz exporter entirely.
    This adds defense-in-depth by warning on suspicious fields.
    """
    empires = data.get("known_empires", [])
    if not isinstance(empires, list):
        return data

    for empire in empires:
        if not isinstance(empire, dict):
            continue
        intel = empire.get("intel_level", "none")
        if intel in ("none", "low"):
            # At low/no intel, strip fields that require higher intel
            stripped = [k for k in empire if k in _FOW_SUSPECT_FIELDS]
            for k in stripped:
                del empire[k]
            if stripped:
                log.warning(
                    "FoW sanitize: stripped %s from empire '%s' (intel=%s)",
                    stripped, empire.get("name", "?"), intel,
                )
    return data


# ======================================================================== #
# Config
# ======================================================================== #

@dataclass
class BridgeConfig:
    """Paths and timing for the bridge."""

    # Autosave mode (Mode A)
    save_dir: Path = Path("")             # e.g. .../Paradox Interactive/Stellaris/save games
    player_name: str = ""                 # auto-detected if empty

    # Directive output (both modes)
    bridge_dir: Path = Path("mod/ai_bridge")
    directive_file: str = "directive.json"
    ack_file: str = "ack.json"
    command_dir: Path | None = None

    # Legacy JSON bridge (Mode B)
    snapshot_file: str = "state_snapshot.json"

    # Timing
    poll_interval_s: float = 2.0

    @property
    def mode(self) -> str:
        """Return 'autosave' if save_dir is configured, else 'json'."""
        if self.save_dir and self.save_dir.exists():
            return "autosave"
        return "json"


# ======================================================================== #
# Mode A — Autosave Bridge (recommended)
# ======================================================================== #

class SaveBridge:
    """Watches autosave directory and parses ``.sav`` files directly.

    This eliminates the need for a Clausewitz state exporter mod.
    """

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        # Lazy import to avoid circular deps
        from engine.save_reader import SaveReader, SaveWatcherConfig

        self._reader = SaveReader(SaveWatcherConfig(
            save_dir=config.save_dir,
            poll_interval_s=config.poll_interval_s,
            player_name=config.player_name,
        ))

    def has_new_snapshot(self) -> bool:
        return self._reader.has_new_save()

    def read_snapshot(self) -> dict | None:
        return self._reader.read_state()


# ======================================================================== #
# Mode B — Legacy JSON File Bridge
# ======================================================================== #

class BridgeReader:
    """Watches for JSON state snapshots written by a Clausewitz mod."""

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._snapshot_path = config.bridge_dir / config.snapshot_file
        self._last_mtime: float = 0.0

    def has_new_snapshot(self) -> bool:
        if not self._snapshot_path.exists():
            return False
        mtime = os.path.getmtime(self._snapshot_path)
        return mtime > self._last_mtime

    def read_snapshot(self) -> dict | None:
        if not self.has_new_snapshot():
            return None
        try:
            raw = self._snapshot_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._last_mtime = os.path.getmtime(self._snapshot_path)
            log.info(
                "Read JSON snapshot: year=%s month=%s event=%s",
                data.get("year"), data.get("month"), data.get("event"),
            )
            return _sanitize_snapshot_fow(data)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read snapshot: %s", exc)
            return None

    def read_ack(self) -> dict | None:
        ack_path = self._config.bridge_dir / self._config.ack_file
        if not ack_path.exists():
            return None
        try:
            raw = ack_path.read_text(encoding="utf-8")
            return json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("Failed to read ack file: %s", exc)
            return None


# ======================================================================== #
# Unified Reader — picks the right mode automatically
# ======================================================================== #

class UnifiedBridge:
    """Automatically selects autosave or JSON mode based on config."""

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._mode = config.mode

        if self._mode == "autosave":
            self._reader = SaveBridge(config)
            log.info("Bridge mode: AUTOSAVE (watching %s)", config.save_dir)
        else:
            self._reader = BridgeReader(config)
            log.info("Bridge mode: JSON (watching %s)", config.bridge_dir / config.snapshot_file)

    @property
    def mode(self) -> str:
        return self._mode

    def has_new_snapshot(self) -> bool:
        return self._reader.has_new_snapshot()

    def read_snapshot(self) -> dict | None:
        return self._reader.read_snapshot()

    def read_ack(self) -> dict | None:
        if isinstance(self._reader, BridgeReader):
            return self._reader.read_ack()
        return None  # autosave mode doesn't have ack files


# ======================================================================== #
# Directive Writer (shared by both modes)
# ======================================================================== #

class BridgeWriter:
    """Writes directives for the Clausewitz mod to consume."""

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._directive_path = config.bridge_dir / config.directive_file

    def write_directive(self, directive: dict) -> None:
        self._config.bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._directive_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(directive, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self._directive_path)
        log.info(
            "Wrote directive: action=%s target=%s",
            directive.get("action"), directive.get("target"),
        )

    def clear_directive(self) -> None:
        """Remove the directive file after the mod acknowledges execution."""
        if self._directive_path.exists():
            self._directive_path.unlink()

    def write_suggestion(self, directive: dict, stellaris_dir: Path | None = None) -> None:
        """Write a human-readable suggestion file for player mode.

        Instead of executing actions, this shows the player what the
        LLM recommends they should do next.
        """
        _SUGGESTION_TIPS = {
            "EXPAND": [
                "Build a starbase outpost in a nearby unclaimed system",
                "Prioritize chokepoint systems for defense",
                "Claim systems with rare resources first",
            ],
            "BUILD_FLEET": [
                "Queue corvettes/destroyers at your shipyard",
                "Use autocannon + plasma loadout (4.3 meta)",
                "Fill your naval cap before upgrading ship designs",
            ],
            "IMPROVE_ECONOMY": [
                "Build mining districts on mineral-rich planets",
                "Build alloy foundries (alloys > consumer goods early)",
                "Specialize planets with designations",
            ],
            "FOCUS_TECH": [
                "Build research labs on your capital",
                "Set Academic Privilege living standard",
                "Prioritize alloy/mineral tech for early economy",
            ],
            "DIPLOMACY": [
                "Send an envoy to improve relations with neighbors",
                "Consider a research agreement or commercial pact",
                "Set diplomatic stance to Cooperative",
            ],
            "PREPARE_WAR": [
                "Move fleets to the border with your target",
                "Stock up on alloys (aim for 1000+)",
                "Set economic policy to Militarist",
                "Claim target systems before declaring war",
            ],
            "DEFEND": [
                "Consolidate fleets at chokepoint starbases",
                "Upgrade starbases with gun batteries + hangars",
                "Build defense platforms at key starbases",
            ],
            "CONSOLIDATE": [
                "Fix any energy/food deficits first",
                "Build amenity buildings on low-stability planets",
                "Pause expansion until economy stabilizes",
            ],
            "COLONIZE": [
                "Build a colony ship and send to the best available world",
                "Prioritize size 20+ planets with good habitability",
                "Void Dwellers: build habitats instead",
            ],
            "BUILD_STARBASE": [
                "Upgrade a starbase at a chokepoint to starhold/fortress",
                "Add anchorages for naval cap, or trade hubs for energy",
                "Build shipyard modules for fleet production",
            ],
            "ESPIONAGE": [
                "Build a spy network in a rival empire",
                "Assign a high-level operative as spymaster",
                "Gather intel before declaring war",
            ],
        }

        action = directive.get("action", "CONSOLIDATE")
        reason = directive.get("reason", "")
        timestamp = directive.get("timestamp", "")
        tips = _SUGGESTION_TIPS.get(action, ["Follow the LLM's recommendation"])

        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            f"║  OVERMIND SUGGESTS: {action:<38}║",
            "╠══════════════════════════════════════════════════════════╣",
            f"║  Year: {timestamp:<50}║",
            "║                                                          ║",
            f"║  Reason: {reason[:48]:<48}  ║",
        ]
        if len(reason) > 48:
            lines.append(f"║          {reason[48:96]:<48}  ║")

        lines.append("║                                                          ║")
        lines.append("║  What to do:                                              ║")
        for tip in tips:
            lines.append(f"║   • {tip:<53}║")
        lines.extend([
            "║                                                          ║",
            "╚══════════════════════════════════════════════════════════╝",
        ])

        suggestion_text = "\n".join(lines)

        # Print to stdout for immediate visibility
        print("\n" + suggestion_text + "\n")

        # Also save to file
        if stellaris_dir and stellaris_dir.exists():
            path = stellaris_dir / "overmind_suggestion.txt"
        else:
            path = self._config.bridge_dir / "overmind_suggestion.txt"

        path.write_text(suggestion_text, encoding="utf-8")
        log.info("Suggestion: %s — %s", action, reason[:80])

    def write_directive_for(self, country_id: int, directive: dict) -> None:
        """Write a directive for a specific AI empire.

        Creates a JSON audit record and, when configured, an allowlisted console
        event command. The injector consumes the command only after atomic rename.
        """
        action = directive.get("action")
        if not isinstance(action, str):
            raise ValueError("AI directive action must be a string")
        command = build_ai_event_command(country_id, action)
        self._config.bridge_dir.mkdir(parents=True, exist_ok=True)
        path = self._config.bridge_dir / f"directive_{country_id}.json"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(directive, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        command_dir = self._config.command_dir
        if command_dir is None:
            return
        command_dir.mkdir(parents=True, exist_ok=True)
        command_path = command_dir / f"overmind_directive_{country_id}.command"
        command_tmp_path = command_path.with_suffix(".tmp")
        command_tmp_path.write_text(command + "\n", encoding="utf-8")
        command_tmp_path.replace(command_path)
        log.debug(
            "Wrote directive for country %d: %s",
            country_id, command,
        )

    def write_player_directive(
        self, directive: dict, country_id: int | None = None
    ) -> None:
        """Write a directive for the human player's own empire.

        Same audit-record + allowlisted-command contract as
        :meth:`write_directive_for`, but emits the player event family
        (overmind.201-211, trigger ``is_ai = no``).

        ``country_id`` is optional — the console's implicit scope is already the
        player's empire, so omitting it produces the safest command to type
        (``event overmind.203``).  Pass it only when the numeric ID is known and
        you specifically want to target it.
        """
        action = directive.get("action")
        if not isinstance(action, str):
            raise ValueError("Player directive action must be a string")
        command = build_player_event_command(action, country_id)

        self._config.bridge_dir.mkdir(parents=True, exist_ok=True)
        label = "player" if country_id is None else f"player_{country_id}"
        path = self._config.bridge_dir / f"directive_{label}.json"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(directive, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)

        command_dir = self._config.command_dir
        if command_dir is None:
            return
        command_dir.mkdir(parents=True, exist_ok=True)
        command_path = command_dir / f"overmind_{label}.command"
        command_tmp_path = command_path.with_suffix(".tmp")
        command_tmp_path.write_text(command + "\n", encoding="utf-8")
        command_tmp_path.replace(command_path)
        log.debug("Wrote player directive: %s", command)
