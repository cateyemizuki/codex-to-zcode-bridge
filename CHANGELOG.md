# Changelog

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
