# Stellaris Overmind — 需求定义：接管玩家本国

> 版本 v1 · 2026-09-14 · 游戏版本 Stellaris 4.4.6 · 目标：单机

---

## 0. 一句话需求

用 AI **接管玩家自己的帝国**（不是 AI 帝国），在**单机**下对内政/科研/军事/外交做**真实硬控制**（不是加 buff 的软引导），全程**不抢键盘**、可随时暂停、每条指令可审计。

---

## 1. 目标对象：玩家帝国（不是 AI）

| 项 | 要求 | 现状 | 差距 |
|---|---|---|---|
| 接管对象 | 玩家自己的国家 | `config.target.mode = "ai"`（一轮给 22 个 AI 帝国各发一条） | ✅ 已改为 `"player"` |
| 国家识别 | 自动定位玩家 country_id | `save_reader._find_player_country()` 已能从存档 `player` 块解析 | 复用即可 |
| 事件可作用于玩家 | overmind.101–111 必须对玩家生效 | **事件 trigger 写死 `is_ai = yes` → 玩家永远不触发** | ❌ 必须改 |
| 指令频率 | 每轮 1 条（玩家只有 1 个帝国） | AI 模式每轮 22 条 | 需降频，省 token、少打扰 |
| 存档来源 | 玩家自己的存档（含 mod） | `bridge.save_dir` 已指向用户存档目录 | 满足 |

**关键结论**：现有 mod 是"AI 专用"的。事件层 `is_ai = yes` 是硬门槛，不改这行，玩家模式下所有注入都是空转。

---

## 2. 控制强度：硬控制，不要软引导

### 2.1 现状 = 软引导（用户明确不接受）
```
overmind_action_improve_economy  →  只是 add_modifier(+X% 产出) + set_country_flag
```
加完 buff 后，**建筑还是得玩家自己点、科技还是得玩家自己选**。这是"倾向性引导"，不是接管。

### 2.2 需求 = 硬控制（直接执行游戏效果）

| 优先级 | 域 | 具体动作 | 实现方式 |
|---|---|---|---|
| **P0** | 内政建造 | 建造/升级建筑 | `add_building`（planet scope） |
| **P0** | 内政建造 | 铺区划 | `add_district` |
| **P0** | 内政建造 | 清地块障碍 | `clear_blockers` |
| **P0** | 内政建造 | 星球自动化托管 | `set_colony_type`（原生 colony_automation，单机可用） |
| **P0** | 科研 | 选研究项 | `research_technology` |
| **P0** | 政策/法令 | 切换政策、生活标准 | `set_policy` / `set_living_standard` / `enact_edict` |
| P1 | 军事 | 按设计模板造舰 | `create_ship` |
| P1 | 军事 | 舰队姿态、轰炸姿态 | `set_fleet_stance` / `set_fleet_bombardment_stance` |
| P2 | 扩张 | 殖民、建/升星基地 | `colonize` / starbase 相关 effect |
| P2 | 外交 | 改善关系、结盟、宣战 | diplomatic action（**宣战需人工确认**） |
| P3 | 高级 | 传统树、飞升 perk、议程 | 相关 effect |
| P3 | 高级 | 人口迁移、市场买卖 | `resettle` / market 相关 |

### 2.3 明确排除的方案（已评估，不采用）

| 方案 | 排除原因 |
|---|---|
| 内存读写 / DLL 注入 | 版本强耦合、风险高、维护成本高 |
| 多人 UDP session proxy（参考仓库 imperial-auto-governor 的做法） | **必须联机**，用户要求单机 |
| 替换 C++ AI 决策层（`human_ai`） | 决策层是 C++ 编译进二进制的，**不可替换**；`human_ai` 只是把玩家帝国交给游戏原生 AI 托管，接不进我们的模型 |

---

## 3. 执行通道（三条，按可靠性排序）

| # | 通道 | 原理 | 状态 |
|---|---|---|---|
| 1 | **`run overmind_run.txt`**（主力） | 官方 console 命令，读取游戏目录下的脚本文件批量执行。scope 天然=玩家帝国，只需敲 20 个字符，不受控制台长度限制 | 脚本已写（`scripts/overmind_run.py`），**实机未验证** |
| 2 | **`colony_automation`**（常驻兜底） | `set_colony_type` 让游戏自己按 designation 自动建设，单机原生、零外部注入 | 游戏文件已确认存在（18 个 automation 模板），待接入 |
| 3 | **`event overmind.NNN <id>`**（保留） | 国家事件，适合带触发条件的周期动作 | 需先去掉 `is_ai = yes` 限制 |

