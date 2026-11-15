# 0.5.0 验证记录

2026-11-15，Windows、ZCode Desktop 3.18.2、当前 Codex Desktop。根代理在委派编码后独立执行下列回归与实机验收；子代理自述不计作实机证据。

## 本轮实际执行

- `python -m unittest discover -s tests -q` → **Ran 480 tests in 79.666s，OK**；Node 行为用例 **42 项**包含在该入口中。
- `python build.py --check` → **Both generated plugins match the canonical sources**。
- `python scripts/audit_release.py` → **{"ok": true, "files_checked": 205, "errors": []}**。
- `python build.py --archives` → 两侧 **0.5.0** 独立 ZIP，源码与生成包一致。

实机验收使用单独的冻结 controller 与私有运行状态，未使用旧的已连接 MCP 冷恢复原生 SID。实验性 Host 在完整复制件内运行，原安装保持原样。下列结果只覆盖这一版本和实例，不代表任意版本均兼容。

## 本轮真实往返

| 检查 | 实际证据 |
| --- | --- |
| 原生实际编码任务 | 通过 `zcode_delegate(transport=desktop_native)` 选择真实配置模型与 high 强度，原生会话执行代码修改和测试，根代理独立复核最终文件与回归 |
| 原生完成后同一 Codex chat 续工 | 该编码任务的完成事件 `turn.completed/idle_stable` 持久化；来源 chat 已结束本轮后被第一方宿主接回；根代理读真实结果并确认事件，仅投递一次 |
| 原生问答高层 API | 真实 `AskUserQuestion` 出现在 `projection.pendingPermissions`，工具返回实际问题、选项和 request ID；`zcode_agent_answer(answer="CHECK-B")` 收到 accepted，随后新消息回显实际 CHECK-B |
| 同一页实时输出 | 通过 Windows 界面检查打开上述会话，依次看到 BEGIN、问答面板、ANSWER CHECK-B、Bash 等待 45 秒的运行状态和 END CHECK-B；回答后未重载、未切换会话 |
| 问答后完成通知 | 同一轮最终 `turn.completed` 被投递一次；根代理按精确 event ID 读取并 ACK，消息尾部与真实选项一致 |
| 独立 engine 报告工具 | 新会话附加 report-only MCP，真实调用 `zcode_agent_report` 完成；owner inspect 返回与唯一标记一致的结构化载荷，仍标记 unverified hint，没有冒充任务成功 |
| 原生 DWF 子代理读取 | 一名真实子代理输出 LIVE、运行约 120 秒的工具，再输出 DONE；固定 Host child 接口通过 parent/run/actor 与真实 SDK 行校验读取运行中及完成内容。最终 Python child 路由另有回归测试，未把单层 Host 验收写成所有入口均已实测 |
| 精确后台取消 | 独立 engine 通过原生后台取消操作终止该任务的等待进程；不相关对照进程仍在，没有使用系统级强杀。`stopped` 在回归中归类为 cancelled |
| 长会话分页 | 真实保存快照有 281 条 raw、129 条 visible；只读回放为 `100/hasMore=true → 29/false`，无丢重，最终 assistant ID 与 snapshot 尾部一致 |

模型 provider ID、会话 ID、工作区路径、私有控制配置、事件及截图留在本机验收记录，不进入公开仓库或 ZIP。

## 0.5.0 新增覆盖

