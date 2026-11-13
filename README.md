# codex-to-zcode-bridge

让 ChatGPT / Codex 和其他支持本机 MCP 的 agent 工具调用本机 ZCode：选择已有模型、在指定工作区创建会话、派发任务、读取主代理和子代理结果、处理问题与授权，并使用 ZCode 的原生动态工作流。

本仓库提供两个独立安装包，版本 **0.3.0**。它们共享同一份运行时代码和本机控制服务。

| 安装包 | 用途 | 说明 |
| --- | --- | --- |
| [GPT / Codex 侧](plugins/codex/README.md) | 本地桌面客户端的纯 MCP 插件，含原生配置项和内嵌面板 | 不依赖 GPT 侧 skill |
| [ZCode 侧](plugins/zcode/README.md) | ZCode 插件，含桥接管理指引、命令与同一 MCP 服务 | 日常编码使用当前会话的原生工具 |

下载：[Releases](https://github.com/cateyemizuki/codex-to-zcode-bridge/releases)。可只安装需要的一侧，也可安装两侧。GitHub 发布与 OpenAI 公共插件目录上架是不同的渠道；本仓库采用本机安装方式，不提供网页版或公网桥接。

## 能做什么

- 读取 ZCode 已配置的模型与实际模型编号，指定主会话模型和支持的思考强度。
- 在已有的绝对工作区创建独立会话，发送内容、等待结果、停止指定会话。
- 分页读取主代理和子代理消息，查看动态工作流的阶段、节点与结果。
- 读取并回答真实的问题与权限请求，包括首次动态工作流授权。
- 按调用方当前权限设置 ZCode 会话；完全访问对应 `yolo`，只读会先降级基础权限再开启规划层。
- 在 ZCode 未运行时按配置启动；通过原生设置或内嵌面板配置工作区、模型和程序路径。

## 安装前

需要 Python 3.10+、Node.js、已安装并完成模型配置的 ZCode。GPT / Codex 安装器还需要可用的 `codex` CLI。运行时没有第三方 Python 依赖。实机集成基线为 Windows、ZCode Desktop 3.17.2 / engine 0.17.1、Python 3.12、Node.js 24；其他 ZCode 协议版本可能需要适配。

从 GitHub 直接安装 Python 服务包或克隆仓库还需要 Git；ZIP 安装器不要求先执行 pip 安装。

安装器会从环境、系统注册信息和常见安装位置寻找 ZCode，也可显式指定。默认工作区是当前用户目录；**不预设任何发布者模型编号**。安装后先读取模型并选择默认模型。

## 手动安装

### 使用 Codex 插件命令

本机已有 Python、Node 和 ZCode 时，先安装 MCP 服务的 Python 包，再用 Codex 命令注册市场和安装插件：

```powershell
python -m pip install "git+https://github.com/cateyemizuki/codex-to-zcode-bridge.git@v0.3.0#subdirectory=plugins/codex"
codex plugin marketplace add cateyemizuki/codex-to-zcode-bridge
codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins
```

Codex 命令负责插件登记，第一条命令准备它要启动的本机服务。Python 必须与客户端 PATH 中的 `python` 一致。若不想管理 Python 包，使用下面的 ZIP / 仓库安装器，它会自动生成本机启动路径并执行同样的 Codex 注册命令。安装后重新载入插件或开始新聊天，再选择工作区和真实模型。

### 从完整仓库安装

```powershell
git clone https://github.com/cateyemizuki/codex-to-zcode-bridge.git
cd codex-to-zcode-bridge
python install.py --target both
```

只装 GPT / Codex 侧：`python install.py --target codex`。只装 ZCode 侧：`python install.py --target zcode`。

如果自动发现失败，使用自己的实际路径：

```powershell
python install.py --target both --zcode-executable "<ZCode 可执行文件的绝对路径>" --workspace "<工作区绝对路径>"
```

也可以分别传入 `--engine-entry`、`--node-executable`、`--codex`。这些参数只会写进**安装机器**的本地配置，不会改写仓库里的发布清单。

### 从 ZIP 安装

在 Releases 下载 `codex-to-zcode-bridge-codex-0.3.0.zip` 或 `codex-to-zcode-bridge-zcode-0.3.0.zip`，解压后进入该目录，运行：

```powershell
python install.py
```

每个 ZIP 都是完整的独立插件，无需另外下载共享目录。不要把两个 ZIP 覆盖解压到同一目录。

安装器会复制到当前用户的本地应用数据目录，生成本机 MCP 启动配置，再通过对应客户端 CLI 注册市场和插件。它保留其他市场、插件与模型凭据，不会重启 ZCode、停止任务或自动打开桌面项目。安装完成后重新载入客户端插件，或开始新聊天。

### 想先检查或自行注册

```powershell
python install.py --target both --dry-run
python install.py --target both --no-register
```

`--dry-run` 只打印计划。`--no-register` 准备本地插件和市场文件，打印实际安装位置，由你随后注册。完整步骤与升级方式见 [安装与排错](docs/INSTALL.md)。

## 让 agent 帮你安装

把下面这段交给有本机文件和命令能力的 agent：

```text
请从 https://github.com/cateyemizuki/codex-to-zcode-bridge 安装 Codex to ZCode Bridge。
先读取仓库 AGENTS.md 和 README.md，按你当前运行的客户端选择对应章节。
检查本机 Python、Node、ZCode 与客户端 CLI，先运行安装器 dry-run；使用本机发现的路径，不复制作者电脑的配置。
安装我需要的一侧或两侧插件，保留已有市场、插件和模型凭据。
读取真实模型列表后让我选择默认模型；权限按当前会话传入，不自行假定完全访问。
验证 MCP 初始化、模型读取与配置面板，报告安装位置和验证结果。
不要为刷新列表重启正在工作的 ZCode，也不要自动打开会抢焦点的桌面项目。
```

如果你还没指定装哪一侧，agent 应先根据当前客户端说明选择，跨客户端安装时再确认目标。

## 初次配置与使用

1. 在支持的本地客户端打开插件设置或调用“打开 ZCode 配置面板”。
2. 确认程序、引擎、Node 和工作区路径；读取已配置的模型。
3. 选择默认模型与思考强度，或在创建会话时显式指定。
4. 给出任务、工作区与验收要求；派发后持续读取结果与待处理请求。

需要为子代理分别选择模型和强度时，通常使用原生动态工作流。用自然语言说明阶段、依赖、并行关系、每个角色的真实模型编号、强度和验收。GPT 侧只需这份简短契约，不需要复制完整的 ZCode 动态工作流 skill。见 [工作流说明](docs/WORKFLOWS.md)。

## 已知边界

- 桥接与桌面使用独立引擎连接。核心会话保存成功，不等于桌面列表已经显示；返回的 `desktop.indexed` 和 `workspace_registered` 会分别报告登记情况。
- 首次打开对应工作区可导入已有会话。已经打开的项目不会实时收到独立引擎的新会话通知，重复打开也不能保证刷新。不要同时在两个引擎中操作同一运行中的会话。
- 打开桌面工作区可能使 ZCode 获得焦点，只能在明确要求时使用 `zcode_launch(workspace=..., allow_focus=true)`。默认派发和轮询不执行这个操作。
- 权限由调用方传入；插件不能读取 GPT 界面上的权限按钮。首次动态工作流仍可能需要原生授权。
- 支持已配置的个人模型提供方；桥接不冒充桌面账号刷新内置订阅鉴权。当前协议不匹配时会返回错误，不保证其他版本的内部协议兼容。

## 开发与发布

```powershell
python -m unittest discover -s tests -q
python build.py --check
python scripts/audit_release.py
python build.py --archives
```

`runtime/` 是唯一运行时代码来源。`plugins/codex` 与 `plugins/zcode` 是由 `build.py` 生成的独立插件，便于直接分发。修改运行时后重新生成两侧包，并提交生成结果。发布前的检查排除本地凭据、运行状态、数据库、个人路径、安装报告和历史会话。

自动测试模板保存在 [docs/ci-example.yml](docs/ci-example.yml)。需要 GitHub Actions 时，由维护者将它放到 `.github/workflows/ci.yml`；上传或更新这个生效路径需要相应的 `workflow` 权限。当前发布采用本机验证，不自动启用 Actions。

agent 维护指南：[AGENTS.md](AGENTS.md)；兼容入口：[agent.md](agent.md)。安装记录只保存在安装机器，不进入 Git。许可证：[MIT](LICENSE)。

本地市场与兼容清单的安装规则参考 [OpenAI 官方插件文档](https://developers.openai.com/plugins/build/plugins)；设置和内嵌面板参考 [官方扩展文档](https://developers.openai.com/plugins/build/extensions)。
