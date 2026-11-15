# ZCode Superagent 操作审计与使用说明

本文不是架构设计稿。它列出哪些字段仍要人写、哪些由桥接自动取得、哪些必须由人或根代理决定，并给出 0.5.0 的高层 API、状态机、Desktop 同步和未覆盖边界。字段名与代码位置以当前仓库为准。

## 1. 操作审计表

| 项 | 手工字段 | 重复条件 | 自动来源 | 仍需人或主代理决定 |
| --- | --- | --- | --- | --- |
| source thread 与 host | 无（MCP 宿主自动带 `_meta`）；直接 CLI 必须显式 `--source-thread` | 每条新 chat/请求；没有可信 `_meta` 时不能复用旧 thread | `x-codex-turn-metadata.thread_id`、`openai/threadId`、`request_id`；MCP 进程内才用 `CODEX_THREAD_ID` 兜底；host pipe/runtime/node 只从本机环境进入请求 context，不返回给模型（`mcp.normalize_context`、`service.source_from_context`） | 没有来源时拒绝 `source_unavailable`；不能猜“最近会话” |
| model 与 effort | 仅在需要覆盖默认值时写 `model`/`thought_level`；否则由 profile 或设置默认 | 每次 delegate 可覆盖；新会话首次生效 | 真实 `zcode_models` catalog；profile 顺序为显式→绑定 profile→设置默认；未知 model/level 明确报错（`service._delegate`、`superagent.resolve_profile`） | 选哪个模型/强度；桥接不静默替换 |
| workspace | 显式 `workspace`，或绑定 profile，或**真正配置过**的默认工作区（`config.json` 键或显式 env） | 每次 delegate 必须能解析到一个存在目录 | 显式→profile→已配置默认；发现层的 `Path.home()` 兜底不算配置；拒绝插件安装目录（`service._delegate`、`superagent.resolve_workspace` / `_installation_roots`、`storage.configured_default_workspace`） | 用户项目目录；缺失时 `workspace_required`，不猜 |
| permission | 每次 mutation 传当前 `caller_permission_mode` | 每个新请求/turn 重新声明；同一请求内可复用已声明值 | 不自动读取 GPT 权限按钮；job 记录 `explicit_caller` 与来源 origin；无全局 full_access 默认（`service.caller_permission`、`superagent.normalize_source`） | 当前权限等级；拿不准用 `always_ask` |
| notify consent | `notify.user_authorized=true` 才是用户授权；`completion_notify`/`auto_continue` 选择 | 每次订阅/派发；不能从旧会话推断 | 授权持久化在订阅；未授权返回 `armed=false` 和原因；`auto_continue` 在 `read_only` 拒绝（`service._notify_consent`、`service._delegate`） | 是否要完成通知/自动续工；真正用户意图 |
| idempotency | 可选 `idempotency_key`；不写时插件生成 | 同一逻辑派发重试才复用；消息级 key 同理 | 生成 `idem_*` 并持久化；同 source thread+key 返回同一 job；同一 key 不重复派发/投递（`superagent.create_job`、`superagent.find_by_idempotency`、`superagent.enqueue_message`） | 只有明确知道要重试时才复用/更换 key；uncertain 不自动重发 |
| busy 跟进 | `zcode_agent_message` 的 `content`；可选 idempotency_key | 每次跟进一条；忙碌时进入队列 | 前台 idle 才发送，后台 workflow/job 不阻塞纠偏；队列上限 20、单条 20 万字符；同 key 不重复投递（`service._drain_job`、`superagent.enqueue_message`） | 是否继续等、纠偏内容、是否取消 |
| cursor | 无 | 每次读取可带上次返回的 cursor/anchor | `zcode_events` 的 native `afterSeq`、`zcode_notify_inbox` 的本地 cursor、`zcode_workflows` 的 `after_sequence` 是不同命名空间；不得混用（`service._events`、`docs/WORKFLOWS.md`） | 选择读消息、事件还是工作流；不能把本地 cursor 当 native 序号 |
| 版本字段 | 发布时人工审核版本/哈希 | 每次安装/升级/补丁 | `runtime/__init__.py` 与 `build.py` 是版本源；Desktop shim 另有 3.18.2 版本与 SHA-256 门禁（`build.py`、`scripts/desktop_sync_shim.py`） | 是否升级、是否接受实验性补丁；不猜版本号 |
| 未开始 subagents | 无；新会话首轮前引擎可能没有 parent 行 | 只在“本代创建、turnCount=0、idle、无 workflow/job”的首个通知发送时豁免 | 仅精确 `Session not found: <SID>` 被视为尚无子代理；已有轮次/后台/恢复/未知会话严格失败（`service._baseline`、`service._is_unstarted_created_session`） | 是否等待首轮或重新读取；不能把错误当成空列表 |
| 失效会话 | 无 | 每次读/恢复时 | 只对本桥接已加载/创建的同一 SID 的只读 `session/read`、`session/messages`、`session/events` 做一次 native resume 后重试；发送/改模式/停止/回答不重放（`service.READ_ONLY_SESSION_RPCS`、`service.rpc`） | 恢复失败时是否新建会话；不允许隐藏 resume 未知 SID |
| 超时不应静默 | `timeout_seconds`/订阅过期参数 | 每次等待/订阅 | `zcode_wait` 超时返回 `verdict=unknown, confirmed=false, reason_code=timed_out`；订阅过期映射 job `expired`，`success=false`（`service._wait_verdict`、`service._verification_for_subscription`） | 继续等、取消或重新派发；超时不是完成 |
| 检查结果与 GUI 证明不同 | 无 | 每次验收 | `desktop.indexed`/`workspace_registered` 只来自 tasks-index 与 setting.json，不等于侧栏可见；可选 sync 只按 allowlist 请求索引/reconcile（`desktop.session_visibility`、`desktop_sync`） | 需要肉眼 GUI 验收；不能用 indexed=true 冒充页面已刷新 |
| Desktop sync 状态 | 启用开关、私有 control 文件、间隔 | 每次修改配置；每次派发/终态自动触发 | 默认关闭；只读私有 control.json；allowlist 规范化；最多 50 个 session；异步按 workspace 节流；token 只在内存（`storage.defaults`、`desktop_sync`） | 是否启用、允许哪些工作区；GUI 最终视觉 |