---

## 4. 交互约束（用户已投诉过抢键盘）

- **不抢键盘**：注入前检测用户空闲 ≥3s 才执行；`--foreground-only` 模式下仅在游戏窗口有焦点时注入
- **最小击键**：整批指令只敲 `run overmind_run.txt` 一行
- **手动模式**：`--once` 跑一次就退出，完全由用户触发
- **一键暂停/恢复**（待实现）：热键或开关文件，随时中断接管

---

## 5. 决策链路

```
存档 → 解析玩家国状态 → LLM 输出结构化意图(JSON)
     → action_compiler 校验 ID → 生成 effect 脚本
     → 写入 overmind_run.txt → 游戏 run 执行 → 审计回写
```

- LLM **禁止**直接输出脚本字符串，只输出 `{type, target, id, ...}` 结构化意图（防幻觉）
- 所有 ID 对照原版游戏文件白名单校验（已扫到 450 建筑 / 147 区划 / 677 科技）
- 编译失败的动作单独记录原因，不影响其他动作执行

---

## 6. 可观测性

- 每条指令写审计：时间 / 动作类型 / 目标 / 参数 / 执行结果
- 执行前 `--dry-run` 打印将要执行的全部脚本
- 失败原因回写（例：`building_xxx 不存在于 4.4.6`）
- 用户能看到"AI 此刻在做什么、为什么这么做"

---

## 7. 安全边界

**黑名单（永不执行）**：`destroy_country` / `kill_pop` / `remove_planet` / `destroy_colony` / `add_resource` / `research_all_technologies` / `activate_all_traditions`

**白名单约束**：只允许作用于玩家自己的帝国，禁止影响未授权的第三国

---

## 8. 验收标准

- [ ] `config.target.mode = "player"` 后，引擎每轮只对玩家帝国产出 1 条指令
- [ ] 实机验证 `run overmind_run.txt` 生效（先测 1 条建造 + 1 条政策）
- [ ] 玩家全程不碰键盘，AI 能自己建起一圈建筑、切好政策
- [ ] 用户敲键盘时注入自动让路，无抢输入现象
- [ ] 每条指令有审计回执，失败有明确原因

---

## 9. 当前阻塞项（按优先级）

1. **`run` 命令未在实机验证** — 已备好验证工具
   `scripts/verify_run_channel.py`（`--prepare` / `--fire` / `--check`），
   测试载荷已写入游戏目录 `overmind_verify.txt`，等游戏启动后执行三步验证。
2. ~~**mod 事件 `is_ai = yes` 未改**~~ — ✅ **已完成**：新增玩家事件族 201–211
3. **action_compiler 覆盖面不足** — 现有 11 类（build/district/tech/policy/stance/bombard/war/colony/human_ai/blockers），缺传统树、飞升、法令、外交
4. **引擎未接 `set_colony_type`** — 事件层已接（`overmind_player_enable_automation`），编译层还没接

---

## 11. 实施进度（2026-09-14 更新）

### 已完成 ✅

| 项 | 文件 | 说明 |
|---|---|---|
| 接管对象切到玩家 | `config.toml` | `[target] mode = "ai"` → `"player"` |
| 玩家事件族 | `events/overmind_events.txt` | 新增 `overmind.201`–`211`，trigger 为 `is_ai = no` |
| 心跳事件兼容双模式 | `events/overmind_events.txt` | `overmind.200` 按 `is_ai` 分派到 AI/玩家族；旧的 `.100` 保留兼容 |
| 玩家行动效果 | `common/scripted_effects/overmind_effects.txt` | 新增 11 个 `overmind_player_action_*`，含**真实结构操作** |
| 原生自动建造 | 同上 | `overmind_player_enable_automation` 用 `set_colony_type = col_capital` 托管核心星球 |
| 舰队姿态硬控 | 同上 | DEFEND 时 `every_owned_fleet { set_fleet_stance = evasive }` |
| 外交好感 | 同上 + `common/opinion_modifiers/overmind_opinions.txt` | `add_opinion_modifier` + 自建 `opinion_overmind_goodwill` |
| 引擎命令构造 | `engine/bridge.py` | `build_player_event_command()` / `is_player_event_command()` / `write_player_directive()` |
| 测试 | `tests/test_bridge.py` | 新增 `TestPlayerDirectiveTransport`（16 个断言点），全套 500 通过 |
| 实机验证工具 | `scripts/verify_run_channel.py` | 三步验证 `run` 通道，测试载荷已就位 |