- 高层 API：`zcode_delegate` 的创建/派发/原子订阅、幂等与冲突、未授权 `armed=false`、`send_uncertain` 不重发；`zcode_agent_message` 的忙碌队列、重启恢复、同 key 不重复投递、uncertain 不重试；inspect 的 pending/verdict/无正文/本轮读取失败回写/`verification_scope=turn_lifecycle`；answer 的归属与认证拒绝、单题 `answer` 便捷参数、真实选项校验、多题/多选/自由文本/权限请求拒绝、`answer` 与 `response`/`option_id` 冲突；cancel 对精确 taskId 的 `session/cancelBackgroundTask`、子树收敛后才 `cancelled`、取消失败/无 taskId 时 pending 且不调 `session/stop`；report 只记录 unverified hint，`session_id` 可省略、显式不匹配仍拒绝、env fallback 标 unverified，载荷有界且默认隐藏。
- 任务契约：`needs_input` 由 DWF 子代理返回主会话、主会话用原生 AskUserQuestion 转交；报告仅需 `agent_id`+`result`；`/workflow` 首行保留。
- Profile 与契约：显式→profile→真正配置的默认工作区解析、真实 catalog 模型/强度校验、workspace 拒绝源码树 `plugins/` 与安装根、fresh config 拒绝 home 兜底、自动幂等键、契约首次/续用/修订、`/workflow` 首行保留。
- 无订阅诊断：`stalled`/`unobservable`/`failed`/`cancelled` 可达；引擎 idle 只给 `engine_idle_unverified`，不会被标成 `verified`。
- 自动回传状态机：`reported` 与 `verified` 分离；completed/failed/expired 终态映射；action_required、unobservable、stalled；同一 job 事件去重；普通进度不产生 job 事件；超时/过期不静默成功；DWF actor pending 归属。
- Desktop sync 适配器：默认关闭、allowlist 路径规范化、最多 50 个 session、按 workspace 节流合并、不可达 endpoint 不抛错、token 不外泄、discovery 文件、终态合并触发。测试使用本地回环假服务，不是真实 Desktop。
- 真实入口与配置：`superagent_live_check.py` 默认 preflight/列模型，live 必须 `--workspace`、`--source-thread`、`--confirm-live`；`--ask-roundtrip` 增加真实主会话提问→inspect pending→`zcode_agent_answer`→继续的入口（本轮只做参数/流程单元测试，未创建真实会话）；`desktop_sync_probe.py` 默认状态检查，sync 必须 `--confirm-sync`；storage/preferences/内嵌面板可视化新增配置。
- Desktop-owned native 后端（fake adapter）：显式/本机默认 `transport=desktop_native` 创建、后续发送与快照仍走同一后端（独立 engine RPC 故意 raise 证明未误走）；SID transport 持久化且旧 SID 不重判；`full_access→review_important_actions` 的当前权限传给 native notify send/后续 direct send；`native_messages` 数组映射；`native_pending` 不是问题队列；`projection.pendingInteractions` 的真实 interaction/option 回答，以及 `projection.pendingPermissions` 中 `toolName=AskUserQuestion` 的 `input.questions` 被识别为 `requestUserInput` 并保留 requestId/toolCallId/input/reason/riskLevel；未知写不重发；后台取消/model setter 明确 `needs_native_ui`/`unsupported`；原生不允许 `attach_report_mcp`；native 契约不强制调用不存在的 `zcode_agent_report`。
- Native ACK 归一化：首发 `status='accepted' + nativeAdmission.accepted=true` 与 V4 `status='accepted'/inputAccepted` 均 accepted；replay/pending/unknown 保持 uncertain 且不重发；真实 `native_children` `{childSessionIds,running,ended:{total,items}}` 与 `native_workflow_events` `{events,hasMore}` 已接；子 SID 登记为只读 native 绑定，`zcode_messages(child)`/`zcode_read_session(child)` 经 owned parent（DWF actor 带真实 `run_id`）走 `native_child_messages`/`native_child_snapshot`，child send/stop 明确拒绝，Host 拒绝原样报错不回退 engine。
- Native 完成通知：订阅→running→真实 `turn.started`/`turn.completed`→新 assistant→stable idle 只产生一个 `turn.completed`；实际 host commandId 替换临时 input id；pending/后台/unknown/read error 不完成；Observer 使用 per-SID identity/pending，不误用 engine 全局身份。
- `stopped` 统一按取消终态：`_run_rows`/`_baseline`/`_active_background` 与通知聚合都不再把它当 pending 或 completed。
- live-check `--ask-roundtrip`：CHECK-A/CHECK-B 实际选项，等待答题后的新 assistant 消息含 `QUESTION-ANSWER: <实际答案>` 才 ok；超时/未 echo/固定错误值都失败。
- `zcode_models` 探针在 `default_model` 已配置时携带 `model.options.reasoningLevel=cfg.default_thought_level`（非 auto）；default_model 为空仍不传 model，由 SDK 安全选择。
- report-only MCP：`zcode_bridge.report_mcp` 的 `tools/list` 只有 `zcode_agent_report`；call 只转发 report 参数；engine create 的 `mcpServers` 只为该 session 添加该薄入口（fake 参数验证）。
- Native 消息分页：父/子共用有界 lookahead，带游标时保留页大小，1000 条上限做一次有界补读；末条消息和用户锚点读取 ready snapshot 尾部；unready/foreign/读错保留诊断，不回退旧引擎。
- 旧 engine 严格 `session/send`：仅含 `sessionId/content/inputId`；`caller_permission_mode` 留在 Bridge/native 层；已知 `-32600/-32602/ZodError` 为明确拒绝，不标为未知结果或建议盲目重发。真实 report-only 往返也已实际接受这一发送形状。
- 发布隐私扫描包括 `.mjs`、`.js`、`.sql` 及对应大写扩展名；真实私有路径、凭据、数据库和运行状态不打包。
- 发布前独立审查复现的三项回归已加入：队列先持久化 claim，crash 恢复为 uncertain，不自动重新派发；报告 hint 与真实 send admission 分开，报告不能改变 uncertain/failed 的发送结论；Native generic permission 核对真实 pending/选项，非 full_access 的允许须有显式 user_approved，fullAccess 模式提升不得超过 caller grant，Host 同样校验，技术 AskUserQuestion 保持原路径。
- 设置面板：新增 `default_transport` / `desktop_native_enabled` / `desktop_native_control_file`，状态码不含 token/路径；旧默认仍为 `engine`。