## 2. 高层 API（旧 28 工具仍可用）

| 工具 | 作用 | 关键返回 |
| --- | --- | --- |
| `zcode_delegate` | 一次创建+派发；`notify.user_authorized=true` 时原子订阅 | `agent_id`、`job_id`、`session_id`、解析后的 model/workspace、`subscription`（未授权 `armed=false`+原因）、`delivery`、`cursor`；`send_uncertain` 原样返回 |
| `zcode_agent_message` | 跟进/纠偏；忙碌时有界持久队列 | `queued`+position / `delivered` / `uncertain`；同 key 不重复投递；不 poll、不递归建会话 |
| `zcode_agent_inspect` | 紧凑聚合 | job 状态、`verdict`/`terminal`/`success`、最近消息 id、真实 pending `request_id`+options、subagents/workflows 摘要、订阅/队列/事件、observation/read_failures、desktop_sync 状态；默认无正文。`include_report=true` 仅 owner 可读有界 untrusted 报告载荷 |
| `zcode_agent_answer` | 回答该 agent 会话/已知子树中的真实请求 | 单题可直接传 `answer="实际选项 label/value"`，桥接校验唯一选项并生成原生 `response={action:accept,content:{answers:{题面:值}}}`；多题/多选/自由文本需显式 `response`，权限请求需真实 `option_id`，`answer` 与 `option_id`/`response` 冲突会拒绝。`request_not_pending`（过期）、`request_not_owned`（越权）、`user_answer_required`（认证/真实偏好）；真实 option 仍由 `zcode_answer` 校验 |
| `zcode_agent_cancel` | 只取消该 job：先按该 session 自己的 `backgroundJobs` 精确 taskId 调 `session/cancelBackgroundTask`，等子树收敛，再停止前台轮次、取消订阅、关闭队列 | 只在**重新读取确认**无活动 background/workflow 后才 `cancelled=true`；取消失败或无法映射到 exact taskId 时返回 `pending=true` 与 `remaining`，不调 `session/stop` 冒充已取消；共享 session 拒绝 |
| `zcode_agent_report` | 子端窄报告通道 | 只需 `agent_id` + `result`；`session_id` 缺省使用 job 绑定会话，显式不匹配仍拒绝。记录 `unverified_hint` 的有界结构化载荷（≤16KB、截断标记、`untrusted=true`）；返回 `sender.attribution`，来源不可证（含 env fallback）标 `unverified`；事件不含正文；默认不可见，仅 owner 用 `inspect(include_report=true)` 读取；不宣告完成、不授予权限、不替代原生提问/权限通道 |