### 4.4.6 实测校验过的名字（不是猜的）

逐个 grep 原版游戏文件确认存在：`set_colony_type`(6 处) / `is_colony`(61) /
`set_fleet_stance`(99) / `every_owned_fleet`(14) / `every_owned_planet`(20) /
`add_opinion_modifier`(27) / `is_same_value`(177) / `col_capital` 等 colony type /
`every_country` / `add_zone`。

**被排除的错误猜测**（grep 结果为零，已改掉）：
- `set_research_focus` — 不存在。4.4.6 无科研目标选择脚本接口，只能用 `run` + `research_technology`
- `every_neighbor_country` — 作为 effect scope 不存在（只有 `any_neighbor_country` trigger）
- `improve_relations` — 是 envoy 任务名，不是 effect。正确写法是 `add_opinion_modifier`
- `is_fleet_in_use` — 不存在。舰队筛选的正确触发器是 `is_mobile`
- `zone_commercial` — 不存在。正确是 `zone_trade`

### ⚠️ 重大发现：4.4.6 没有通用工业区划

4.x 经济重做把**通用工业区划删掉了**，改成 **zone** 体系：
- 真实区划只剩 `district_mining` / `district_generator` / `district_farming` /
  `district_city` / `district_polytechnic` 等基础类型
- 工业生产改用 `add_zone = { district = <宿主区划> zone = <zone> zone_slot = N }`
  （宿主默认 `district_city`），zone 有 `zone_foundry` / `zone_factory` /
  `zone_industrial` / `zone_research` / `zone_trade` 等

**这修掉了一个静默 bug**：旧编译器的模糊匹配会把 `industrial` 解析成
`district_ark_military_industrial`（**只有游牧方舟能用**），在普通星球上必然失效
且不报错。现在：
- 编译器新增 `zone` 动作类型（共 12 类）
- 模糊匹配**排除变体 ID**（`_ark_` / `_ring_world_` / `_hab_` / `_hive_` / `_nexus_`
  / `_arcology_` / `_machine_` / `_resort_` / `_rw_` 等）
- `industrial` 区划请求**明确报错**而不是给个错的
- 新增 `audit_aliases()`，**121 个别名全部对照游戏文件验证**，有一个失效就让测试失败

### 测试

`522 通过`（原 485 通过 + 1 失败）。
新增 `tests/test_action_compiler.py`（21 个用例，此前编译器**完全没有测试**，
所以那些 bug 才能存活）和 `TestPlayerDirectiveTransport`（16 个断言点）。
顺手修掉了 `test_latencies_recorded` 的既有失败（stub provider 瞬时返回，
耗时合法地四舍五入为 0.0，原断言 `> 0` 过严）。

### 待办 ⏳

1. **实机跑 `verify_run_channel.py`**（游戏需启动）
2. 事件层改造后需**重启游戏**才能加载新事件（Clausewitz 不热重载）
3. 引擎主循环接 `write_player_directive`（当前 `player` 模式走 `GameLoopController`，
   还没有把它和新的玩家事件族连起来）
4. action_compiler 补传统树 / 飞升 / 法令 / 外交
5. 一键暂停开关

### 一句重要提醒

**事件层是"结构姿态层"，不是"硬执行层"。** 真正的逐星球硬控制（建哪个建筑、
上哪个科技）走 `run` 文件通道 —— 因为 `run` 的 console scope 天然就是玩家帝国，
且 Python 端能用 `action_compiler` 校验过的 ID 精确寻址。事件层负责的是
"把核心星球交给原生自动化""把舰队改成规避姿态"这类**不需要逐 id 的结构决策**。
两条腿分工，不要混。

---

## 10. 已确认的事实清单

- 4.4.6 中 `building_alloy_foundry` 已改名 `building_foundry_1`
- `living_standard` 在 4.x 不是 policy，要用 `set_living_standard`
- 政策键名：`economic_policy_military`（非 militarist）、`diplo_stance_cooperative`（非 diplomatic_stance_）、`unrestricted_wars`（非 war_philosophy_）
- 静态修正器：`pop_growth_speed` → `bonus_pop_growth_mult`；`country_outpost_influence_cost_mult` 已不存在
- localisation 文件必须带 UTF-8 BOM，否则游戏内显示原始 key
