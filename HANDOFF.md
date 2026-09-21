# Stellaris Overmind — 交接文档

> ⚠️ **本文档已部分过时（2026-09-15 标注）**
>
> 第二节描述的**「玩家事件族 + `run` 命令 + 控制台注入」通道已经整体退役**，
> 4 个相关脚本文件已归档到 `_legacy/m0_directive_channel/`（退役理由见该目录 README）。
> 退役原因：控制台通道与"边玩边用电脑"根本冲突，且事件族受制于
> Clausewitz 无法读运行时文件。
>
> **现行架构 = 大模型写「法案（Lex）」+ 游戏内自治执行，全程零控制台。**
> 现行文档请以这三份为准：
> - `docs/PROJECT_PLAN.md` —— 项目计划书（含里程碑与验收）
> - `docs/ARCHITECTURE.md` —— 架构与 ADR
> - `docs/BACKLOG.md` —— 任务看板与**实测基线**（当前进度看这份）
>
> 本文保留作历史记录：第一、三节的**需求与硬限制结论依然有效**。

> 更新：2026-09-14
> 目标：**接管玩家自己的帝国**（单机）
> 约束：**单机模式**（不走联机协议注入）

---

## 一、需求变更（最重要）

**接管对象已从 AI 帝国切换到玩家帝国。** 完整需求见 `REQUIREMENTS.md`。

`config.toml` 的 `[target] mode` 已从 `"ai"` 改为 `"player"`。

---

## 二、本轮完成的改造

### 2.1 玩家事件族（核心修复）

原 mod 的 `overmind.101`–`111` 事件 trigger 写死 `is_ai = yes` → **玩家帝国永不触发**。
新增平行的玩家族：

| 事件 | 动作 | 事件 | 动作 |
|---|---|---|---|
| `.201` | EXPAND | `.207` | DEFEND |
| `.202` | BUILD_FLEET | `.208` | CONSOLIDATE |
| `.203` | IMPROVE_ECONOMY | `.209` | COLONIZE |
| `.204` | FOCUS_TECH | `.210` | BUILD_STARBASE |
| `.205` | DIPLOMACY | `.211` | ESPIONAGE |
| `.206` | PREPARE_WAR | | |

trigger 为 `is_ai = no`。心跳事件 `overmind.200` 按 `is_ai` 自动分派两族；
旧的 `.100` 保留向后兼容。

### 2.2 玩家行动效果（含真实结构操作）

`common/scripted_effects/overmind_effects.txt` 新增 11 个 `overmind_player_action_*`。
与 AI 族（只加限时修正）不同，玩家族做**真实操作**：

- `overmind_player_enable_automation` — `set_colony_type = col_capital`，
  把核心星球交给**游戏原生 colony_automation** 自动建设（单机、零注入）
- DEFEND — `every_owned_fleet { set_fleet_stance = evasive }` 真改舰队姿态
- DIPLOMACY — `add_opinion_modifier` 给非交战国加好感
- PREPARE_WAR / CONSOLIDATE / BUILD_FLEET — 真改 `economic_policy` /
  `war_philosophy` / `diplomatic_stance`

### 2.3 引擎侧

- `engine/bridge.py`：新增 `PLAYER_EVENT_IDS`、`build_player_event_command()`、
  `is_player_event_command()`、`write_player_directive()`
- `engine/game_loop.py`：`_emit_directive()` 自动 stage 玩家事件命令
- 两个事件族**严格互不通过校验**（AI validator 拒 2xx，player validator 拒 1xx）

---

## 三、⚠️ 重大发现：4.4.6 没有通用工业区划

**本轮最有价值的发现，修掉了一个静默 bug。**

4.x 经济重做删掉了 `district_industrial`，改用 **zone** 体系：

```
add_zone = { district = <宿主区划> zone = <zone> zone_slot = N }
```

宿主默认 `district_city`。zone 有 `zone_foundry` / `zone_factory` / `zone_industrial`
/ `zone_research` / `zone_trade` 等。

**旧编译器的 bug**：模糊匹配把 `industrial` 解析成 `district_ark_military_industrial`
—— **游牧方舟专用**区划，在普通星球上必然无效，而且**不报错**。

已修：
- 编译器新增 `zone` 动作类型（共 12 类）
- `_resolve()` **排除变体 ID**（`_ark_` / `_ring_world_` / `_hab_` / `_hive_` /
  `_nexus_` / `_arcology_` / `_machine_` / `_resort_` / `_rw_` 等）
- `industrial` 区划请求**明确报错**，不再给错答案
- 新增 `audit_aliases()` — **121 个别名全部对照游戏文件验证**，有一个失效就测试失败

---

## 四、名字校验：grep 游戏文件，不靠猜

**确认存在**：`set_colony_type`(6 处) / `is_colony`(61) / `set_fleet_stance`(99) /
`every_owned_fleet`(14) / `every_owned_planet`(20) / `add_opinion_modifier`(27) /
`is_same_value`(177) / `every_country` / `add_zone` / `col_capital` 等

**猜错并已改掉**：