旧工具：`zcode_status/models/create_session/sessions/read_session/send/messages/subagents/set_model/set_permissions/pending/answer/wait/events/workflows/stop` 与 6 个通知工具、`zcode_settings`、`zcode_open_settings` 全部保留。MCP 工具总数为 34（32 控制 + 2 原生设置）。

## 2.1 会话后端：独立引擎 / 桌面原生（实验）

- 选择方式：`zcode_create_session` / `zcode_delegate` 的 `transport=engine|desktop_native`，或 profile 的 `transport`，或本机 `default_transport`。旧 profile 没有该字段时仍是 `engine`，旧 28+ 工具行为不变。
- 绑定按 SID 持久化在 `sessions.json`。已经创建/绑定的 SID 按自己的 `transport` 路由，不会因为当前全局设置改变而重判；独立引擎 SID 不会被原生后端接管，原生 SID 也不会被旧同步或独立引擎冷恢复。
- `desktop_native` 只在显式启用 `desktop_native_enabled` 且配置私有 `desktop_native_control_file`（其中 `nativeTransport.enabled=true`）时工作；host 未就绪、`unsupported`、`stale_owner`、`unknown` 均明确返回，不退回独立引擎、不自动打开项目/抢焦点/重启、不恢复 foreign SID。token 不进入响应、日志、提示词或仓库。
- 原生请求按实际源码：每条带 `workspace` 和当前 `caller_permission_mode`；`native_model_catalog` / `native_create` / `native_send` / `native_resolve` / `native_cancel` 带稳定 `request_id`。`native_snapshot.result` 是 snapshot 对象，`native_messages.result` 直接是数组；`native_pending` 只是写请求结果查询，不是交互问题队列。问题从 snapshot 的 `pendingInteractions`/`pendingPermissions` 读取，用真实 interaction/option ID 回答。
- 原生发送前若当前权限低于会话原权限，按当前值降级并校验；未知写结果不自动重发。ACK 归一化按真实形状：首发 `status='accepted'` + `nativeAdmission.accepted=true`，后续 V4 `status='accepted'`/`inputAccepted` 均视为 admission；`replay`/`pending`/`unknown`/`duplicate`/`noop` 不当作 accepted，保持 uncertain，绝不重发。原生暂不暴露 model setter、saved workflow catalog 和 background cancel；这些返回 `needs_native_ui`/`unsupported`。原生 `zcode_agent_cancel` 只做前台 stop；有活动后台任务时返回明确 pending。
- 原生完成通知已接入现有 Observer/Delivery：按 SID 读 native snapshot/messages/events/jobs/workflows/pending，订阅在发送前持久化，admission 后用 host 实际 `commandId`/inputId 替换临时 id，只有真实 `turn.started`/`turn.completed` + 新 assistant 消息 + stable idle 才完成；pending/后台/未证明 anchor/unknown/read error 都不完成。不会为通知偷偷创建独立 engine 或挪用其他 engine 身份。
- `attach_report_mcp=true`（仅 engine）会在 `session/create` 的 `mcpServers` 中附加一个 session-local、只暴露 `zcode_agent_report` 的薄 MCP 入口（`python -m zcode_bridge.report_mcp`，路径由本机 `sys.executable`/runtime/state 动态生成）。它不暴露 delegate/message 等控制能力，不改全局 plugin registry，不自动 grant workspace trust。原生 host 没有 `mcpServers` 参数，因此 native create 对该标志明确 `unsupported`；没接通前不得声称工具已加载。
- 状态：`zcode_status.native_transport` 与设置面板“会话后端”组显示 disabled/not_configured/native_disabled/ready 和最近一次状态；只给状态码，不含 source、token、路径。
- GUI 通过范围：根代理只在其测试的 3.18.2 隔离复制件、当前固定 native op 上验证了“同页先 BEGIN/Bash 等待后自动 END/发送恢复”。公共跨版本仍受版本与 SHA-256 门禁约束；本工作树没有重跑 GUI，也不把该证据外推到其他版本或运行实例。

