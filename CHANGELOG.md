# Changelog

## 0.5.4 — 2026-11-16

- 默认且仅支持正式 ZCode Desktop 原生会话。公共参数、profile 与配置拒绝独立 engine；旧偏好迁移到原生，新版本不恢复旧 engine SID，底层独立 Client 启动入口也已封闭。
- 复用唯一正式主进程；未运行且允许自动启动时只启动已配置的安装程序。Windows 命名锁协调并发启动，多个主实例、路径或身份不符时停止；状态和后台观察不启动应用。ZCode 自己的 GPU、渲染与脚本工作进程不被误判为第二个主实例。
- 模型目录和状态查询改读 Desktop 宿主，核对其可执行文件身份，不再为查询启动独立引擎。项目可见切换交给原生界面，不再启动转发进程。
- 从原生设置及内嵌面板移除旧 Desktop 补同步选项，并禁止重新启用。原生会话、消息和任务状态使用 Desktop 自己的更新链路。默认工作区黑名单继续只屏蔽 C 盘及其子路径。
- 修复同时存在个人市场与显式市场登记时安装器只检查首项而误报冲突；保留既有登记。
- 584 项回归通过，包含 15 项直接使用生产 Bridge 的桌面唯一性与独立引擎禁用检查；生成包一致性、231 项源码/归档审计及 diff 检查通过。

## 0.5.3 — 2026-11-16

- Desktop native/sync 新控制配置默认采用黑名单，只禁用 C 盘及其子路径；其他现存工作区不再逐项登记。旧白名单保留兼容，新增显式迁移命令，迁移保留认证和 ownership。
- Python 客户端与 Desktop shim 统一校验目录边界、大小写、路径规范化及真实路径；规则每次请求重新读取，损坏或无效规则拒绝访问。
- 新增默认规则、旧配置迁移、实时规则更新、junction 解析和分发审计回归；工作区准入与文件隔离的适用范围见 `docs/WORKSPACE_POLICY.md`。

## 0.5.1 — 2026-11-15

- 修复技术权限被误判为认证问题：`zcode_agent_answer` 不再对整个请求参数序列化后做凭证子串扫描。`interaction/requestPermission` 按真实技术权限处理；用户问题只检查实际题面、header、选项文案与题面自身的 sensitive 标志，工作流脚本、路径、变量名和日志里的 `token`/`password` 等文本不再阻断 `allow_once`。真正的 password/API token/OAuth 问题仍需用户本人回答，未知用户问题结构仍保守拒绝。
- 修复工作流持续推进时误报 `stalled`：父会话 idle 且 messageId 不变时，job 按 run 保存同 epoch 的游标水位，只有稳定 epoch 内严格前进才刷新；不变、倒退、重排、epoch/成员往返或轮询本身不伪造进度，epoch 变更只建立新基线；新 epoch 首 cursor 单独落盘，不刷新 `last_progress.at`、不清除读失败，后续同 epoch 严格增加才是真进度。订阅路径复用观察器持久化的 `run_cursors`，无订阅路径复用已读 run rows，并都参考子会话 ID 与子快照 `eventSeq`/`stateRevision` 摘要。`run.status=running` 本身不会刷新计时，真正无变化仍超时 `stalled`，有新进展时从 `stalled` 恢复。`workflowRunEvents` 读取失败不会被吞成 cursor 0 进展，连续失败仍可达 `unobservable`；成功读取会打断连续失败计数。真实 pending 优先于读失败诊断，终态仍最高优先，通知唤醒边界不变。
- 消息进展只持久化无正文摘要（角色、part 类型、工具名/状态、文本长度与哈希），不保存消息正文、路径、provider 或凭据；观测字段长度有界，超长活动串保留前缀并对完整值做哈希，避免截断漏报。
- 新增 34 项定向回归：engine/native CreateWorkflow 技术权限、真实凭证问题与未知结构的负对照、订阅与无订阅的 cursor 进展/真静默/恢复、同 messageId 内容更新、读取失败不刷新、终态优先。全量 514 项通过。

## 0.5.0 — 2026-11-15

