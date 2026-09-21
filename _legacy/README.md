# M0 指令通道（已退役）

归档时间：2026-09-15（M1 执行层交付时）
退役理由：**已被 M1 自治层（Lex 机制）整体取代**，且是 `error.log` 真实报错的来源。

---

## 一、这四个文件原本做什么

M0 的设计是"两条腿"：

| 层 | 通道 | 职责 |
|---|---|---|
| 结构姿态层 | 事件 `event overmind.1NN/2NN` | 把核心星球交给自动化、改舰队姿态 |
| 硬执行层 | `run overmind_run.txt` | 逐星球精确建造 |

两条腿**都必须用控制台**：事件不会自己触发，得由 `scripts/auto_execute.py`
用键盘模拟把 `event overmind.203` 敲进游戏控制台；`run` 更是直接的
控制台命令文件。

| 文件 | 内容 |
|---|---|
| `overmind_events.txt` | 事件族：`overmind.1`/`.2`（开局初始化）、`.100`/`.200`（月度指令读取）、`.101-111`（AI 帝国族）、`.201-211`（玩家指令族） |
| `overmind_effects.txt` | 上述事件调用的 11+11 个 `overmind_action_*` / `overmind_player_action_*` 效果 |
| `overmind_personality.txt` | 4 个 `overmind_controlled*` 人格，`allow` 里要求 `has_country_flag = overmind_active` |
| `overmind_modifiers.txt` | 11 个 `overmind_*_focus` 静态修正，只被 `overmind_effects.txt` 使用 |

---

## 二、为什么必须退役（三条独立理由，任一条都足够）

### 1. 与控制台需求直接冲突（G-2）

用户明确要求"脱离控制台，AI 自主游玩"。控制台一旦用过一次，本局**永久**
失去成就资格（约束 C2）。而这条通道离开了控制台就是死的：
`overmind.100`/`.200` 的触发器要求 `has_country_flag = overmind_directive_ready`，
这个 flag 只有控制台注入的事件才会设置。

**实测确证**：载入存档后这条通道不会产生任何行为，只在载入时产生报错。

### 2. 它是 `error.log` 报错的来源（AC-3 阻塞项）

修好 on_action 作用域问题（23:34）之后，`error.log` 里属于 mod 的报错**全部**
来自这四个文件（22:47 那一轮日志，55 行 overmind 相关）：

```
metascript.cpp:218  Invalid macro entry in overmind_action_consolidate: VERMIND   ×6
trigger.cpp:635     Wrong scope for trigger 'has_planet_flag'    @ overmind_effects.txt:446
trigger.cpp:580     Wrong scope for trigger 'has_planet_flag'    @ overmind_effects.txt:446
effect.cpp:919      Wrong scope for effect 'set_living_standard' @ overmind_effects.txt:251
effect.cpp:73       CRITICAL: Max effects post init recursive depth of 5 reached
                    @ overmind_effects.txt:181 @ overmind_action_expand
```

前两类是**载入期校验**（`trigger.cpp`/`effect.cpp` 在解析脚本时就跑），
所以**每次载入必现**，与事件是否真的触发无关。只有把这四个文件移出载入路径
才能清空。

### 3. 命名空间污染与死代码

- `overmind_personality.txt`：`allow` 要求 `overmind_active`，但 M1 自治层
  **从不设置**这个 flag（它设的是 `overmind_autonomy`）。而且 4.4.6 没有
  `set_personality` effect，人格只在开局定死——即使是 M0 也从未真正生效。
- `overmind_modifiers.txt`：11 个静态修正只被 `overmind_effects.txt` 引用，
  没有第二个消费者。
- `events/` 目录里留着两个 `namespace = overmind` 的文件，任何覆盖式 mod
  冲突面翻倍。

---

## 三、M1 用什么取代了它

**一句话**：M0 是"运行时外部注入"，M1 是"载入期编译注入"。

```
M0:  引擎写 directive.json ──▶ auto_execute.py 敲控制台 ──▶ event overmind.2NN
                                                                    ↑ 需要控制台

M1:  引擎读档 → 大模型 → 编译法案(Lex) ──▶ 脚本文件 ──▶ 载入即生效
                                                                    ↑ 零控制台
```

对 M0 的两项职责，M1 都有对应物：

| M0 职责 | M1 取代物 |
|---|---|
| 结构姿态（舰队姿态/经济政策/殖民地自动化） | `overmind_autonomy_posture` + 相位 5/6 |
| 硬执行（逐星球建造） | 相位 1-4：`add_district` / `add_zone` / `add_building` + **自建费用模型**（`overmind_charges.txt`，造价取自游戏数据） |
| 指令事件族 | `overmind.300` 月度心跳 + 编译进脚本的 8 格日程表 |
| 人格层 | 不需要——4.4.6 无 `set_personality`，且政策/姿态已由脚本直控 |

`overmind_personality.txt` 里那套 4.3 舰船 meta（动能偏好、0.3/0.5/0.2 装甲盾构比）
如果 M3 做舰船设计时仍有用，可从这里取回。

---

## 四、注意：引擎侧的遗留助手

`engine/bridge.py` 里仍保留 `AI_EVENT_IDS` / `PLAYER_EVENT_IDS` 与
`build_*_event_command` / `is_*_event_command`，以及
`scripts/auto_execute.py`、`scripts/overmind_run.py`。

它们**不会**产生行为，因为：

1. 目标事件已不存在——注入了也只会在 `error.log` 里报 `unknown event`；
2. `config.toml` 的 `command_dir` 已注释掉，`write_directive_for` /
   `write_player_directive` 在 `command_dir is None` 时**只写 JSON、不写命令**。

保留而非删除的理由：它们是白名单式字符串助手，有完整单测，删掉的收益
小于改 `game_loop` 的回归风险。真要继续试验控制台通道时，取消
`config.toml` 里 `command_dir` 的注释即可（但必须先恢复事件族）。

守护测试见 `tests/test_bridge.py::TestConsoleChannelRetired`。