## 3. Profile 与任务契约

- Profile 解析顺序：显式字段 → 绑定 profile → 设置默认值。每个字段独立取来源，并在 job 的 `sources`/配置中记录。
- 模型与思考强度必须命中真实 `zcode_models` catalog；未知模型、缺失 catalog、模型不支持的强度分别返回 `model_not_in_catalog`、`catalog_unavailable`、`thought_level_unsupported`，不静默替换。
- workspace 只来自显式参数、profile 或真正配置过的默认工作区（`config.json` 的 `default_workspace` 或显式 `ZCODE_WORKSPACE`/`ZCODE_DEFAULT_WORKSPACE`）。发现层算出的 `Path.home()` 兜底不算配置；必须存在，且不能是插件安装目录（源码树的 `runtime/`、`plugins/` 与 `plugins/<host>` 同样拒绝）。
- 契约 `superagent-contract-v1`：首次/修订附完整契约（agent/job、范围与完成条件、当前配置、报告/提问方式、结构化最终结果字段：摘要/实际改动/检查与证据/未验证项/阻碍）；后续消息只附短引用。任务原文原样保留；`/workflow` 任务第一行仍是 `/workflow`，契约附在其后，不注入完整 DWF skill。
- 平台约定：原生 DWF 子代理没有 AskUserQuestion 工具，不得假装能直接问人。它应把问题、选项与阻塞点以结构化 `needs_input` 返回给 ZCode 主会话；主会话用原生 AskUserQuestion 向外部父代理转交，收到答案后带着继续，不自行代答。报告只需 `agent_id` + `result`，`session_id` 可省略。

## 4. 状态机与自动回传

| 状态 | 含义 | 终态/成功 |
| --- | --- | --- |
| `created` / `dispatching` / `running` | job 已建立/派发中/运行中 | 否 |
| `action_required` | 该 job 会话或已知子树有真实 pending 请求 | 否；`inspect` 给出 request_id/options |
| `reported` | 子端通过报告通道自述；仅 `unverified_hint` | 否；不等于完成 |
| `send_uncertain` | 发送可能已执行；不自动重发 | 否；需 inspect/取消/明确重试 |
| `stalled` | 长时间无可观测进展 | 诊断状态，不是完成 |
| `unobservable` | 连续读取失败/observer degraded 达阈值 | 诊断状态，不是完成 |
| `verified` | 桥接按 0.4 真实终态证据判定该执行轮结束 | 是；唯一 lifecycle 成功终态；不代表自然语言任务成功 |
| `failed` / `cancelled` / `expired` | 失败/取消/超时 | 终态，但不是成功 |