- 根代理独立完成 480 项回归和两侧 ZIP 隐私检查。实机验证原生实际编码任务结束后接回原 Codex chat、真实 AskUserQuestion → `zcode_agent_answer(CHECK-B)` → 同一页工具运行/最终输出、仅一次完成投递、真实 report-only MCP 调用以及 DWF 子代理运行中/完成内容读取；具体层级和未覆盖边界见 `docs/VALIDATION.md`。
- 发送队列在副作用前原子持久 claim，未完成发送在重启后保留 uncertain，避免 crash 后重复投递；send admission 与不可信报告分开。Native 权限回答核对真实 pending 与选项，弱权限 allow 需显式批准，fullAccess 不能越过当前 caller grant，Host 与高层入口均校验，普通 AskUserQuestion 保持可用。
- 修复 native 长会话提前结束分页和进度锚点停在首段：父/子消息使用有界 lookahead 判断真实 hasMore，游标分页保持请求页大小，最近消息与用户锚点取 ready snapshot 尾部；不能读取时保留诊断，不恢复旧引擎。发布扫描新增 JS/MJS/SQL 源码与大写扩展名。

- 新增高层子代理控制面：`zcode_delegate` 一次创建、派发并可选原子订阅；`zcode_agent_message` 忙碌时有界持久队列；`zcode_agent_inspect` 聚合状态/verdict/pending/子代理/工作流/订阅/队列/事件/读取失败，并支持 owner-scoped `include_report`；`zcode_agent_answer` 只回答所属 agent 会话或已知子树的真实请求；`zcode_agent_cancel` 先按精确 taskId 调 `session/cancelBackgroundTask`、确认子树收敛后才取消；`zcode_agent_report` 保留有界结构化不可信载荷。旧 28 个工具保持兼容，MCP 工具总数 34（32 控制 + 2 原生设置）。
- 降低重复手工协调：`zcode_agent_answer` 支持单题 `answer="实际选项 label/value"`，校验唯一真实选项后生成原生 `answers` 包装；多题/多选/自由文本/权限请求仍要求显式 `response`/`option_id`，冲突输入拒绝。`zcode_agent_report` 的 `session_id` 改为可选（agent_id 已绑定 job），返回真实 sender attribution，env fallback 不算验证身份。任务契约增加 DWF 子代理无 AskUserQuestion 时 `needs_input` 交主会话转交的约定。`zcode_agent_inspect`/job 摘要新增 `verification_scope=turn_lifecycle`、`task_success=null`、`task_review=needs_parent_review`，避免把生命周期完成当业务成功。
- 新增可选 Desktop-owned native 后端：`zcode_create_session` / `zcode_delegate` 支持 `transport=engine|desktop_native`（或 profile/本机默认），每个 SID 持久绑定 backend，旧 SID 不按全局设置重判。`DesktopNativeAdapter` 复用私有 control 文件/loopback 鉴权/allowlist，原生 create/send/snapshot/messages/events/workflows/children/resolve/cancel；`native_messages` 直接数组、snapshot pending 真实 interaction、`native_pending` 只做写请求结果查询。默认关闭，失败明确返回 `unsupported`/`stale_owner`/`needs_native_ui`，不回退独立 engine、不打开/聚焦/重启、不自动恢复 foreign SID，token 不进入响应/日志/提示词。Native ACK 按真实形状归一化：首发 `status='accepted'+nativeAdmission.accepted` 与 V4 `status='accepted'/inputAccepted` 为 admission，replay/pending/unknown 保持 uncertain 且不重发；`native_children` 的 `childSessionIds/running/ended.items` 与 `native_workflow_events` 的 `{events,hasMore}` 已接；`stopped` 统一为取消终态而不是 pending/completed。Native 完成通知已接入 Observer/Delivery（per-SID identity/pending、实际 commandId 锚点、pending/后台/unknown 不完成）。仍不支持 background cancel、model setter 和 saved workflow catalog；native child 消息/快照经 owned parent + 真实 run（DWF）只读，child 写动作拒绝且 Host 拒绝不回退 engine；`projection.pendingPermissions` 中的 `AskUserQuestion` 按真实 `input.questions` 识别为 `requestUserInput` 并保留 requestId/toolCallId/input/reason/riskLevel。旧 engine `session/send` 不再传 Bridge 私有的 `caller_permission_mode`（3.18.2 会 -32602），已知 schema 拒绝映射为 failed/not_accepted 而不是 uncertain；未知写不自动重发。
- 新增 `attach_report_mcp`（仅 engine）：在 `session/create` 的 `mcpServers` 中附加 session-local、只暴露 `zcode_agent_report` 的薄 MCP 入口（`python -m zcode_bridge.report_mcp`）；不改全局 registry，不自动 grant workspace trust，native host 无该参数时明确 unsupported。设置面板新增会话后端选择与状态（默认不变），文档区分旧同步与新的 Desktop-owned native 通道。
- 新增持久 superagent 存储：稳定 `agent_id`/`job_id`、source thread 隔离、workspace/model/thought/permission 记录、自动幂等键、队列、cursor、最后可观测进展和连续读取失败计数；同 key 不重复派发/投递，`send_uncertain` 不自动重发。
- Profile 按“显式参数→绑定 profile→真正配置的默认工作区”解析，使用真实 `zcode_models` catalog 校验模型与思考强度；拒绝未知模型/不支持强度；workspace 不再把发现层的 `Path.home()` 兜底当配置，并拒绝源码树 `plugins/` 与安装目录。任务契约 `superagent-contract-v1` 首次/修订完整、后续短引用，保留任务原文和 `/workflow` 首行。
- 自动回传状态机区分 child `reported` 与桥接 `verified`；只有持久通知终态 `completed` 才 verified；新增 `action_required`、`send_uncertain`、`stalled`、`unobservable`；无订阅 job 也做节流诊断轮询，但不能据 idle 变成 verified，inspect 返回 `engine_idle_unverified`；同一 job 同一转移事件去重，普通进度不产生 job 事件；超时映射 `expired`，不静默成功。
- pending 归属覆盖 job session、订阅已知子会话，并按需从 DWF 拓扑发现 actor；未知来源 report 只作为 unverified hint，载荷有界且仅 owner 可读，不宣告完成或授予权限，也不替代原生提问/权限通道。
- 新增默认关闭的可选 `desktop_sync` 客户端适配器：只读私有 control 文件、allowlist 规范化、最多 50 个 session、异步按 workspace 节流合并；配置/token/endpoint 失败只记录状态，不影响业务；token 不进入响应、提示词、日志或仓库；不打开项目/抢焦点/重启实例。创建、输入、delegate、message、cancel 与终态均接入合并 sync。
- 新增配置与可视化：`desktop_sync_enabled`、`desktop_sync_control_file`、`desktop_sync_min_interval_seconds`、`superagent_stall_seconds`、`superagent_read_failure_threshold`；storage 校验、原生设置和内嵌面板都已覆盖。
- 新增真实入口：`scripts/superagent_live_check.py` 默认只 preflight/列模型，必须显式 `--workspace`、`--source-thread`、`--confirm-live` 才创建真实会话并执行 delegate→inspect→message→cancel；`--ask-roundtrip` 进一步验证真实 DWF 子端提问→inspect pending→`zcode_agent_answer`→继续；`scripts/desktop_sync_probe.py` 默认只做状态检查，sync 需要显式 `--confirm-sync`，不打开项目/不抢焦点。
- 新增 `docs/SUPERAGENT.md` 操作审计表、状态机、profile/契约、Desktop 同步与未覆盖边界；操作审计改用稳定函数名而不是易漂移行号；Desktop shim 文档明确当前不是实时外来所有者传输，默认关闭并等待根代理 native host 契约合并。自动化测试增至 417 项。

