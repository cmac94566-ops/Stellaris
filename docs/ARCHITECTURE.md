# 架构设计

项目：Stellaris Overmind · arch v1 · 2026-09-14

---

## 1. 总体数据流

```
                 ┌──────────────── 游戏外（本地） ────────────────┐
                 │                                              │
  存档 .sav ──▶ ①读档解析 ──▶ ②大模型战略 ──▶ ③编译器 ──▶ ④法案脚本
     ▲            (save_reader)  (strategic_planner) (strategy_lex)   │
     │                                                              │
     │                                                    ⑤审计门禁 │
     │                                                    (script_audit)
     │                                                              │
     └──────── ⑧存档回流（成效量化）                                 │
                                                                    │
                 └───────────────────┬────────────────────────────┘
                                     │  ⑥启动游戏（-continuelastsave）
                                     ▼
                 ┌──────────────── 游戏内（Clausewitz） ────────────────┐
                 │  ⑦自治执行层：每月自检 → 按法案日程行动             │
                 │     on_monthly_pulse_country → overmind.300         │
                 │       → overmind_autonomy_tick                     │
                 │          ├─ 换版检查（om_lex_rev vs @om_lex_rev）   │
                 │          ├─ 姿态自适应                             │
                 │          └─ 本月日程格位 → 相位 1..8                │
                 └────────────────────────────────────────────────────┘
```

**关键点**：闭环在**⑤/⑥之间**被物理切断——因为 Clausewitz 不读外部文件（约束 C1）。
补上这个断点的唯一办法是"**在游戏加载脚本的时刻把大模型的决策送进去**"，
这就是法案（Lex）机制存在的全部理由。

---

## 2. 模块职责

### 2.1 游戏外（Python）

| 模块 | 职责 | 不负责 |
|---|---|---|
| `engine/save_reader.py`（既有） | 解析存档为状态字典 | 不做决策 |
| `engine/strategic_planner.py`（既有） | 产出战略（LLM 优先，失败回退代码评估） | 不产出脚本 |
| `engine/strategy_lex.py`（新） | 战略 → 法案载荷 → 渲染为两个脚本文件 | 不解析存档、不调模型 |
| `engine/script_audit.py`（新） | 扫描 mod 脚本，校验所有 ID 在游戏数据中存在 | 不做语义判断 |
| `scripts/overmind_takeover.py`（新） | 校验并启动游戏（启动开关从 exe 实测） | 不做编译 |
| `scripts/mine_action_vocabulary.py`（新） | 从 exe + 官方脚本挖出可用动作清单 | 不执行动作 |
| `scripts/overmind_play.py`（**待建**） | 一键：读档→模型→编译→审计→启动 | — |

### 2.2 游戏内（Clausewitz 脚本）

| 文件 | 职责 |
|---|---|
| `common/scripted_variables/overmind_lex_vars.txt` | 法案版本戳（**必须放这里**：只有该目录的 `@` 变量全局可见） |
| `common/scripted_effects/overmind_lex.txt` | 法案装载器：把法案写入国家变量（**生成文件**） |
| `common/scripted_effects/overmind_autonomy.txt` | 执行器：逐月自检并行动（手写） |
| `common/scripted_triggers/overmind_autonomy_triggers.txt` | 状态判定谓词 |
| `events/overmind_autonomy_events.txt` | 事件壳（只做转发） |
| `common/on_actions/overmind_on_actions.txt` | 挂载月度/年度/开局脉冲 |
| `common/edicts/overmind_autonomy_edicts.txt` | 零控制台 UI 开关 |

---

## 3. 法案（Lex）接口契约

法案是游戏外与游戏内之间**唯一的契约面**。它必须小、封闭、可校验。

### 3.1 标量旋钮（国家变量）

| 变量 | 取值 | 含义 |
|---|---|---|
| `om_lex_rev` | 正整数 | 版本戳（与全局 `@om_lex_rev` 比对决定是否换版） |
| `om_lex_slot1..8` | 1..8 | 月度日程八格，每格一个相位 |
| `om_lex_threat` | 1..4 | 威胁等级：低/中/高/危急 |
| `om_lex_bottleneck` | 1..3 | 救济相位优先补：能源/矿物/粮食 |
| `om_lex_stance` | 1..3 | 舰队姿态：守/均衡/攻 |
| `om_lex_focus_zone` | 1..5 | 专业区方向：工业/铸造/科研/贸易/凝聚力 |

### 3.2 相位编码

| 码 | 相位 | 主要动作 |
|---|---|---|
| 1 | RELIEF | 补真正赤字的资源产出 |
| 2 | HOUSING | 建城区 |
| 3 | JOBS | 建专业区（按 `focus_zone`） |
| 4 | AMENITIES | 建舒适度建筑 |
| 5 | SCIENCE | 建科研设施 + 科研友好政策 |
| 6 | MILITARY | 舰队补船 + 舰队姿态 |
| 7 | ASCENSION | 传统 + 飞升天赋 + 星球飞升 |
| 8 | WAR | 星港 / 陆军 / 轰炸姿态 / 战争目标 |

### 3.3 生成式 effect（供未来扩展）

对无法用整数表达的决策（传统/飞升的顺序、战争目标选择），
编译器**生成一段独立的 scripted effect**，其中每个 ID 都由编译器对照游戏定义文件校验后才落盘。
即：**大模型选 ID，编译器证明 ID 存在**。

### 3.4 契约不变式

- 生成文件永不包含自由文本（除经净化的 `log` 行：去 `[`、`$`、引号、非 ASCII）
- 生成文件的取值域是**封闭整数集**，因此不可能产出语法非法脚本
- 版本戳不匹配 = 换版，版本戳相同 = 不动（幂等）

---

## 4. 两处易错的工程细节（已被本项目的真实故障验证过）

| 细节 | 故障表现 | 结论 |
|---|---|---|
| `@变量` 的作用域 | 若把版本戳定义在 `common/scripted_effects/` 里，执行器读不到 | **必须**放 `common/scripted_variables/`（全局可见） |
| 日志字符串转义 | 曾写 `log = "[OVERMIND] ..."` → 方括号被当数据函数解析，报 `Invalid macro entry: VERMIND` | 日志只用纯 ASCII，去 `[]`、`$`、引号 |

---

## 5. 架构决策记录（ADR）

| 编号 | 决策 | 理由 | 被否方案 |
|---|---|---|---|
| ADR-1 | 用"编译式注入"而非运行时注入 | C1：游戏不读运行时文件 | 控制台键盘注入（用户否决）、UDP 注入（需联机） |
| ADR-2 | 大模型输出**封闭取值域**，不产出脚本 | 脚本语法错误会静默失效且难查 | 让大模型直接写脚本（不可控） |
| ADR-3 | 法案用**整数编码 + 生成式 effect**混合 | 整数保证安全；生成式 effect 保证表达力 | 纯整数（表达力不足）、纯生成（风险高） |
| ADR-4 | 建造即时生效 + 自建费用模型 | C3：脚本塞不进玩家建造队列 | 依赖原生自动发展（无脚本开关）、神模式（无成本，失真） |
| ADR-5 | 使用原生 edict 作为开关 | 唯一零控制台、零 mod 冲突的国家级 UI 入口 | decision（4.4.6 基本是行星作用域） |
| ADR-6 | 动作清单从游戏本体挖掘 | 靠记忆必漏且会编造名字 | 手写清单 |
| ADR-7 | 放弃成就 | 用户裁定（见计划书 1.2） | — |