规则：

- 只有已持久化的通知订阅终态 `completed` 才会把 job 标为 `verified`；`accepted`、首次 idle、waiting、paused、error、协议错误、仍有 background/workflow 都不算完成。
- `verified`/`success` 的机器验证范围固定为 `verification_scope=turn_lifecycle`：只证明该执行轮已结束，不证明自然语言任务成功。`inspect` 与 job 摘要同时给出 `task_success=null`、`task_review=needs_parent_review`；父代理必须读 `include_report=true` 的结构化结果并自行复核业务目标。
- 没有通知订阅的 job 也会按节流读取真实 session 投影：可持续进入 `stalled`（无进展）、`unobservable`（连续读取失败）、`failed`（引擎 error / 失效会话）或 `cancelled`（引擎取消）。引擎看起来 idle 时只给 `verdict=engine_idle_unverified`、`confirmed=false`，绝不据 idle 标 `verified`；自动宿主唤醒仍然只属于显式 notify 授权。
- 同一 job 同一 kind/reason 的 job 事件只写一次；普通进度只更新 `last_progress`，不产生 job 事件。`stalled`/`unobservable` 额外发一次稳定 hint 事件，避免唤醒风暴；无订阅时只写本地诊断状态，不触发宿主唤醒。
- 待回答归属 job 会话 + 订阅 `known_child_ids`；未知 session 有 pending 时按 5 秒节流从 DWF 拓扑发现 actor，之后 inspect/answer/auto-return 都能归属。
- `zcode_agent_inspect` 在本轮读取失败/进度写入之后重新读取 job，因此同一响应里的 `job.read_failures`/`observation` 与 `errors` 一致。
- 事件 ID、订阅 ID、输入 ID 稳定；重启后读取同一状态；超时/过期只映射 `expired`，不会静默成功。

## 5. Desktop 同步（可选，默认关闭）

配置（本机 `config.json`，可由原生设置/内嵌面板修改）：

- `desktop_sync_enabled`：默认 `false`。
- `desktop_sync_control_file`：私有 `control.json` 路径，默认空；token 只从该文件读取，不进入设置值、工具响应、提示词或日志。
- `desktop_sync_min_interval_seconds`：默认 5，范围 1–300。
- `superagent_stall_seconds`：默认 600，范围 60–86400。
- `superagent_read_failure_threshold`：默认 5，范围 2–50。
- `default_transport`：默认 `engine`；可选 `desktop_native`（实验，失败不回退）。
- `desktop_native_enabled`：默认 `false`。
- `desktop_native_control_file`：私有 control.json 路径，默认空；必须已启用 `nativeTransport`，token 不进入设置值或响应。

前置步骤（可选，必需时才做）：

1. 用 `scripts/desktop_sync_copy.py prepare` 复制并打补丁到隔离副本；版本、host/engine SHA-256、锚点数量任一不符即拒绝。补丁工具不在默认安装包内，需从仓库取得。
2. 用 `scripts/desktop_sync_shim.py configure --workspace <允许的工作区> --port 0` 生成私有 `control.json`；allowedWorkspaces 必须显式列出。
3. 在插件设置中开启 `desktop_sync_enabled` 并把 `desktop_sync_control_file` 指向该私有文件。
4. 正常派发/跟进/终态后，适配器异步按 workspace 合并请求；最多 50 个 session；配置/token/endpoint 失败只记录状态，不影响业务。

探针命令（不会打开项目、不会抢焦点）：

```powershell
python scripts/desktop_sync_probe.py --control <private>/control.json --op status
python scripts/desktop_sync_probe.py --control <private>/control.json --op probe
python scripts/desktop_sync_probe.py --control <private>/control.json --op sync --workspace <allowed-workspace> --session-id <session-id> --confirm-sync
```

GUI 肉眼验收步骤与预期：

