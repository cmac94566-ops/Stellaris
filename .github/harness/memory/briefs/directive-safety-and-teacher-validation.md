# Architecture Brief: Directive Safety and Teacher Validation

## Scope

Bridge, engine, mod events, transport automation, tests, and executor documentation. The work
removes an unused direct-execution path, applies the existing directive validator to
evaluation-derived teacher records, and adds a constrained per-country event transport.

## Impact Map

- `engine/bridge.py` (bridge): owns directive-file writing and the unsafe console generator.
- `engine/game_loop.py` (engine): writes per-country directive JSON in AI mode.
- `mod/stellaris_overmind/events/overmind_events.txt` (mod/events): handles targeted AI action
   events and applies existing native-AI modifier effects.
- `scripts/auto_execute.py` (bridge): injects only validated Overmind country-event commands.
- `scripts/collect_teacher.py` (training): owns teacher-record admission.
- `tests/test_bridge.py` and `tests/test_training_pipeline.py` (tests): cover the changed paths.
- `docs/EXECUTOR_SPEC.md` (docs): describes the execution contract.

## Decisions

1. Remove `BridgeWriter.write_console_commands`. It is unused and directly mutates game state.
2. `BridgeWriter.write_directive_for` writes the existing JSON audit record plus an atomic,
   per-country command file containing only `event overmind.<action-event> <country-id>`.
3. `auto_execute` accepts only that exact mapped event command, injects it directly, and deletes
   the command file after the console accepts it. It never executes arbitrary command files.
4. Each targeted country event verifies the country is a default AI empire, activates Overmind,
   and invokes only the existing scripted effect for the mapped action.
5. Validate evaluation-derived teacher responses with the same `validate_directive` admission gate
   used for replay-derived responses before writing JSONL records.

## Gates

| Gate | Verdict | Rationale |
| --- | --- | --- |
| Layer alignment | Pass | Bridge owns command construction, automation owns injection, and mod events own game effects. |
| Fog-of-war safety | Pass | The existing validator remains the admission boundary. |
| Version lock | Pass | No game mechanics or meta data are added. |
| Action whitelist | Pass | Teacher output is checked by the existing whitelist validator. |
| Ruleset hierarchy | Pass | Evaluation scenarios use their generated ruleset unchanged. |

## Do Not

- Do not use `play <country id>` as an injection mechanism; it takes player control from native AI.
- Do not emit console effects other than the allowlisted `overmind` country events.
- Do not re-enable direct resource, district, building, research, or fleet manipulation.

## Testing

- Bridge tests must prove only valid action/country pairs produce targeted event commands.
- Transport tests must reject arbitrary console commands.
- Mod contract tests must verify every allowed action has a targeted country event.
- Teacher collection tests must prove invalid evaluation output is skipped and valid output is kept.
- Run bridge, training, safety, and full regression harness loops after implementation.
