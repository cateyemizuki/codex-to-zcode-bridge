# 安装与排错

## 两种安装方案

手动：克隆仓库，或下载对应侧的 Release ZIP，在解压目录运行 `python install.py`。完整仓库可加 `--target codex`、`--target zcode` 或 `--target both`。

agent 协助：将根 README 的安装提示词交给拥有本机文件和命令能力的 agent。它应先读 `AGENTS.md`，识别自己的客户端，运行 dry-run 后安装，保留你现有配置。

## 路径与本机配置

发布包不保存固定 Python、ZCode、Node 或工作区路径。安装器在本机生成绝对路径的 MCP 文件，以避免不同 Python 环境混淆；这些文件属于本地安装结果。

自动寻找顺序包括显式环境变量、系统注册信息、常见应用目录。找不到时传入 `--zcode-executable "<实际可执行文件>"`；只有引擎位置已知时传 `--engine-entry "<实际 zcode.cjs>"`。`--workspace` 要求现有目录，不会创建任意工作区。

可选环境变量：`ZCODE_INSTALL_DIR`、`ZCODE_EXECUTABLE`、`ZCODE_ENGINE_ENTRY`、`ZCODE_NODE_EXECUTABLE`、`ZCODE_WORKSPACE`、`ZCODE_BUILTIN_PROVIDER_FILE`、`ZCODE_PERSONAL_PROVIDER_FILE`、`ZCODE_MODEL_PROVIDER`、`ZCODE_MODEL_ID`。它们是本机输入，不应放进 Git。

默认状态目录是当前用户本地应用数据目录下的 `CateyeZCodeBridge`；其他平台使用当前用户的 `.local/share`。可用 `ZCODE_BRIDGE_STATE` 指定隔离状态目录。这个目录含连接令牌和会话记录，应保留在本机。

模型鉴权由本机 ZCode 的模型配置提供。桥接只读取模型元数据，不把密钥作为工具结果返回，也不把 provider 文件复制到安装包。

## 手动注册与直接市场安装

安装器通常会调用客户端 CLI。需要自行注册时先运行 `--no-register`，使用输出中的市场根目录：

```powershell
codex plugin marketplace add "<安装器输出的市场根目录>"
codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins
```

ZCode 侧使用当前安装的引擎 CLI：

```powershell
node "<本机 zcode.cjs>" plugins marketplace add "<市场根目录>" --json
node "<本机 zcode.cjs>" plugins install codex-to-zcode-bridge@codex-to-zcode-bridge-plugins --json
```

熟悉 Python 环境的用户也可以直接从仓库市场安装 Codex 侧：先 `python -m pip install ./plugins/codex`，让客户端使用的 `python` 能导入 `zcode_bridge`；再 `codex plugin marketplace add cateyemizuki/codex-to-zcode-bridge` 和 `codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins`。当前 Codex 版本不会给 MCP 服务展开插件根目录变量，因此这一步 Python 包准备不能省略；普通用户也可使用安装器，自动生成本机脚本路径。

ZCode 侧使用自己的市场命令。其宿主支持插件根目录变量，会启动包内服务，无需额外 pip 安装；两侧可共用同一个控制服务。

## ChatGPT 桌面列表未发现插件

新聊天或重载插件后再检查。全新的个人插件目录可由安装器登记；已存在且属于其他市场的个人目录会保留，不会被覆盖。可以让 agent 按 [官方本地市场规则](https://developers.openai.com/plugins/build/plugins) 将输出的插件路径合并进已有个人目录，并保留原来的市场名与其他条目。自定义安装位置在用户目录之外时，优先通过 CLI 注册本地市场，不写越出个人市场根目录的相对路径。

客户端需支持本地 stdio MCP；原生设置和 MCP App 还需要相应客户端功能。功能不可用时，可以继续通过 MCP 工具配置，或明确要求打开本地浏览器配置页。未提供网页版连接。

## 升级与移除

从新版仓库或 ZIP 重新执行安装器。相同安装位置会先备份原插件再更新；已有配置与 provider 凭据保留。当前发布版本为 0.3.1；状态返回的 `bridge_version` 可用于确认实际运行版本，这不需要重启 ZCode 桌面。两侧使用同一个控制服务，升级不要强制结束正在运行的会话。已运行的旧控制服务可能仍使用旧代码；先等该服务管理的任务结束，再重新载入桥接服务。

移除时先用对应客户端的插件管理功能卸载该插件，再删除你确认属于此安装的目录。安装器不会自动删除用户工作区、ZCode 数据库或模型配置。不同市场名的旧开发版本需单独检查，避免两个版本同时暴露重复工具。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 找不到引擎、Node 或 CLI | 检查实际安装路径，使用相应参数；先 `--dry-run` |
| 尚未选择默认模型 | 读取真实模型列表，在面板选择或创建时传入模型 |
| 配置面板暂时连接不上 | 路径设置仍可打开，修正后重试，不重装作者机器的配置 |
| 会话已保存但桌面列表没有 | 检查 `desktop.indexed`；独立引擎不实时推送到已打开项目，`indexed` 也不等于侧栏当前可见 |
| 消息分页/锚点为 null | 0.3.1 起兼容 3.17 的 `info.id` 与 3.18.2 的 `info.messageId`；没有任何 ID 时保留调用方 anchor，不伪造 ID |
| ZCode 获得焦点 | 只有明确的工作区打开才应发生，不用该操作刷新列表 |
| 首次动态工作流仍要求授权 | 正常读取并处理真实请求，完全访问不能伪造该步骤 |
| 官方订阅鉴权刷新不可用 | 采用已配置且桥接能使用的个人模型提供方，不复制桌面账号令牌 |
