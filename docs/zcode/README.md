# Codex to ZCode Bridge：ZCode 侧

ZCode 插件，提供桥接管理用的原生 skill、`bridge-settings` 命令和同一套 21 项 MCP 工具。两侧共用本机控制服务和配置。

## 手动安装

需要本机 Python 3.10+、Node 和已安装的 ZCode。下载本侧 Release ZIP，解压后在目录中执行：

```powershell
python install.py --dry-run
python install.py
```

完整仓库中执行 `python install.py --target zcode`。自动寻找失败时，加 `--zcode-executable "<本机实际可执行文件>"`，或指定 `--engine-entry`、`--node-executable`。

安装器调用当前 ZCode 的插件 CLI 添加本地市场并安装，不改动应用安装文件，不复制 provider 凭据，不重启正在工作的 ZCode。重新载入插件或开始新会话后检查工具是否发现。

模型与工作区在本机面板配置。没有可用的内嵌 MCP App 时，可调用 `zcode_settings`；明确要求打开浏览器时才调用 `zcode_open_settings(open_browser=true)`。

## 让 agent 安装

```text
请安装这个目录中的 ZCode 侧 Codex to ZCode Bridge。
先读 AGENTS.md，检查本机 Python、Node 和 ZCode 引擎，先运行安装器 dry-run，再安装。
保留已有市场、插件和模型凭据，使用当前机器的路径。
验证真实工具列表与模型读取，报告结果。不要通过桥接把安装任务递归派发给自己。
不要重启正在工作的 ZCode 或自动切换到前台。
```

## 使用边界

正常编码和当前委派任务使用本会话的原生工具。只有用户明确要求管理另一桥接会话时，才对其精确 ID 使用桥接工具；不要为普通任务创建自己递归派发的会话。

需要为子代理分别选模型和强度时，使用 ZCode 原生动态工作流并加载它自己的 skill。GPT 侧只需要简短的自然语言能力说明。

当前权限由发起工具提供；完全访问不能免除首次工作流的真实授权。不要根据其他会话的历史权限提升当前会话。

本机路径由安装器生成。直接通过 ZCode 市场安装时，宿主会解析插件根目录并启动包内服务，无需额外 pip 安装；需要保证宿主 PATH 中的 `python` 可用。

完整项目和已知桌面同步限制：[GitHub 仓库](https://github.com/cateyemizuki/codex-to-zcode-bridge)。许可见本包 `LICENSE`。
