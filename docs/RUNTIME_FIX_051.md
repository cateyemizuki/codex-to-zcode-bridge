# 0.5.1 运行期定向修复

本文记录 0.5.1 对两项真实协作问题的修复。示例均使用通用名称，不包含会话 ID、工作区路径、模型 provider 编号或凭据。0.5.1 是叠加在 0.5.0 上的补丁；0.5.0 的发布包与验证记录保持原样。

## 1. 技术权限不再因工作流代码文本被当作认证问题

### 现象

父代理调用 `zcode_agent_answer` 回答一个真实的技术权限请求（例如批准运行 `CreateWorkflow`）时，桥接在原生 resolve 之前抛出：

```
user_answer_required: 认证或真实用户偏好必须由用户本人回答
```

该请求的 payload 包含 benchmark 脚本，脚本里有 `total_tokens`、`password` 等测试变量。它们既不是登录问题，也不是凭证输入。

### 原因

旧实现把整个 `request.params` 序列化后做凭证子串扫描。任何出现在工具参数、脚本、路径、变量名或日志里的 `token`、`password`、`login` 等词，都会把技术权限误判成认证问题。

### 修复

- `interaction/requestPermission` 一律按真实技术权限处理，不再扫描它的 `input` 数据；是否需要 `user_approved`、能否使用 `fullAccess` 仍由真实的 pending request、实际 `optionId` 和当前 caller 权限决定。
- `interaction/requestUserInput` 只检查真实人类题面：`question`、`header`、选项的 `label`/`description`，以及题面自身的 `sensitive`/`secret` 标志。不检查 `value`、工具参数或代码体。
- 题面出现 password/passcode/API token/OAuth/登录/凭证等凭证语义时，仍要求用户本人回答；`full_access` 和 `user_approved` 都不能替代。
- 题面出现偏好语义时，仍要求真实用户决定。
- 无法读出题面的用户输入结构保持保守拒绝，不猜测答案。

### 回归

- engine 与 native 两条 `zcode_agent_answer` 路径都使用包含 `total_tokens`、`password` 等文本的 CreateWorkflow 权限 payload，证明 `allow_once` 抵达真实 resolve 一次。
- 负对照：真实的 password/OAuth 问题在 `full_access` 与 `user_approved` 下仍被拒绝；未知用户输入结构仍被拒绝；偏好问题保护仍在。

## 2. 工作流持续进展时不再误报 stalled

### 现象

父主会话长时间 idle、父 messageId 不变，但动态工作流的 journal cursor 持续推进、子代理持续产出时，job 被标为 `stalled` / `no_observable_progress`，父代理因此误判任务停滞。

### 原因

订阅与无订阅两条同步路径都只用父会话 `status` 和最后一条消息 ID 计时。父会话 idle 时这两个值不变，工作流与子代理的真实进展完全不可见。观察器的稳定采样摘要也不包含 run cursor，因此同一摘要下 cursor 34→61→91 不会被识别。

### 修复

- job 的 `last_progress.run_watermarks` 按 run 保存 `{epoch, cursor}` 高水位；只有同一 run 在同一 epoch 内的 cursor 严格大于已保存水位才刷新，新的 run 只有在已观察到事件（cursor > 0）后才算进展。
- 不变、倒退、重排或单纯轮询都不会刷新；倒退时保留高水位，避免之后的重放伪造进度。epoch 变更只建立新基线，不因旧值往返刷新；epoch 内后续严格前进才继续刷新。
- 新 epoch 的首次 cursor 通过独立的基线落盘保存（`update_run_watermarks`）：只更新 `run_watermarks`，不改变 `last_progress.at`、不改变 activity、不清除读失败；Bridge 重建/存储重载后仍以此基线判断后续同 epoch 前进。
- 订阅路径复用观察器已经持久化的 `run_cursors`，优先当前 epoch 的 key；无订阅路径复用已读取的 run rows，并使用快照的 stream/engine epoch。
- 子会话 ID 集合、子快照的 `eventSeq`/`stateRevision`/`turnCount` 摘要与最后一条消息的无正文活动摘要也参与判断；子快照只在已有读取路径中抽取小元数据，不拉取子代理正文。
- `run.status=running` 本身不刷新；只有真实 cursor/子流/消息活动或父状态变化才刷新。
- 真正没有变化的 job 仍在 `superagent_stall_seconds` 后进入 `stalled`；出现新进展时从 `stalled` 恢复到 `running`。
- `workflowRunEvents` 读取失败不会变成 cursor 0 进展：失败行被跳过并保留旧水位，连续失败仍按阈值进入 `unobservable`；成功读取会打断连续失败计数。
- 真实 pending 优先于读失败诊断：已有待答问题时，连续读失败不会把 `action_required` 改写为 `unobservable`；只有实时 pending 集为空才恢复。终态 `failed`/`cancelled` 仍最高优先。
- pending/`action_required`、终态/`verified`、队列与权限边界、通知唤醒授权都不变；进展更新不产生事件、不唤醒宿主。

### 隐私与开销

- 只持久化紧凑、可审计的元数据：`run_watermarks`（run → `{epoch, cursor}`）、已知子代理 ID、子快照标量摘要、消息 ID + 无正文摘要。
- 消息摘要只覆盖角色、part 类型、工具名/状态与文本长度/哈希，不保存消息正文、路径、provider 或凭据。
- 不额外每秒拉取所有子代理正文；活动字符串长度有界，超长时保留前缀并对完整值做哈希，避免截断导致漏报。

### 回归

- 订阅与无订阅都分别覆盖：cursor 34→61→91、父 idle/messageId 不变、65 秒时仍 `running`；冻结 cursor 34→34→34 在 65 秒时进入 `stalled`。
- 倒退/重排/新 run cursor 0/epoch 往返不刷新；稳定 epoch 内严格前进（或新 run 已有事件）才刷新。
- 新 epoch 基线：e0:91@0s → e1:1@30s 只落盘基线且 `last_progress.at` 不变；e1:2@65s 才刷新并保持 `running`。基线单独存在到 65 秒仍 `stalled`；Bridge 重建后 e1:2 仍能刷新。
- `workflowRunEvents` 交替失败/恢复持续 180 秒不会刷新进度并最终 `stalled`；连续失败第 5 次进入 `unobservable`；已有 pending 时保持 `action_required`。
- 订阅：新子代理或子流 `eventSeq`/`stateRevision` 变化视为进展；终态 `completed` 优先于 cursor 变化，仍判 `verified`。
- 无订阅：同一 messageId 下文本/工具活动变化视为进展；workflow 读取失败不刷新计时并可达 `unobservable`；已有“真正静默仍 stalled、idle 不冒充完成”回归保持通过。

## 验证方式

在 `runtime` 源码修改后运行：

```powershell
python -m unittest discover -s tests -q
python build.py
python build.py --check
python scripts/audit_release.py
```

0.5.1 的本轮结果为 514 项单元/行为测试通过、生成的 Codex/ZCode 包与源码一致、发布隐私扫描通过。上述验证使用 fake engine/native 与真实源码语义测试，**未**在本轮重新执行真实 CreateWorkflow 实机授权；实机验收由根代理在发布前独立完成。

## 边界

- 工作流可见性取决于当前 SDK 暴露的 run cursor、子代理列表和消息 part。SDK 不暴露的信号不会被猜测为进展。
- 观察器已经持久化的 cursor 可能比实时状态晚一个轮询周期；桥接只在观察到变化时刷新，不把“稍后可能变化”当作进展。
- `stalled`/`unobservable` 仍是诊断状态，不是完成。`verified` 只来自持久通知终态。
