# Stellaris Overmind 部署说明

部署时间：2026-09-13
项目路径：`C:\Users\<user>\.zcode\workspace\default\Stellaris_Overmind`
游戏：`D:/SteamLibrary/steamapps/common/Stellaris`（v4.4.6）

---

## 一、已完成

| 项 | 状态 | 说明 |
|---|---|---|
| Ollama 0.34.0 | 已装 | `C:\Users\<user>\AppData\Local\Programs\Ollama` |
| qwen2.5:7b（主用） | 已导入 | Q4_K_M，4.7 GB |
| qwen2.5:14b | 已导入 | Q4_K_M，9.0 GB（显存吃紧，见第三节） |
| qwen2.5:3b | 已导入 | Q4_K_M，1.9 GB（子代理备用） |
| 模型存储 | D 盘 | `D:\Ollama\models`，已用 junction 兜底 |
| 本地推理 | 已验证 | 360–520 ms / 次 |
| 智谱在线回退 | 已验证 | glm-4.7-flash |
| mod junction | 已装 | `Documents\Paradox Interactive\Stellaris\mod\stellaris_overmind` |
| 引擎测试 | 485 / 486 通过 | 唯一失败是 stub 延迟为 0 的断言，与功能无关 |

## 二、怎么跑

双击 `start_overmind.bat` 即可。它会：

1. 检测 Ollama 服务，没起就拉起一个最小化窗口（**必须走 `ollama serve`，GUI 版内嵌服务会启动超时**）
2. 进入项目目录，启动 `engine.main --console` TUI

TUI 里按 `M` 切 hybrid 模式，按 `F` 开 fast decisions。

手动等价命令：

```
set OLLAMA_MODELS=D:\Ollama\models
ollama serve
cd C:\Users\<user>\.zcode\workspace\default\Stellaris_Overmind
py -3.12 -m engine.main --console
```

## 三、显存：为什么主用 7B 而不是 14B

RTX 4070 Ti 只有 12 GB，游戏也要占显存。实测：

| 模型 | 加载后显存 | 剩余 |
|---|---|---|
| qwen2.5:14b | **11266 MiB / 12282 MiB** | 约 1 GB |
| qwen2.5:7b | 6428 MiB / 12282 MiB | 约 5.8 GB |

14B 只剩 1 GB，Stellaris 一开必 OOM。**所以默认配 7B**，14B 保留可用（不开游戏时，或愿意忍受 CPU 卸载降速时可切）。

要切模型，改 `config.toml` 的 `[llm] model = "qwen2.5:14b"` 即可，无需重新下载。

## 四、我改了上游三处（都是必要的修复）

1. `engine/clausewitz_parser.py`
   - 大存档保护：gamestate 超过 `OVERMIND_MAX_GAMESTATE_MB`（默认 512）直接报错跳过，不再崩栈
   - 深嵌套保护：递归解析放进 128 MB 栈的线程里跑（CPython 在 Windows 上最大接受 128 MB）

2. `engine/save_reader.py`
   - 捕获 `RecursionError` / `MemoryError`，解析失败只记录日志、不中断循环

3. `engine/qwen_provider.py`
   - `/v1/chat/completions` 返回 404 时自动回退到 `/chat/completions`
     智谱真实路径是 `/api/paas/v4/chat/completions`，没有 `/v1`，不加这个回退在线通道必然 404

## 五、已知限制

- **当前存档解析不了**：现有 `autosave_2242.01.01.sav` 的 gamestate 解压后 **1.7 GB**（2242 年后期大档）。纯 Python 解析器吃不下（官方注释是 5 MB 约 2–4 秒，换算要 17 分钟以上）。
  → **开新档**即可，早期存档只有几 MB。
  非要解析旧档的话：`set OVERMIND_MAX_GAMESTATE_MB=2048`，但会很慢很吃内存。
- 智谱 glm-4.7-flash 是混合思考模型，`max_tokens` 必须给足（配置里已是 1024）。给 256 会被思考过程吃光，返回空内容。
- C 盘只剩约 41 GB。`C:\Users\<user>\Downloads\OllamaSetup.exe`（1.5 GB）可以删。
- 游戏目录有 `cream_api.ini` 等非官方文件，若 mod 加载异常需排查这个因素。