## 历史真实基线（0.4.0，未在本轮复测）

- 2026-11-14 在 Windows、ZCode Desktop 3.18.2 与本机 Codex Desktop 上，通过标准 MCP stdio 创建独立工作区会话、选择已配置模型、派发真实模型任务；两次任务分别产生一个 `turn.completed`，宿主投递各一次。
- 其中一次在来源 Codex 会话结束本轮后，经第一方标准 MCP `send_message_to_thread` 接回同一会话继续验收。
- 0.3.1/0.4.0 的 3.18.2 消息字段、分页、终态证据、来源绑定、权限继承、幂等、取消、确认、重启状态和宿主适配器测试作为回归保留。

这些结果属于历史实测版本；本轮另外完成上表中的 0.5.0 实机往返，不能仅凭自动化通过声称新的宿主版本也可续工。

## 版本与证据边界

- 宿主唤醒依赖当前版本 Codex Desktop 的第一方 `send_message_to_thread`；跨版本不保证。一次成功的宿主 `tools/call` 只表示宿主接受消息，不代表模型已读或继续。
- 独立引擎保存不等于 Desktop 可见。`desktop.indexed`/`workspace_registered` 只说明索引/注册，不等于侧栏项目或打开页面已刷新；可选 Desktop sync 仍需隔离副本、真实 shim 和 GUI 肉眼验收。
- `zcode_agent_report` 是子端自述，永远只记为 `unverified_hint`；`verified` 只来自桥接持久通知终态证据。
- `stalled`/`unobservable` 是诊断状态，不是完成；`expired` 是终态但不是成功。
- 真实 DWF actor 自己提问、所有模型、所有项目归属与其他宿主版本没有全覆盖。列表被动出现有延迟；不把创建 ACK、索引登记或一次 GUI 成功当成即时显示保证。
- 会话级 report-only MCP 已真实连接并调用；这不证明任意 MCP 配置可以绕过 workspace trust。首次动态工作流授权仍可能出现，需按真实计划与当前授权处理。
- 默认插件仍用独立 `engine`。Desktop-owned `desktop_native` 需要另行准备版本/哈希门禁的实验性 Host 完整复制件，不在默认 ZIP 安装中自动修改 ZCode。
- Native setter、后台 task 取消、会话级 MCP 注入、外部 SID 接管、自动 runtime 恢复仍不支持，返回明确诊断。已卸载的 runtime 不能由保存历史推断为完成；经原生 Desktop 恢复后再观察。
