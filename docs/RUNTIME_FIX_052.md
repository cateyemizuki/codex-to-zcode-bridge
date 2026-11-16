# 0.5.2：原版接入、项目归属与恢复状态

本版保持 34 项 MCP 工具及两侧共享控制服务，默认独立引擎模式不变。原生模式仍需显式准备兼容的 Desktop Host，不会因安装 ZIP 自动修改应用。

## 原版 Desktop

新增 `scripts/desktop_native_install.py` 的 preflight、prepare、apply、status、rollback。它仅接受已核对的 Desktop 3.18.2 字节与固定补丁锚点，原 EXE 和模型引擎不改写；保留正常登录与 profile，不复制账户或数据库。展开树和原件按摘要校验，运行中、版本变化或外部改动会明确拒绝覆盖。步骤见 [原版安装说明](DESKTOP_NATIVE_INSTALL.md)。

## 指定工作区的项目归属

旧入口仅传 workspace，原生 `createSession` 将缺失的 projectId 视为 null；会话因此落在未归属“任务”分组，而非工作区对应项目。折叠的分组可能使人误判为列表完全缺失。

固定只读 resolver 查询已有本机项目：唯一活动项目拥有该工作区时复用其 ID，无匹配明确未归属，歧义或无效显式 ID 在创建前拒绝。不按名称或父目录猜测，不改已有会话归属，也不清除 ownership 标记。

create-only 阶段可能只有预留记录，尚无持久 CLI 事实。补丁在首轮输入真实接受后最多执行一次事实导入与主动列表通知；缺事实或通知失败只作诊断，不重发输入、不轮询、不强制刷新或展开界面。原生摘要与高层派发返回创建时的项目归属，持久索引与 GUI 可见仍需分别确认。

## Codex 通知环境与升级

Codex 侧原始清单和安装器输出共同列出三个 `env_vars` 名称：`CODEX_APP_TOOLS_PIPE_PATH`、`CODEX_APP_RUNTIME_CLI_PATH`、`CODEX_MCP_NODE_PATH`。它们使当前客户端把宿主连接信息提供给 MCP 进程；发布包不保存连接值，ZCode 侧不继承这些 Codex 专用能力。

安装器固定本机 `ZCODE_BRIDGE_STATE`，升级时继承已有安装选择的状态目录，避免客户端环境过滤后误用另一份配置。重载 MCP 后须重新探测；仅给旧 daemon 临时补上下文，不代表新安装的环境转发已经验收。

## 可恢复工作流与真实错误

观察器保留 SDK 当前状态，不用旧 journal 的终态覆盖恢复后的 running。真实 run-started/run-launched 清除拓扑中的历史停止状态；errored 归为失败。

stopped 需结合停止原因和 resumable：明确 user/model/superseded 停止仍为取消，interrupted 且可恢复保持非终态 paused。缺少可靠原因或元数据不一致时保留 unknown/read_error，阻止错误的完成判定。独立 engine 的补证仅查询已绑定工作区、同一 runId 与 parentSessionId；native 不转到独立 engine。历史终态事件和旧 job 不自动改写，恢复或修订后的活动工作可重新 attach 订阅，无需重发任务输入。

普通跟进消息到达 ZCode 主会话，不保证注入正在运行的 DWF actor。需要中途输入的工作流应预留 hole/escalation；重大目标更正可受控修订工作流，并重新核对其生命周期与通知归属。

## 验证

合并测试、构建一致性和发布内容审计，以及实机覆盖和未覆盖范围，统一记录在 [VALIDATION.md](VALIDATION.md)。不以子代理自述、一次 idle 或工具 accepted 作为任务通过证据。
