# Codex to ZCode Bridge：GPT / Codex 侧

本机 stdio MCP 插件，提供 19 项控制工具、2 项原生设置工具，以及内嵌配置面板。GPT 侧没有 skill，也不需要复制 ZCode 的完整工作流说明。

## 手动安装

需要本机 Python 3.10+、Node、已配置模型的 ZCode，以及 `codex` CLI。下载本侧 Release ZIP 并解压，在目录中运行：

```powershell
python install.py --dry-run
python install.py
```

从完整仓库安装则运行 `python install.py --target codex`。自动寻找失败时，传 `--zcode-executable "<本机实际路径>"`；CLI 不在 PATH 时传 `--codex "<codex CLI 实际路径>"`。自定义位置用 `--install-root`。

安装器会在当前机器生成 Python 和脚本的绝对路径，不会使用发布者的电脑目录。它通过客户端 CLI 注册市场，保留其他市场和插件；支持的全新个人目录也会登记供 ChatGPT 桌面发现。已有其他个人市场会保留，应按总安装说明合并条目。

重新载入客户端插件或开始新聊天，在插件设置中确认程序路径，读取模型后选择默认模型。支持时可使用原生设置和内嵌面板；其他本机 MCP 宿主也可调用同一套工具。网页版不在本项目范围内。

## 让 agent 安装

```text
请安装这个目录中的 GPT / Codex 侧 Codex to ZCode Bridge。
先读 AGENTS.md，检查本机 Python、Node、ZCode 和 Codex CLI。
先运行 python install.py --dry-run，再安装；使用本机路径并保留已有配置。
验证 MCP 初始化、真实模型列表和配置面板。默认模型由我选择，不猜测私有模型编号。
不要为刷新而重启 ZCode，也不要自动打开会抢焦点的桌面项目。
```

## 使用

先读模型，再在指定已有工作区创建会话。每次写入都传入当前 GPT 会话的实际权限。派发后等待最终结果；读取子代理内容时使用返回的真实子会话 ID。

为子代理单独选模型和思考强度时，通常创建 `onDemand` 会话，发送 `/workflow` 自然语言任务，说明范围、阶段、并行、依赖、模型和验收。首次动态工作流授权仍须处理真实请求。

桥接是独立引擎，桌面列表不会实时接收后续新会话通知；核心保存不等于界面已显示。只有明确请求 `zcode_launch` 打开工作区才可能使 ZCode 获得焦点，不能用它自动刷新。

## 手动接入 MCP

不使用安装器时，先准备本机 Python 服务，再使用标准 Codex 插件命令：

```powershell
python -m pip install "git+https://github.com/cateyemizuki/codex-to-zcode-bridge.git@v0.3.0#subdirectory=plugins/codex"
codex plugin marketplace add cateyemizuki/codex-to-zcode-bridge
codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins
```

已解压本侧 ZIP 时也可先 `python -m pip install .`。确保宿主 PATH 中的 `python` 与安装 Python 包的环境一致。本包原始 `.mcp.json` 采用 `python -m zcode_bridge mcp`；普通安装器会替换成本机确定的启动路径。

项目、完整安装与版本说明：[GitHub 仓库](https://github.com/cateyemizuki/codex-to-zcode-bridge)。许可见本包 `LICENSE`。
