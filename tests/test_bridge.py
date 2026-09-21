"""Tests for bridge — Stellaris 4.4.6."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from engine.bridge import (
    AI_EVENT_IDS,
    PLAYER_EVENT_IDS,
    BridgeConfig,
    BridgeReader,
    BridgeWriter,
    UnifiedBridge,
    build_ai_event_command,
    build_player_event_command,
    is_ai_event_command,
    is_player_event_command,
)


@pytest.fixture
def bridge_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ai_bridge"
    d.mkdir()
    return d


@pytest.fixture
def bridge_config(bridge_dir: Path) -> BridgeConfig:
    return BridgeConfig(bridge_dir=bridge_dir, save_dir=Path(""))


class TestBridgeWriter:

    def test_does_not_expose_direct_console_execution(self) -> None:
        assert not hasattr(BridgeWriter, "write_console_commands")

    def test_write_directive(self, bridge_config: BridgeConfig) -> None:
        writer = BridgeWriter(bridge_config)
        writer.write_directive({"action": "EXPAND", "target": "Sol"})
        path = bridge_config.bridge_dir / "directive.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["action"] == "EXPAND"

    def test_write_is_atomic(self, bridge_config: BridgeConfig) -> None:
        """No .tmp file should remain after write."""
        writer = BridgeWriter(bridge_config)
        writer.write_directive({"action": "DEFEND"})
        tmp = bridge_config.bridge_dir / "directive.tmp"
        assert not tmp.exists()

    def test_clear_directive(self, bridge_config: BridgeConfig) -> None:
        writer = BridgeWriter(bridge_config)
        writer.write_directive({"action": "EXPAND"})
        writer.clear_directive()
        assert not (bridge_config.bridge_dir / "directive.json").exists()

    def test_clear_nonexistent_ok(self, bridge_config: BridgeConfig) -> None:
        writer = BridgeWriter(bridge_config)
        writer.clear_directive()  # should not raise

    def test_writes_targeted_ai_event_command(self, bridge_config: BridgeConfig) -> None:
        command_dir = bridge_config.bridge_dir / "commands"
        bridge_config.command_dir = command_dir
        writer = BridgeWriter(bridge_config)

        writer.write_directive_for(42, {"action": "EXPAND"})

        command_path = command_dir / "overmind_directive_42.command"
        assert command_path.read_text(encoding="utf-8") == "event overmind.101 42\n"
        assert not command_path.with_suffix(".tmp").exists()

    def test_rejects_invalid_ai_event_command(self, bridge_config: BridgeConfig) -> None:
        writer = BridgeWriter(bridge_config)

        with pytest.raises(ValueError, match="Unsupported AI directive action"):
            writer.write_directive_for(42, {"action": "INVALID"})

        assert not (bridge_config.bridge_dir / "directive_42.json").exists()


class TestConsoleChannelRetired:
    """The console-driven directive channel was retired in M1.

    M0 pushed decisions into a *running* game by asking the engine to write
    ``event overmind.1NN`` / ``event overmind.2NN`` console commands, which
    ``scripts/auto_execute.py`` then typed into the game.  Two things killed
    that design:

    * requirement **G-2** (console-free).  Using the console once permanently
      voids achievement eligibility, and it needs the user's keyboard.
    * the events those commands target no longer exist, so injecting them would
      only produce ``unknown event`` noise in ``error.log``.

    M1 replaces the whole channel with the **Lex**: a law compiled into script
    before launch, executed in-game by the autonomy layer.  These tests freeze
    that decision so the console path cannot quietly come back — a regression
    here would silently reintroduce both the console dependency and the log
    errors it produces.
    """

    MOD = Path(__file__).parent.parent / "mod" / "stellaris_overmind"
    LEGACY = Path(__file__).parent.parent / "_legacy" / "m0_directive_channel"

    @pytest.mark.parametrize("rel", [
        "events/overmind_events.txt",
        "common/scripted_effects/overmind_effects.txt",
        "common/personalities/overmind_personality.txt",
        "common/static_modifiers/overmind_modifiers.txt",
    ])
    def test_legacy_files_no_longer_in_mod(self, rel: str) -> None:
        assert not (self.MOD / rel).exists(), (
            f"{rel} 仍在 mod 载入路径中；它会在载入时产生 scope 错误"
        )

    @pytest.mark.parametrize("rel", [
        "overmind_events.txt",
        "overmind_effects.txt",
        "overmind_personality.txt",
        "overmind_modifiers.txt",
    ])
    def test_legacy_files_preserved_for_reference(self, rel: str) -> None:
        """Retired, not lost — the M0 work is archived with its rationale."""
        assert (self.LEGACY / rel).exists()

    def test_autonomy_events_are_the_only_event_family(self) -> None:
        names = sorted(p.name for p in (self.MOD / "events").glob("*.txt"))
        assert names == ["overmind_autonomy_events.txt"]

    def test_on_actions_only_hooks_autonomy_events(self) -> None:
        content = (
            self.MOD / "common/on_actions/overmind_on_actions.txt"
        ).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in content.splitlines() if not line.lstrip().startswith("#")
        )
        hooked = set(re.findall(r"overmind\.(\d+)", body))
        # 303 = M1-9 验收加速器。它挂在月度脉冲上，但只在存档里存在
        # om_verify_boost 时才动作，正式游戏里是死代码（见
        # scripted_effects/overmind_verify_boost.txt 开头的说明）。
        # 304 = D-17/F1 归因守卫（2026-09-15）：human_ai 接管告警。
        # 它是本族唯一不带 is_ai 门的事件，专门在心跳被门挡住时说话。
        assert hooked == {"300", "301", "302", "303", "304", "310"}, (
            f"on_actions 挂载的事件集变了: {sorted(hooked)}"
        )

    def test_no_mod_file_declares_an_allowlisted_console_event(self) -> None:
        """The retired events must not creep back into the mod."""
        retired = set(AI_EVENT_IDS.values()) | set(PLAYER_EVENT_IDS.values())
        for path in self.MOD.rglob("*.txt"):
            text = path.read_text(encoding="utf-8")
            for event_id in retired:
                assert f"id = overmind.{event_id}" not in text, (
                    f"{path.relative_to(self.MOD)} 重新声明了已退役的事件 "
                    f"overmind.{event_id}"
                )

    def test_config_disables_the_console_injector(self) -> None:
        """Without command_dir the engine writes JSON only, never a command."""
        text = (Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8")
        active = [
            line for line in text.splitlines()
            if line.strip().startswith("command_dir")
        ]
        assert not active, "config.toml 重新启用了控制台注入通道"


class TestAIEventCommands:

    def test_builds_allowlisted_command(self) -> None:
        assert build_ai_event_command(3, "ESPIONAGE") == "event overmind.111 3"

    @pytest.mark.parametrize("command", [
        "event overmind.101 3",
        "event overmind.111 42",
    ])
    def test_recognizes_allowlisted_command(self, command: str) -> None:
        assert is_ai_event_command(command)

    @pytest.mark.parametrize("command", [
        "effect add_resource = { alloys = 100 }",
        "event overmind.101 0",
        "event overmind.112 3",
        "event overmind.101 3; play 3",
    ])
    def test_rejects_non_allowlisted_command(self, command: str) -> None:
        assert not is_ai_event_command(command)


class TestPlayerDirectiveTransport:
    """Command *shape* for the player family.

    The mod-side events this family addressed were retired in M1 (see
    :class:`TestConsoleChannelRetired`), so only the pure string helpers are
    still exercised here: they are the allow-list that used to keep an
    arbitrary console string from ever being written.
    """

    def test_player_command_omits_country_by_default(self) -> None:
        assert build_player_event_command("IMPROVE_ECONOMY") == "event overmind.203"

    def test_player_command_accepts_optional_country(self) -> None:
        assert build_player_event_command("IMPROVE_ECONOMY", 5) == "event overmind.203 5"

    def test_rejects_unknown_player_action(self) -> None:
        with pytest.raises(ValueError):
            build_player_event_command("NOT_AN_ACTION")

    @pytest.mark.parametrize("command", [
        "event overmind.201",
        "event overmind.203",
        "event overmind.211 7",
    ])
    def test_recognizes_player_command(self, command: str) -> None:
        assert is_player_event_command(command)

    @pytest.mark.parametrize("command", [
        "event overmind.101",          # AI id without a country is invalid
        "event overmind.100",
        "event overmind.212",
        "event overmind.103 5; play 5",
        "effect add_resource = { alloys = 100 }",
    ])
    def test_rejects_non_allowlisted_player_command(self, command: str) -> None:
        assert not is_player_event_command(command)

    def test_families_do_not_overlap(self) -> None:
        """A command valid for one family must be invalid for the other."""
        assert not is_ai_event_command("event overmind.203")
        assert not is_player_event_command("event overmind.101 3")


class TestBridgeReader:

    def test_no_snapshot_returns_none(self, bridge_config: BridgeConfig) -> None:
        reader = BridgeReader(bridge_config)
        assert reader.read_snapshot() is None

    def test_read_snapshot(self, bridge_config: BridgeConfig) -> None:
        snap_path = bridge_config.bridge_dir / "state_snapshot.json"
        snap_path.write_text(json.dumps({"year": 2230, "month": 6}))
        reader = BridgeReader(bridge_config)
        data = reader.read_snapshot()
        assert data is not None
        assert data["year"] == 2230

    def test_no_double_read(self, bridge_config: BridgeConfig) -> None:
        snap_path = bridge_config.bridge_dir / "state_snapshot.json"
        snap_path.write_text(json.dumps({"year": 2230}))
        reader = BridgeReader(bridge_config)
        assert reader.read_snapshot() is not None
        assert reader.read_snapshot() is None  # same file, not re-read

    def test_read_ack(self, bridge_config: BridgeConfig) -> None:
        ack_path = bridge_config.bridge_dir / "ack.json"
        ack_path.write_text(json.dumps({"status": "ok"}))
        reader = BridgeReader(bridge_config)
        ack = reader.read_ack()
        assert ack is not None
        assert ack["status"] == "ok"

    def test_corrupt_json_returns_none(self, bridge_config: BridgeConfig) -> None:
        snap_path = bridge_config.bridge_dir / "state_snapshot.json"
        snap_path.write_text("{invalid json")
        reader = BridgeReader(bridge_config)
        assert reader.read_snapshot() is None


class TestUnifiedBridge:

    def test_json_mode_when_no_save_dir(self, bridge_config: BridgeConfig) -> None:
        config = BridgeConfig(
            save_dir=Path("/nonexistent_path_xyz"),
            bridge_dir=bridge_config.bridge_dir,
        )
        bridge = UnifiedBridge(config)
        assert bridge.mode == "json"

    def test_autosave_mode_when_save_dir_exists(self, tmp_path: Path) -> None:
        save_dir = tmp_path / "save games"
        save_dir.mkdir()
        config = BridgeConfig(save_dir=save_dir, bridge_dir=tmp_path / "bridge")
        bridge = UnifiedBridge(config)
        assert bridge.mode == "autosave"
