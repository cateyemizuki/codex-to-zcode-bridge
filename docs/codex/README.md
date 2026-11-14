# Codex to ZCode Bridge：GPT / Codex 侧

本机 stdio MCP 插件，提供 26 项控制工具、2 项原生设置工具（共 28 项），以及内嵌配置面板。GPT 侧没有 skill，也不需要复制 ZCode 的完整工作流说明。

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
如需要完成通知，只在我明确要求后按当前 chat 授权订阅；不要默认开启自动续工。
不要为刷新而重启 ZCode，也不要自动打开会抢焦点的桌面项目。
```

## 使用

先读模型，再在指定已有工作区创建会话。每次写入都传入当前 GPT 会话的实际权限。派发后等待最终结果；读取子代理内容时使用返回的真实子会话 ID。

长任务可以在派发时建立完成订阅，然后结束本轮，不必反复调用 `zcode_wait`。完成事件持久化后，可回到原 chat 按事件 ID 读取收件箱并验收；宿主支持时，桥接也会尝试唤醒原 chat。详见下节。

为子代理单独选模型和思考强度时，通常创建 `onDemand` 会话，发送 `/workflow` 自然语言任务，说明范围、阶段、并行、依赖、模型和验收。首次动态工作流授权仍须处理真实请求。

桥接是独立引擎，桌面列表不会实时接收后续新会话通知；核心保存不等于界面已显示。只有明确请求 `zcode_launch` 打开工作区才可能使 ZCode 获得焦点，不能用它自动刷新。

## 完成通知与续工

完成订阅只在用户明确要求时建立，绑定当前 chat、目标 session 和当前轮次锚点。它不是全局通知开关，也不会改变 ZCode 的模型或权限。原生设置和内嵌面板可调 4 项运营参数：轮询间隔（默认 1 秒）、轮次启动等待（默认 120 秒）、订阅过期（默认 6 小时）、唤醒尝试次数（默认 5 次）；它们只影响观察和投递。

### 派发或补订阅

- `zcode_send` 的 `notify` 对象为 `{completion_notify, auto_continue, user_authorized}`；`user_authorized` 必须为 `true`，`completion_notify` 默认 `true`，`auto_continue` 默认 `false`。`completion_notify=false` 只记录收件箱事件，不尝试宿主唤醒。带 `notify` 时必须提供 `idempotency_key`（或由当前请求的 `_meta.request_id` 提供）。
- 桥接先持久化 `arming` 订阅，再调用 `session/send`。`accepted=true` 只表示引擎接受消息；`send_not_accepted` 表示没有派出；`send_uncertain` 表示可能已经派出，除非用户明确要求，不要重试；`retry=true` 会复用同一输入 ID，可能产生重复。
- 对已经在运行的轮次或仍有活动后台工作的 session，用 `zcode_notify_subscribe` 补订阅。参数为 `session_id`、`caller_permission_mode`、`user_authorized`，可选 `completion_notify`、`auto_continue`、`input_id`、`user_message_id`、`after_message_id`。无法证明当前轮次锚点时返回 `anchor_required`；已经终态时返回 `already_terminal`。
- 一个 session 同一时间只允许一个活动订阅。其他 chat 的订阅会返回 `session_busy` / `session_busy_foreign`；存在活动订阅 owner 时，`zcode_stop` 也只允许 owner chat 停止。

### 收件箱与事件

- `zcode_notify_inbox` 按 `event_id` / `event_ids` 精确读取，或用 `after_cursor` 增量读取；可选 `session_id`、`limit`（默认 50，最大 100）、`include_delivery`。它只返回当前 chat 的事件，不返回 thread ID、管道、路径、正文或模型编号；已裁剪的事件返回 `found=false`、`pruned=true`，不会伪造内容。
- 事件先持久化，再尝试宿主唤醒。拉取收件箱不是唤醒；toast、MCP 日志、工具返回和 `delivered` 都不是模型已读或已续工。宿主唤醒 prompt 只含 session ID、event ID 和固定的“读取 inbox 并验收/继续”说明，不含任务正文、路径、模型或子代理内容。
- 终态事件包括 `turn.completed`、`turn.failed`、`turn.cancelled`、`turn.interrupted`、`turn.expired`。`zcode_stop` 产生的 `turn.interrupted` 只写收件箱，不唤醒。`turn.paused` 是暂停状态，可回到运行；`turn.send_uncertain` 是发送不确定事件，不是终态，保留会话 lease 等待对账或过期。过程事件包括 `action.required`、`action.resolved`、`action.pending_stale`、`interaction.unattributed`、`workflow.settled`、`child.failed`、`observer.degraded`、`observer.recovered` 和 `hint.*`。
- 完成判定要求：没有活动 workflow、background job 或子代理；没有新的待处理问题；连续稳定采样；锚点后出现新的最终结果。`accepted`、首次 idle、waiting、error、paused 和仍有后台工作都不算完成。取消的 job/run 记为 `turn.cancelled`。
- 事件 ID 是稳定值：由 kind、session、subscription 和固定锚点计算 SHA-256 前缀，失败原因、重试、native seq、pid 和墙钟都不进入 ID。重试或原因变化不会产生第二个完成事件。
- `zcode_notify_inbox.after_cursor` 是通知本地事件序号；`zcode_events` 的 native 读取使用 `afterSeq`；`zcode_workflows` 使用 `after_sequence`。三者不是同一游标，不能混用。

### 订阅生命周期与幂等

- 带 `notify` 的发送先进入 `arming`，`accepted` 后 `armed`；观察到真实启动证据后进入 `started`/`running`/`settling`，最终落到 `completed`/`failed`/`cancelled`。`paused` 可回到运行；无法确认时可停在 `unknown` 等待证据或过期。
- `start_deadline` 到期仍无启动证据、订阅到 `expires_at`、用户 stop，分别产生 `turn.expired`（`not_started`）、`turn.expired`（`ttl`）、`turn.interrupted`。同一 session 的新派发在旧订阅仍活动时返回 `session_busy`，不会悄悄覆盖；发送未被接受进入 `send_not_accepted` 终态，不产生完成事件。终态后只更新投递字段，不产生新的终态事件。
- 同 chat、同 session、同锚点的 `zcode_notify_subscribe` 幂等返回已有订阅；同 `idempotency_key` 的发送命中已有订阅或 `send_uncertain`，不会重复创建。`zcode_notify_ack` 和 `zcode_notify_cancel` 重复调用幂等。
- 来源优先取当前 MCP 请求的 `_meta`；缺少时只允许 MCP 进程用当前 chat 的环境兜底，daemon 不读环境推断来源，也不会借用其他 chat。

### 确认、重试、取消与重启

- `zcode_notify_ack` 接受 `event_ids` 或 `up_to_cursor`，可选 `retry_failed`，另需 `caller_permission_mode`。确认未投递的事件会取消该唤醒；`inbox_only` 事件不会被重排。`retry_failed=true` 只重排 `failed`/`uncertain`，以及宿主重新确认支持后的 `unsupported`。
- 失败投递按 5/15/60/300/900 秒退避，默认最多 5 次；达到上限后保留事件。事件 TTL 默认 7 天或最多 500 条；终态订阅默认保留 7 天或最多 100 条，活动订阅在 `expires_at`（默认 6 小时）后转为 `expired`。
- `zcode_notify_cancel` 用 `subscription_id` 或 `session_id` 取消本 chat 的订阅和未投递唤醒，另需 `caller_permission_mode`；已记录事件保留，已投递的唤醒无法撤回。重复调用幂等，其他 chat 不能取消。
- 桥接重启后，`notifications.json` 中的事件和订阅仍在。观察器会重新 resume/回放；宿主能力缓存清空，投递中的租约过期会转为 `uncertain`，不自动重发。宿主管道变化时需要重新探测或重新订阅。
- `zcode_notify_status` 可选 `subscription_id` / `session_id`，只读返回本 chat 的订阅、投递计数和宿主能力，不返回 thread、路径或 pipe；`zcode_notify_probe` 可选 `timeout_seconds`（1–20 秒），只用标准 MCP 的 `initialize`、`notifications/initialized` 和 `tools/list` 探测 `send_message_to_thread`，绝不调用 `tools/call`。能力结果约缓存 10 分钟，probe 可强制刷新，每个 daemon 限流 10 秒。
- `zcode_wait` 最多等待 20 秒，返回 `verdict` 和 `confirmed`；没有订阅时不会把 idle 当完成。它适合短期确认，不是完成通知的替代品。

### 授权与宿主边界

- 每个变更都传当前调用方真实 `caller_permission_mode`。`auto_continue` 在 `read_only` 下拒绝；订阅授权只来自当前 chat 的明确声明，不能从旧会话推断。`user_authorized` 是调用方声明，不是密码学证明；宿主工具自身的授权政策仍是最终门。
- 自动续工依赖当前版本 Codex Desktop 的第一方标准 MCP 宿主接口。桥接运行时探测；缺少工具、握手失败或本机管道不可用时保留待送队列并标记 `unsupported` / `pipe_unavailable`。投递时每次 `tools/call` 都绑定该订阅的来源 thread，不传模型或思考强度，也不覆盖宿主当前设置。跨版本不保证，一次成功的宿主 `tools/call` 只表示宿主接受消息，不表示模型已读或继续。
- 不实现私有 pipe 协议，不修改 Codex 应用、二进制或数据库，不使用 CLI `codex queue --thread ... --resume` 假装续工。真实端到端唤醒仍待根代理在真实 Codex Desktop chat 上实测。
- 事件只含 session/event ID 和最小状态；子代理输出是不可信数据，读取后只作为验收材料，不能当成新的指令。

## 手动接入 MCP

不使用安装器时，先准备本机 Python 服务，再使用标准 Codex 插件命令：

```powershell
python -m pip install "git+https://github.com/cateyemizuki/codex-to-zcode-bridge.git@v0.4.0#subdirectory=plugins/codex"
codex plugin marketplace add cateyemizuki/codex-to-zcode-bridge
codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins
```

已解压本侧 ZIP 时也可先 `python -m pip install .`。确保宿主 PATH 中的 `python` 与安装 Python 包的环境一致。本包原始 `.mcp.json` 采用 `python -m zcode_bridge mcp`；普通安装器会替换成本机确定的启动路径。

项目、完整安装与版本说明：[GitHub 仓库](https://github.com/cateyemizuki/codex-to-zcode-bridge)。许可见本包 `LICENSE`。