1. 用户明确要求后，手动启动隔离副本（`desktop_sync_copy.py launch ... --execute`）；探针脚本本身不启动、不打开、不聚焦。
2. 在隔离副本中打开一个已存在的会话，记录第一条助手回复的可见标记。
3. 通过独立 engine 或 `scripts/superagent_live_check.py --confirm-live ...` 向该会话写入第二条带唯一标记的消息。
4. 运行 `desktop_sync_probe.py --op sync`（显式 `--confirm-sync`）。
5. 预期：同一打开页面在无导航、无刷新的情况下出现第二条标记；host 侧发出 `workspace_task_list_changed`；若没有出现，记录 `probe` 的 ready/event 计数和 sync 的 changed/missing/errors，不能声称已刷新。
6. 侧栏项目归属需另行确认；`desktop.indexed=true` 只代表索引收录，不等于项目分组或 GUI 已显示。

### 5.1 会话级 MCP 工具加载（只读源码核查，未实现）

- 3.18.2 的 `session/create` 参数 schema（内部 `Zat`）包含 `mcpServers?: k_e[]`；`k_e` 是**完整 server 定义**的联合，不是已有 server 名选择器：
  - stdio：`{name, scope?, command, cwd?, args: string[], env, isolation?, protocolVersion?, alwaysLoad?, timeoutMs?}`；
  - http/sse：`{name, scope?, type:'http'|'sse', url, headers, oauth?, isolation?, protocolVersion?, alwaysLoad?, timeoutMs?}`。
  - handler 把 `mcpServers` 传给 `createSessionRecord`。
- 原生 `mcp/list`（方法 `mcp/list`，params `{workspace, mcpServers?, mode:'connect'|'status'}`）返回每个 server 的 `status`（含 `connected`/`failed`/`untrusted`）与 `toolCount`，是验证“工具是否真的加载”的正确探针。插件根的 `.mcp.json` 是插件清单，不是 workspace 配置；它不出现在原生 `mcp/list`。
- workspace-local 配置是 `<workspace>/.agents/mcp.json`，格式 `{mcpServers: {name: config}}`；workspace 声明受 trust 门禁：`workspace/mcp/trust` params `{workspace, serverName, declarationDigest, trusted}`，trust 记录在 user config 同目录的 `workspace-mcp-trust-v1.json`。
- **未确认**：per-session `mcpServers` 是否绕过 workspace trust，以及工具是否马上进入该会话的模型工具列表；必须在真实隔离会话用 `mcp/list(mode=connect)` 看 `status`/`toolCount`。
- 最小接线（未实现）：在 create/delegate 入口增加可选配置，把 stdio 定义随 `session/create` 传入，例如 `{name:'zcode-superagent', command:<python>, args:['-m','zcode_bridge.report_mcp'], cwd:<plugin runtime>, isolation:'session', alwaysLoad:true}`；为最小权限只暴露 `zcode_agent_report`，不要暴露完整 `zcode_*` 以免子代理递归派发。不要靠往工作区写 `.mcp.json` 来假装已加载。

## 6. 真实验收入口

- 预检（不创建持久会话，只读版本/工作区/模型）：

```powershell
python scripts/superagent_live_check.py --workspace <existing-workspace>
```

- 真实最小往返（必须显式确认，且给当前 chat/thread；会创建并最终取消一个真实会话）：

```powershell
python scripts/superagent_live_check.py --workspace <existing-workspace> --source-thread <current-thread-id> --confirm-live
```

该入口默认做 delegate → inspect → message → inspect → cancel，不重试 uncertain，不打开桌面项目，不抢焦点。输出 JSON 证据；real engine 未运行或配置缺失时如实报错。

- 真实主会话提问回归（必须显式确认；委托的 ZCode 主会话直接用原生 AskUserQuestion 提问，父侧 inspect 看到真实 pending 后由 `zcode_agent_answer` 回答该 request_id/option，再清理）。原生 DWF 子代理没有 AskUserQuestion，它的契约是向主会话返回 `needs_input` 再由主会话转交，不要把“子代理直接提问”当验收要求：