## 0.4.0 — 2026-11-14

- 修复通知观察器的真实接线，并以 3.18 原生轮次事件关联输入与消息；兼容对象和 JSON 字符串形式的来源 metadata，排除隐藏提醒及系统时间线造成的错误完成判定。
- 修复引擎仍在线但会话已卸载时的过期驻留缓存：只对已加载会话的安全读取做一次原生重新加载和重试，不重放发送或权限变更。初次发送前，只在严格证实全新未开始会话时处理尚不存在的子代理存储记录。
- 新增持久完成订阅、收件箱和稳定事件 ID：订阅绑定 session、轮次锚点和来源 chat，事件先落盘再尝试投递；`zcode_notify_subscribe`、`zcode_notify_inbox`、`zcode_notify_ack`、`zcode_notify_status`、`zcode_notify_cancel`、`zcode_notify_probe`、`zcode_notify_emit` 共 7 个新工具，MCP 工具总数为 28（26 项控制 + 2 项原生设置）。
- `zcode_send` 支持 `notify`、`idempotency_key` 和显式 `retry`；先持久化 arming 再发送，accepted 只表示引擎接受。`zcode_wait` 改为最多 20 秒的 `verdict`/`confirmed`，没有订阅时不会把 idle 当成完成；`zcode_stop` 只允许订阅 owner chat 停止。
- 完成判定覆盖 session、workflow、background job、子代理和 pending：accepted、首次 idle、waiting、error、paused、仍有运行中的后台工作或问题等待都不算完成；取消的 job/run 记为 `turn.cancelled`。`workflow.settled` 只是单个 run 的收件箱事件，不等于整轮完成。
- Codex Desktop 唤醒适配器使用第一方标准 MCP stdio，运行时探测 `send_message_to_thread`；每次 `tools/call` 绑定来源 thread 的 metadata。缺少工具、握手失败或本机管道不可用时保留待送队列并标记 `unsupported` / `pipe_unavailable`；跨版本不保证，2026-11-14 的实机验收已验证真实任务完成后接回同一会话。
- 失败投递按 5/15/60/300/900 秒退避，默认 5 次；事件默认保留 7 天或 500 条，订阅默认 6 小时过期。支持幂等重试、过期、取消、重启恢复和重新订阅；`send_uncertain` 不自动重发。
- 原生设置和内嵌面板新增 4 项完成通知运营参数（轮询间隔、启动等待、过期小时、唤醒尝试次数）及只读宿主能力聚合；不提供全局唤醒/自动续工开关，也不提供面板取消。自动续工只在单订阅显式授权且非 `read_only` 时启用。
- 通知状态保存在本机 `notifications.json`，事件只含 session/event ID 和最小状态；来源 thread、宿主启动上下文、消息正文、提示词、路径、模型与 provider 编号不进入事件、日志或工具返回。`.zcode/` 为本机状态，不随仓库或发布包分发。
- 不实现私有 pipe 协议，不修改 Codex 应用、二进制或数据库，不使用 CLI `codex queue --thread ... --resume` 假装续工；子代理输出按不可信数据处理。