| 错误写法 | 正解 |
|---|---|
| `set_research_focus` | **不存在**。4.4.6 无科研目标脚本接口，只能用 `run` + `research_technology` |
| `every_neighbor_country` | 只有 `any_neighbor_country` trigger；effect 用 `every_country` + limit |
| `improve_relations` | 是 envoy 任务名。正确是 `add_opinion_modifier` |
| `is_fleet_in_use` | **不存在**。舰队筛选用 `is_mobile` |
| `zone_commercial` | **不存在**。正确是 `zone_trade` |

---

## 五、测试

**522 全绿**（原 485 通过 + 1 失败）

新增：
- `tests/test_action_compiler.py` — 21 个用例。**此前编译器完全没有测试**，
  所以那些 bug 才能存活。覆盖幻觉 ID 拦截、变体陷阱、zone 语法、别名审计
- `TestPlayerDirectiveTransport`（`tests/test_bridge.py`）— 16 个断言点，
  含"两族互不通过校验"

顺手修掉 `test_latencies_recorded` 的既有失败（stub provider 瞬时返回，
耗时合法四舍五入为 0.0，原断言 `> 0` 过严）。

---

## 六、待办

### 6.1 最高优先：实机验证 `run` 通道（唯一没验证的地基）

```bash
py -3.12 scripts/verify_run_channel.py --prepare   # 已执行，载荷已就位
py -3.12 scripts/verify_run_channel.py --fire      # 游戏启动后执行
py -3.12 scripts/verify_run_channel.py --check     # 存盘后执行
```

流程：`--fire` 敲命令 → 游戏走一个月并存档 → `--check` 扫存档找标记 flag
（`overmind_run_channel_ok`）。

### 6.2 事件层改动必须重启游戏

Clausewitz **不热重载** mod 脚本。改完事件/效果后必须**完全退出游戏再进**。

### 6.3 其他

- `action_compiler` 补传统树 / 飞升 perk / 法令 / 外交动作
- 一键暂停开关（热键或开关文件）
- 引擎主循环的选择性开关（现在每轮都会 stage 命令）

---

## 七、关键架构判断（别搞混）

**两条腿分工：**

| 层 | 通道 | 职责 |
|---|---|---|
| **结构姿态层** | 事件（`event overmind.2NN`） | "把核心星球交给自动化""舰队改规避姿态"——不需要逐 id 的结构决策 |
| **硬执行层** | `run overmind_run.txt` | 逐星球精确操作（建哪个建筑、上哪个科技）——Python 编译 + ID 校验 + 精确寻址 |

**不要试图把事件层做成硬执行器。** `run` 的 console scope 天然就是玩家帝国，
且能寻址具体星球 scope，这才是硬控制的正确通道。事件层只能操作当前 scope
（国家）及其子 scope，做不了精确的跨星球批量建造。

**已排除的方案（别再花时间）：**
- 内存读写 / DLL 注入 — 版本强耦合、风险高
- 多人 UDP session_proxy（参考仓库 IAG 的做法）— 需联机，用户要单机
- 替换 C++ 决策层 — 决策在 C++ 里，脚本无 hook；`human_ai` 只是交给游戏原生 AI

---

## 八、环境

| 项 | 值 |
|---|---|
| 项目 | `C:/Users/<user>/.zcode/workspace/default/Stellaris_Overmind` |
| 游戏 | `D:/SteamLibrary/steamapps/common/Stellaris` (4.4.6) |
| 存档 | `C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games/<id>/` |
| 模型 | `D:\Ollama\models`（`C:\Users\<user>\.ollama\models` 已 junction） |
| 引擎模型 | `qwen2.5:7b`（14B 占 11.2GB 显存，与游戏同开必 OOM；3B/14B 已导入备用） |
| 注入器 | `scripts/auto_execute.py`（事件）/ `scripts/overmind_run.py`（run 文件） |
| 启动 | `start_overmind.bat`（会先杀 GUI ollama 再起 serve） |

**游戏启动**：桌面 `Stellaris.url` → `steam://rungameid/281990`

---

## 九、历史遗留问题（已解决，存档备查）

- 旧档 2242 年 gamestate 解压后 1.7GB，纯 Python 解析器吃不下
  → 已加保护优雅跳过（`clausewitz_parser.py` 大存档保护 + 128MB 栈线程）
- `OLLAMA_MODELS` 环境变量不被识别 → junction 到 D 盘
- 智谱 GLM 404 → `qwen_provider.py` 加 `/v1` → 无 `/v1` 回退
- mod 本地化显示原始 key → 补 UTF-8 BOM
- 人格接管层失效（鸡生蛋：flag 需开局后设，但人格开局就定死；
  且 4.4.6 无 `set_personality` effect）→ 不再依赖人格层
- 政策键名全错：`economic_policy_militarist`→`_military`、
  `diplomatic_stance_cooperative`→`diplo_stance_cooperative`、
  `war_philosophy_unrestricted`→`unrestricted_wars`
- `living_standard` 在 4.x 不是政策 → 改用 `set_living_standard`
- 无效修正键：`country_outpost_influence_cost_mult` 删除、
  `pop_growth_speed`→`bonus_pop_growth_mult`
- `config.toml` 缺 `command_dir` → 引擎压根不生成 `.command` 文件