```powershell
python scripts/superagent_live_check.py --workspace <existing-workspace> --source-thread <current-thread-id> --confirm-live --ask-roundtrip --question-wait 180
```

`--ask-roundtrip` 只回答带 `LIVE-CHECK-QUESTION` 标记的检查问题；遇到权限/认证或真实用户偏好请求时返回 `pending_without_check_question`/`blocked`，不会代答。

## 7. 未覆盖边界

- 本工作树的自动化测试使用 fake engine / 本地回环假服务；真实 ZCode engine、真实 Codex host 唤醒和 Desktop GUI 未在本环境执行。根代理需按上面的命令和 GUI 步骤实机验收。
- 宿主唤醒依赖当前版本 Codex Desktop 的第一方 `send_message_to_thread`；跨版本不保证。`delivered` 只表示宿主接受 `tools/call`，不表示模型已读或续工。
- 未授权 notify 的 job 没有订阅，因此不会自动 `verified`，也不会触发宿主唤醒；但它会持续做本地诊断轮询（stalled/unobservable/failed/cancelled），inspect 会明确给出 `engine_idle_unverified`。`zcode_agent_report` 的验证轮询仍只在存在活动订阅时触发。
- DWF actor 的 pending 归属通过按需拓扑发现；大型工作流的发现受 5 秒节流。原生 DWF 子代理没有 AskUserQuestion，必须 `needs_input` 给主会话转交；`--ask-roundtrip` 验证的是主会话原生提问链路，本工作树未运行真实会话，真实 DWF 转交仍需根代理实机验收。
- `zcode_agent_cancel` 只对当前 session 投影里的精确 taskId 调 `session/cancelBackgroundTask`，并在重新读取确认无活动 background/workflow 后才标 `cancelled`；无法映射或取消失败返回 `pending`，不触碰其他 SID、进程或无关任务。
- Desktop sync 可选适配器默认关闭；当前 shim 的已知限制：它不证明 UI 已实时刷新，强制 hydrate 仍读本进程 `InMemorySessionEventStore`，冷恢复可能把外部所有者的非终态运行标成 stopped。根代理已在其隔离运行副本上加了守卫，但公共源码尚未合并原生 host 控制传输；在根代理完成真实 GUI 验收和 native host 契约合并前，不得用该路径声称“界面已实时同步”，也不得冷恢复外来活动 SID 用于查看。
- 版本、模型编号、工作区路径和 token 都不应写进仓库；本机状态目录中的队列正文、订阅和连接信息只属于该机器。
- `desktop_native` 的自动化测试使用 fake native adapter：验证创建、发送、消息数组、snapshot pending、真实 interaction 回答、同后端路由、权限降级、未知写不重发和不支持项。真实 host 控制通道、真实 GUI 和真实原生问题往返仍需根代理在其隔离复制件上验收；本工作树未创建真实原生会话。
- 原生后端已支持完成通知（按 SID 的 native 观察）；仍不支持 background cancel、model setter 和 saved workflow catalog；这些是明确的 `needs_native_ui`/`unsupported`，不是“已覆盖”。`native_children` 的真实 `{childSessionIds,running,ended:{total,items}}` 已解析并登记只读子 SID；child 消息/快照经 owned parent 路由（DWF actor 另带真实 `run_id`），Host 每次重新验证父/子关系。child 的 send/stop/answer/model/permission 写动作明确拒绝；Host 拒绝或运行时未就绪原样返回，不落到独立 engine，也不冷恢复子会话。
- `attach_report_mcp` 只做了 fake 引擎参数与只读工具列表验证；首次真实加载、原生 host 是否接受 session `mcpServers`、以及工具是否立即出现在模型工具列表，仍需根代理用真实 `mcp/list(mode=connect)` 验收。