## 0.3.1

- 消息读取兼容 ZCode 3.17 的 `info.id` 与 3.18.2 的 `info.messageId`；`zcode_messages` 的 `after_message_id` 使用同一解析，不再因字段改名变成 null；两者都没有时保留调用方传入的 anchor，不伪造 ID。
- `build.py` 写生成文件时显式使用 LF；修复 Windows 上 `build.py --check` 因 CRLF/LF 误报不一致的问题。
- 明确实时 UI 限制：独立引擎仍不会向已打开的 Desktop 项目推送新会话或新消息；`desktop.indexed` 只表示索引收录，不等于侧栏当前可见。本版本不声称解决实时刷新。
- 工具数量保持 21 项，未引入通知分支功能。

## 0.3.0

- 从本机定制版整理为两个独立、可移动的分发包：GPT / Codex 侧与 ZCode 侧。
- 安装时发现本机 ZCode、Node、Python 与工作区，或使用用户显式参数；发布清单不含固定个人路径。
- 移除发布者的默认模型编号。先读取真实模型，再选择或显式指定；支持使用模型默认思考强度。
- 提供总 README、两侧 README、手动与 agent 安装流程，以及按运行环境分类的 AGENTS.md。
- 使用同一份规范运行时代码生成两侧插件，增加生成一致性和归档隐私检查。
- 保留原生设置、内嵌配置面板、权限同步、问题授权、主/子代理消息及动态工作流操作。
- 保留独立桌面索引状态和显式焦点选项；记录桌面实时同步限制。

## 0.2.x 本机原型

验证了实际会话派发、个人模型配置、消息读取、授权、并行工作流与桌面列表导入。旧本机安装记录、原始会话、凭据和临时修复脚本不随公开版本发布。
