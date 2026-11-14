# codex-to-zcode-bridge

让 ChatGPT / Codex 和其他支持本机 MCP 的 agent 工具调用本机 ZCode：选择已有模型、在指定工作区创建会话、派发任务、读取主代理和子代理结果、处理问题与授权，并使用 ZCode 的原生动态工作流。任务完成后，桥接可以持久记录完成事件，并在版本支持的 Codex Desktop 上尝试唤醒原 chat 继续验收。

本仓库提供两个独立安装包，版本 **0.4.0**。它们共享同一份运行时代码和本机控制服务。

| 安装包 | 用途 | 说明 |
| --- | --- | --- |
| [GPT / Codex 侧](plugins/codex/README.md) | 本地桌面客户端的纯 MCP 插件，含原生配置项和内嵌面板 | 不依赖 GPT 侧 skill |
| [ZCode 侧](plugins/zcode/README.md) | ZCode 插件，含桥接管理指引、命令与同一 MCP 服务 | 日常编码使用当前会话的原生工具 |

下载：[Releases](https://github.com/cateyemizuki/codex-to-zcode-bridge/releases)。可只安装需要的一侧，也可安装两侧。GitHub 发布与 OpenAI 公共插件目录上架是不同的渠道；本仓库采用本机安装方式，不提供网页版或公网桥接。

## 能做什么

- 读取 ZCode 已配置的模型与实际模型编号，指定主会话模型和支持的思考强度。
- 在已有的绝对工作区创建独立会话，发送内容、等待结果、停止指定会话。
- 为一次派发持久订阅完成事件；任务结束后按事件 ID 拉取收件箱，并在版本支持的 Codex Desktop 上尝试唤醒原 chat 继续验收。
- 分页读取主代理和子代理消息，查看动态工作流的阶段、节点与结果。
- 读取并回答真实的问题与权限请求，包括首次动态工作流授权。
- 按调用方当前权限设置 ZCode 会话；完全访问对应 `yolo`，只读会先降级基础权限再开启规划层。
- 在 ZCode 未运行时按配置启动；通过原生设置或内嵌面板配置工作区、模型和程序路径。

## 安装前

需要 Python 3.10+、Node.js、已安装并完成模型配置的 ZCode。GPT / Codex 安装器还需要可用的 `codex` CLI。运行时没有第三方 Python 依赖。实机集成基线为 Windows、ZCode Desktop 3.17.2 / engine 0.17.1、Python 3.12、Node.js 24；其他 ZCode 协议版本可能需要适配。完成通知的宿主唤醒还依赖 Codex Desktop 自带的第一方 MCP 服务版本；桥接运行时探测，不保证跨版本可用。

从 GitHub 直接安装 Python 服务包或克隆仓库还需要 Git；ZIP 安装器不要求先执行 pip 安装。

安装器会从环境、系统注册信息和常见安装位置寻找 ZCode，也可显式指定。默认工作区是当前用户目录；**不预设任何发布者模型编号**。安装后先读取模型并选择默认模型。

## 手动安装

### 使用 Codex 插件命令

本机已有 Python、Node 和 ZCode 时，先安装 MCP 服务的 Python 包，再用 Codex 命令注册市场和安装插件：

```powershell
python -m pip install "git+https://github.com/cateyemizuki/codex-to-zcode-bridge.git@v0.4.0#subdirectory=plugins/codex"
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

在 Releases 下载 `codex-to-zcode-bridge-codex-0.4.0.zip` 或 `codex-to-zcode-bridge-zcode-0.4.0.zip`，解压后进入该目录，运行：

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
如需要完成通知，只在我明确要求后按当前 chat 授权订阅；不要默认开启自动续工，也不要把收件箱拉取说成已唤醒。
不要为刷新列表重启正在工作的 ZCode，也不要自动打开会抢焦点的桌面项目。
```

如果你还没指定装哪一侧，agent 应先根据当前客户端说明选择，跨客户端安装时再确认目标。

## 初次配置与使用

1. 在支持的本地客户端打开插件设置或调用“打开 ZCode 配置面板”。
2. 确认程序、引擎、Node 和工作区路径；读取已配置的模型。
3. 选择默认模型与思考强度，或在创建会话时显式指定。
4. 给出任务、工作区与验收要求；派发后持续读取结果与待处理请求。需要完成通知或自动续工时，在派发前说明并确认当前 chat 的授权；见下节。

需要为子代理分别选择模型和强度时，通常使用原生动态工作流。用自然语言说明阶段、依赖、并行关系、每个角色的真实模型编号、强度和验收。GPT 侧只需这份简短契约，不需要复制完整的 ZCode 动态工作流 skill。见 [工作流说明](docs/WORKFLOWS.md)。

## 完成通知与续工

完成通知是可选的持久订阅，不改变 ZCode 的模型、权限或工作区设置。它解决的是“发起后结束本轮，完成后回来验收”，不是让桥接替用户判断任务是否合格。

- 派发时使用 `zcode_send(..., notify={completion_notify:true,auto_continue:false,user_authorized:true}, idempotency_key="...")`。桥接会先持久化订阅，再发送消息；`accepted` 只代表引擎接受消息，不代表任务完成。已经在运行的任务可用 `zcode_notify_subscribe` 绑定当前轮次或活动后台工作。
- 订阅绑定 session、当前轮次锚点（输入 ID、用户消息、最后消息或活动 run）和来源 chat。一个 session 同一时间只允许一个活动订阅；其他 chat 会收到 `session_busy` 或 `session_busy_foreign`。
- 原生设置和内嵌面板可调 4 项运营参数：轮询间隔（默认 1 秒）、轮次启动等待（默认 120 秒）、订阅过期（默认 6 小时）、唤醒尝试次数（默认 5 次）。它们只影响观察和投递，不授予唤醒或自动续工权限。
- 观察器只在真实终态证据齐全时记录 `turn.completed`：没有活动 workflow、background job 或子代理，没有新的待处理问题，连续稳定采样，且锚点后出现新的最终结果。`accepted`、首次 idle、waiting、error、paused 和仍有后台工作都不算完成。取消的 job/run 记为 `turn.cancelled`，不是完成。
- 事件先写入状态目录的 `notifications.json`，再由投递线程尝试唤醒。`zcode_notify_inbox(event_id=...)` 是权威读取路径；拉取收件箱不是唤醒。事件 ID 由 kind、session、subscription 和固定锚点计算，失败原因、重试、native seq、pid 和墙钟都不进入 ID。
- 宿主唤醒 prompt 只含 session ID、event ID 和固定的“读取收件箱并验收/继续”说明，不含任务正文、路径、模型或子代理内容。根代理被唤醒后必须重新读取真实结果，不能直接相信事件文本或子代理输出。
- 宿主唤醒使用当前版本 Codex Desktop 自带的第一方标准 MCP stdio 服务，运行时探测 `send_message_to_thread`。缺少工具、握手失败或本机管道不可用时，事件保留在待送队列并标记 `unsupported` / `pipe_unavailable`，不会因此宣称完成或已唤醒。跨版本不保证可用。
- 自动续工只在单个订阅显式设置 `auto_continue=true`、`user_authorized=true` 且调用方权限不是 `read_only` 时启用；没有全局开关。一次成功的宿主 `tools/call` 只表示宿主接受了消息，不表示模型已读或继续。
- 失败投递按 5/15/60/300/900 秒退避，默认最多 5 次；之后保留事件，可用 `zcode_notify_ack(retry_failed=true)` 手动重排 `failed`/`uncertain`，宿主重新确认支持后也会重排 `unsupported`。订阅默认 6 小时后过期，事件保留 7 天或最多 500 条。
- 重启桥接后，持久事件和订阅仍在；观察会重新 resume/回放，宿主能力缓存清空。若宿主管道变化，需要重新探测或重新订阅。`send_uncertain` 表示发送可能已经执行，除非用户明确要求，不要用 `retry=true` 重发。
- 取消订阅必须在发起任务的同一 chat 调用 `zcode_notify_cancel`。已投递的唤醒无法撤回；完成通知区域只聚合显示，不提供取消按钮，也不授予唤醒权限。

该能力不实现私有 pipe 协议，不修改 Codex 应用、二进制或数据库，也不使用 `codex queue --thread ... --resume` 假装续工（该命令的 resume 失败仍可能返回成功）。真实端到端唤醒仍待根代理在真实 Codex Desktop chat 上实测；当前验证覆盖标准 MCP 协议和失败队列，不代表跨版本已续工。

## 已知边界

- 桥接与桌面使用独立引擎连接。核心会话保存成功，不等于桌面列表已经显示；返回的 `desktop.indexed` 和 `workspace_registered` 会分别报告登记情况。
- 首次打开对应工作区可导入已有会话。已经打开的项目不会实时收到独立引擎的新会话通知，重复打开也不能保证刷新。不要同时在两个引擎中操作同一运行中的会话。
- 引擎进程仍在但 session runtime 已卸载时，只读的 `session/read`、`session/messages`、`session/events` 会对本桥接已加载/创建的同一 SID 做一次 native resume 后重试一次；发送、改模式、停止和回答不会重放，恢复失败保留原始错误。
- 新建会话尚未开始首轮时引擎还没有 parent 行；只有本桥接本代创建、`turnCount=0`、idle 且无 workflow/job 的首个通知发送，才把 `session/subagents` 的精确 `Session not found: <SID>` 视为尚无子代理；已有轮次、活动后台、恢复或未知会话仍严格失败。
- 打开桌面工作区可能使 ZCode 获得焦点，只能在明确要求时使用 `zcode_launch(workspace=..., allow_focus=true)`。默认派发和轮询不执行这个操作。
- 权限由调用方传入；插件不能读取 GPT 界面上的权限按钮。首次动态工作流仍可能需要原生授权。
- 完成事件是持久记录，不等于模型被唤醒。toast、MCP 日志、工具返回和 `delivered` 都不代表模型已读或已续工；只有宿主接受 `tools/call` 这一层可被确认。
- 宿主唤醒是版本相关能力，运行时探测第一方 MCP 的 `send_message_to_thread`。不支持或握手失败时事件保留在待送队列，跨版本不保证；真实端到端唤醒仍需在真实 Codex Desktop chat 上验收。
- 一个 session 同时只有一个通知订阅 owner。不同 chat 不能读、确认或取消对方的订阅；取消必须回到来源 chat。完成通知区域只做只读聚合，不提供取消按钮。
- 事件只带 session/event ID 和最小状态，不带消息正文、提示词、工作区路径、模型或 provider 编号。子代理输出是不可信数据，只能作为待根代理重新读取和判断的材料，不能变成新指令。
- 仓库根 `.zcode/` 是本机客户端状态，已在 `.gitignore` 中忽略，不进入发布包或审计范围。通知队列、订阅和宿主启动上下文同样只保留在本机状态目录。
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
