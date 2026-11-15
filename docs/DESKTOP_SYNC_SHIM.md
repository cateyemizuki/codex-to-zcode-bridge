# ZCode Desktop 实时同步实验 shim（可选、可回滚）

本文件描述一个**实验性、默认不安装**的 JS 层修复：它只作用于调用方显式指定的
ZCode Desktop 完整复制件，用来验证独立 engine 写入的会话事实能否通过 Desktop
自己的 host 进程进入 `tasks-index.sqlite`、发布 host 内的会话列表变更事件，并让
已经建立的会话订阅重新读取持久化消息。

它不属于默认插件安装，不会被 `python build.py` 打进 `plugins/`，也不会修改任何
已经安装的 ZCode 目录。没有 GUI 自动化，也没有向运行中的用户实例注入代码。
注意：Desktop 启动时会改写 HKCU 下的 `zcode://` 协议和 Explorer“Open in ZCode”
右键菜单；工具会在 prepare/launch 前备份这些值，并在启动后、restore 时恢复。

## 1. 适用版本与哈希门禁

- 已验证 Desktop 版本：`3.18.2`
- 目标文件：
  - `resources/app/out/host/index.js`
  - `resources/glm/zcode.cjs`
- 3.18.2 SHA-256：
  - host：`e6346359c9b659b7f33595565cde04b1badaca02babeee6d46f4bdbc0639f105`
  - engine：`08726b5ef69e1e9072828baa4656713cb3aac93bb096938681278c5600312b2c`

安装前必须同时满足：

1. 复制件的 `package.json` 版本等于清单中的版本；
2. host 与 engine 文件的 SHA-256 等于内置值或显式 `--manifest` 中的值；
3. 两个文件的补丁锚点各恰好出现一次。

版本不符、哈希不符、锚点数量异常、文件不是 UTF-8、或已有 shim 标记时一律拒绝
启用。对 3.18.2 不允许用 manifest 覆盖内置哈希；其他版本必须提供经人工复核的
manifest，否则拒绝。

## 2. 它改了什么

全部是 JS 层补丁，分为 host 与 engine 两处。

### 2.1 host：索引事实与控制面

在 `out/host/index.js` 中增加三处：

1. 在创建 host 任务索引同步器后，把实例挂到进程内
   `globalThis.__zcodeDesktopSync`，供窄控制面使用；
2. 在同步器对象上增加 `syncSessionFacts` 方法：
   - 调用 Desktop 自己的 `reconcileSessionsIndexV4`
     （`v4/sessionsIndex/reconcile`）重新读取已持久化的 CLI 会话事实；
   - 通过已有的 `TaskIndexRepo.applyCliFacts` 路径写入索引；
   - 只在标题、更新时间或状态变化时发出与正常镜像相同的
     `workspace_task_list_changed` 事件；
   - 会话不存在时返回 `missing`，删除时走已有的 `removed` 路径；
3. 在文件末尾追加一个仅监听 `127.0.0.1` 的控制端点，只接受 `probe` 和 `sync`。

### 2.2 engine：让已打开的会话订阅重新读取消息

在 `resources/glm/zcode.cjs` 的 `ConversationV4Gateway.reconcileSessions` 中，在
原有 `host.reconcileSessions(...)` 返回事实之后，为本次请求的每个 session 检查
是否存在活跃的 conversation 订阅；存在时调用已有的
`hydratePublisher(sessionId, undefined, true)`。

这条路径会：

- 通过 `loadPersistedEvents` / `persistedMessages` 重新读取 `sessionStore.messages`；
- 重建 projection，并标记该 publisher 的所有订阅需要重新发送快照；
- 把新的 snapshot/delta 帧推送给已经建立的订阅（包括 renderer 的连接）。

它不调用 `session/stop`、`session/close`、`session/resume`，也不新建或接管用户任务。

没有新增 `eval`、Shell、`child_process`、任意命令执行、任意协议注入或通用注入器。

### 2.3 启动 bootstrap：让复制件真正创建主窗口

复制件里 Electron 的路径服务无法解析 `appData` / `userData` / `sessionData`
（`app.getPath(...)` 抛 `Failed to get '...' path`，而 `home` 与 `temp` 正常）。
应用在**模块求值阶段**就会调用 `app.getPath('userData')` 初始化遥测，这个 promise
被 `app.whenReady()` 回调里的 `await` 等待；一旦它 reject，回调在
`[startup] 创建主窗口` 之前就退出——进程、GPU、renderer 都在，但没有任何窗口，
host 也不会加载。

`prepare` 因此在展开后的 `resources/app/out/main` 里加入一层窄 bootstrap：

1. 把原始的 `out/main/index.js` 原样保存为 `out/main/index.sync-original.js`
   （写入前要求它的 SHA-256 等于 `app.asar` 里的同名条目）；
2. 新的 `out/main/index.js` 带 `ZCODE_DESKTOP_SYNC_BOOTSTRAP_V1` 标记，只做两件事：
   - 用启动器已经传入的 `ZCODE_DESKTOP_USER_DATA_DIR` /
     `ZCODE_DESKTOP_SESSION_DATA_DIR`（必须是绝对路径）调用 `app.setPath`；
   - `await import('./index.sync-original.js')` 加载未改动的原始 main。
3. 应用自己稍后也会用同一组环境变量调用 `app.setPath`，所以这是把已有的合法步骤
   提前，不改变应用语义。

bootstrap 不读其他环境变量、不接受相对路径、不写文件、不引入 IPC/eval/子进程。
`launch` 在启动前会校验 bootstrap 标记、host shim 标记与 engine reconcile 补丁，
三者缺一即拒绝启动；`restore` 删除展开的 `resources/app`，bootstrap 与原文件一起消失。

已对已有复制件补装（不需要重新复制 944 MB）：

```bash
python scripts/desktop_sync_copy.py bootstrap --destination <copy>
```

## 3. 控制面

控制配置放在私有 state 目录（默认
`%LOCALAPPDATA%/ZCodeDesktopSyncShim`，可用 `ZCODE_DESKTOP_SYNC_STATE` 覆盖）：

```json
{
  "host": "127.0.0.1",
  "port": 0,
  "token": "<随机 token>",
  "discoveryPath": "<state>/host-discovery.json",
  "allowedWorkspaces": ["<显式允许的工作区绝对路径>"]
}
```

- host 进程从环境变量 `ZCODE_DESKTOP_SYNC_CONFIG` 读取该文件；
- 监听端口写在私有 discovery 文件中，不进入仓库；
- 每个连接第一行必须提交 token，长度不足 16 的直接拒绝；
- `sync` 只接受 `allowedWorkspaces` 中的路径（Windows 下按解析后的路径比较），
  事务最多 50 个 session id；
- CLI 与 shim 都会先把请求路径解析成白名单中的配置路径，再转发给 host；
  正斜杠、反斜杠、尾部斜杠或大小写差异不会产生第二个 workspace key；
- 没有通配符、递归扫描或“任意工作区”模式；
- `sync` 面向 host 已经初始化/打开过的工作区；冷工作区如果还没有 engine
  运行时，reconcile 会失败并在响应里返回可观测错误，而不是伪造成功。

这些连接信息不写入仓库；仓库只提供生成和读取它们的工具。

## 4. 使用步骤（完整复制件，推荐）

以下命令都在本仓库根目录执行。`--source` 是已安装目录，工具只读它；
`--destination` 是新的隔离复制件。

```bash
# 1) 先 dry-run，确认版本、asar/engine 哈希、文件数与目标路径
python scripts/desktop_sync_copy.py --state-dir <private>/copy-state prepare \
  --source <installed-zcode> --destination <copy> --dry-run

# 2) 复制完整安装（可加 --hardlink 节省同盘空间）、展开 asar、打 host/engine 补丁
#    并写入启动 bootstrap（bootstrap 文件与 host/engine 补丁一起通过哈希门禁）
python scripts/desktop_sync_copy.py --state-dir <private>/copy-state prepare \
  --source <installed-zcode> --destination <copy> --hardlink --experimental

# 3) 生成隔离控制配置（私有 token/端口）
python scripts/desktop_sync_shim.py --state-dir <private>/state configure \
  --workspace <allowed-workspace> [--workspace <another>] --port 0

# 4) 只显示隔离启动命令（不启动）
python scripts/desktop_sync_copy.py launch --destination <copy> \
  --sync-config <private>/state/control.json --workspace <allowed-workspace>

# 5) 显式启动复制件（隔离 user-data-dir / HOME）
python scripts/desktop_sync_copy.py launch --destination <copy> \
  --sync-config <private>/state/control.json --workspace <allowed-workspace> --execute

# 6) 运行中探测、同步
python scripts/desktop_sync_shim.py probe --config <private>/state/control.json
python scripts/desktop_sync_shim.py sync --config <private>/state/control.json \
  --workspace <allowed-workspace> --session-id <session-id>

# 7) 一条命令回到原始复制件
python scripts/desktop_sync_copy.py --state-dir <private>/copy-state restore
```

`launch --execute --smoke` 会追加 `--version` 并设置超时；本机实测该参数仍会启动
真实 app main，不能替代 GUI 验收。`launch` 会清除调用方继承的 `ZCODE_*` 变量，
只保留复制件自身的 builtin provider 路径和隔离 HOME/user-data。

`prepare` 会把 HKCU 的以下键值快照到私有 state：

- `Software\Classes\zcode`
- `Software\Classes\zcode\shell\open\command`
- `Software\Classes\Directory\shell\ZCode.OpenInZCode` 及其 `command`
- `Software\Classes\Drive\shell\ZCode.OpenInZCode` 及其 `command`

`launch --execute` 启动后等待 6 秒（Desktop 在启动早期写入注册表），立即恢复原值，
并启动一个 detached guard 每 2 秒检查、在复制件退出后再次恢复。`restore` 也会恢复
这些 HKCU 值。若检测到 HKCU 已经指向目标复制件而没有快照，launch 会拒绝继续。

`restore` 的规则：

- 逐字节恢复 host 与 engine，校验安装时记录的原始 SHA-256；
- 删除展开的 `resources/app`，把 `app.asar.original` 改回 `app.asar` 并校验哈希；
- 校验 `ZCode.exe` 哈希未变；
- 默认拒绝覆盖安装后被外部修改的文件，确认后可用 `--force`。

## 5. 回滚

1. 停止使用该复制件的 Electron/host 进程；
2. 执行 `desktop_sync_copy.py restore`，确认输出中的 `hostSha256`、`engineSha256`、
   `asarSha256`、`executableSha256`；
3. 可选：删除整个复制件和私有 state 目录；
4. 文件层面：已安装的 ZCode、`app.asar`、`ZCode.exe` 和签名均未被本工具改动；
5. 注册表层面：`restore` 会恢复 prepare 时快照的 HKCU 协议/右键菜单值。

## 6. 已验证到哪一层

在隔离 HOME、隔离工作区、完整安装复制件中做过以下实测：

- 独立 engine（同一个 `zcode.cjs app-server`）创建会话并写入两条助手回复；
- 使用本地假 OpenAI 兼容端点生成回复，未发真实用户消息、未访问真实模型；
- 完整复制件由工具复制并展开 asar，host 与 engine 补丁均通过哈希门禁；
- host 复制件以两种方式运行过：纯 Node 模拟 `parentPort`，以及真实 Electron
  `utilityProcess`（最小 main，不创建 BrowserWindow）；
- host 运行后，`probe` 在独立 engine 写入前后都没有该会话的列表事件；
- `sync` 后 `v4/sessionsIndex/reconcile` 返回持久化事实，索引出现对应行，
  host 内发出 `workspace_task_list_changed`；
- 同一会话再次 `sync` 返回 `changed=false`，不重复发事件；
- 不存在的 session 返回 `missing=true`，不创建索引行；
- 非允许工作区返回 `workspace_not_allowed`；错误 token 被拒绝；
- 会话订阅层实测：先缓存第一条助手回复（snapshot 行数/消息 id），再由独立
  engine 追加第二条带唯一标记的消息；调用 `v4/sessionsIndex/reconcile` 后，
  订阅收到新的 snapshot/delta 帧，帧内包含第二条标记；
- 完整复制件 `restore` 后 host、engine、asar、EXE 哈希均与原件一致；
- 注册表快照/恢复实测：当前 HKCU 的 `zcode://`、Directory/Drive 右键命令已恢复为
  `<installed>/ZCode.exe`；`restore_shell_registry` 对同一快照执行后值不变；
- 复制件 `ZCode.exe` 的 Authenticode 在补丁前后均为 `Valid`，签名者为
  `CN=北京智谱华章科技股份有限公司`。

加入启动 bootstrap 后的实机结果（隔离 HOME、隔离 workspace、隔离 user-data）：

- 复制件创建了真实 Desktop 主窗口（`MainWindowTitle=ZCode`，非零窗口句柄），日志出现
  `[startup] 创建主窗口`；同一份日志不再出现 `[arms] electron init failed` 或
  app-ready 早退；
- 进程树完整：main、GPU、renderer、host（`utilityProcess` NodeService）、extension
  host，以及从复制件 `resources/glm/zcode.cjs` 启动的 app-server（不是安装目录）；
- host 写出隔离的 discovery 文件；`probe` 返回 `ready=true` 与 host pid，
  `sync` 返回该会话的持久化事实（索引已一致时为 `changed=false`）；
- 启动 6 秒后及 guard 运行期间，HKCU 协议/右键命令均指回安装目录
  （`<installed>\ZCode.exe`）。

**没有验证**：真实 Electron 渲染进程/store 的正文重绘、以及 GUI 内“打开中的会话是否
立即刷新”的最终视觉结果。代码路径已确认：host 的 `subscribeConversationV4` 与
`reconcileSessionsIndexV4` 都通过同一个 workspace engine client（`ot()`）发到同一个
engine 进程，engine 的 `performHydration` 会遍历本进程 `flushStates` 并对仍存在的
订阅重发 snapshot；但这一步仍需根代理用 Computer Use 在真实窗口里验收。

**所有权与存活证据边界（根代理 2026-11-15 审计）**：本 shim 不是实时外来所有者传输。
3.18.2 冷恢复会创建新的 `bIi` service，并调用 `$Ti/reconcileOrphanRuns` 把共享的
非终态行标成 stopped，没有跨进程所有权校验；强制 hydrate 仍读本进程
`InMemorySessionEventStore`，`LBc` 还会排除已见过的 run，模型 streaming delta 也不是
持久事实。因此：不得用本 shim 声称另一进程的实时会话已在 UI 实时显示；不得冷恢复
外来活动 SID 用于查看；默认保持关闭。根代理的最终方案是另一工作树中的可选 native
Desktop host 控制传输（`createZCodeAgentService` 的 `createSession`、
`sendConversationCommandV4`、`readSession`、`readSessionMessages`、`readSessionEvents`、
`conversationWorkflowRunsV4` 等），在真实 GUI 验收与契约合并前，本工作树不把它当作
可用能力，也不覆盖或重启另一个工作树/运行实例。

## 6.1 探针与 GUI 肉眼验收

探针脚本只做本地状态检查或显式 sync，不启动/聚焦 Desktop，也不打印 token：

```bash
python scripts/desktop_sync_probe.py --control <private>/control.json --op status
python scripts/desktop_sync_probe.py --control <private>/control.json --op probe
python scripts/desktop_sync_probe.py --control <private>/control.json --op sync \
  --workspace <allowed-workspace> --session-id <session-id> --confirm-sync
```

`--op sync` 必须显式 `--confirm-sync`；一次最多 50 个 session，workspace 必须是 `allowedWorkspaces` 中的规范化路径。

GUI 验收步骤与预期：

1. 用户明确要求后，手动启动隔离副本（`desktop_sync_copy.py launch ... --execute`）；探针不负责启动窗口。
2. 在隔离副本中打开一个已存在的会话，记录第一条助手回复的可见标记（例如 FIRST）。
3. 通过独立 engine 或 `scripts/superagent_live_check.py --confirm-live ...` 向该会话追加第二条带唯一标记的消息（例如 SECOND）。
4. 运行 `desktop_sync_probe.py --op sync`（显式 `--confirm-sync`）。
5. 预期：同一打开页面在无导航、无刷新的情况下出现 SECOND；host 发出 `workspace_task_list_changed`；`probe` 的 ready 为 true，sync 返回 changed/missing/errors 计数。
6. 若页面没有出现 SECOND，记录 probe/sync 的真实状态，不要声称已刷新；侧栏项目归属还要单独确认，`desktop.indexed=true` 不等于项目分组或 GUI 已显示。

## 7. 运行实例与重启

本 shim 不能无重启接入一个已经在运行的 Desktop。`out/host/index.js` 只在 host
窗口进程 fork 时加载一次，engine 文件也只在 engine 启动时读取；要让两个补丁
生效，需要**重启复制件一次**（关闭该复制件的全部窗口后重新启动）。实验中没有
重启或注入任何真实用户实例。

## 8. 已知限制

- renderer 打开中会话正文是否立即重绘仍需 GUI 验收（见第 6 节）。
- 启动 bootstrap 依赖 `ZCODE_DESKTOP_USER_DATA_DIR` / `ZCODE_DESKTOP_SESSION_DATA_DIR`
  为绝对路径；直接双击复制件（不经 `launch`）不会设置这些变量，仍会停在
  `Failed to get 'userData' path`。
- 通过 host 的 `Et.subscribeConversationV4` 新建 background 订阅时，3.18.2 host
  自身在解析首个 conversation frame 时出现 `protocol_parse_error`；因此本实验
  不使用该路径做观察者，消息刷新用独立的 v4 conversation 订阅验证。
- 打开中的会话正文是否立即重绘仍取决于 renderer 对订阅帧的处理，未做 GUI 验收。
- 删除会话路径（`applyCliDeletion`/`removed`）未在实验中实际触发。
- 完整复制件约 944 MB；展开 asar 会新增约 3.2 万个文件。`--hardlink` 仅适用于
  同盘复制。
- 工具不重新打包 asar；它保留 `app.asar.original` 并让 Electron 从
  `resources/app` 目录加载。
- launch 不能完全阻止 Desktop 在启动早期写 HKCU；采用“快照 + 6 秒后恢复 + guard”
  的方式，存在短时间窗口。若应用在运行中再次改写，guard 会在 2 秒内恢复。
- 不处理 Windows 自动更新；修改展开后的 JS 会破坏该复制件的完整性，属于实验行为。
- 没有做生产 DLL 注入，也不提供通用注入器。

## 9. 与 Desktop-owned native 后端的区别（0.5 可选）

旧的 `probe` / `sync` 操作只让复制件的 host 重新读取独立 engine 已持久化的会话事实，
**不是**实时外来所有者传输；本文第 6、8 节的证据边界仍然有效。0.5 另有一个显式选择
的 `desktop_native` 后端，语义完全不同：

- 新会话由复制件的 native host 自己创建并拥有；不接管、不迁移、不冷恢复独立 engine
  或外来 SID。只有完成 create 的 owned SID 才能发送/读取。
- 传输走 `runtime/desktop_sync/native_transport.mjs` 的固定 `native_*` 操作：
  `native_model_catalog` / `native_create` / `native_send` / `native_snapshot` /
  `native_messages`（直接数组）/ `native_events` / `native_resolve` /
  `native_cancel` / `native_workflows` / `native_workflow_events` /
  `native_children` / `native_topology` / `native_pending`（写请求结果查询，不是
  问题队列）。每条请求带 workspace 与当前 `caller_permission_mode`；写操作带稳定
  `request_id`。
- 默认关闭。启用需要私有 control 文件里的 `nativeTransport.enabled=true`；host
  未就绪、`unsupported`、`stale_owner`、`unknown` 均明确返回，不退回独立 engine，
  不自动打开/聚焦/重启项目。token 不进入桥接响应/日志/提示词。
- 当前 MVP 已支持 native 完成通知：订阅按 SID 持久化，admission 后使用 host 实际
  `commandId`/inputId 作为锚点，Observer 读 native snapshot/messages/events/jobs/
  workflows/pending；真实 `turn.started`/`turn.completed` + 新 assistant 消息 +
  stable idle 才完成，pending/后台/unknown/read error 不完成。仍不支持 native model
  setter、saved workflow catalog 和 background cancel；这些是明确
  `needs_native_ui`/`unsupported`，不是静默降级。native child 消息/快照经 owned
  parent 与真实 run（DWF）只读，child 写动作明确拒绝。未知写结果不自动重发。
- ACK 归一化按真实形状：首发 `status:'accepted'` + `nativeAdmission.accepted:true`，
  V4 `status:'accepted'`/`inputAccepted` 均为 admission；replay/pending/unknown/
  duplicate/noop 不是 accepted，保持 uncertain 且不重发。
- GUI 证据范围：根代理只在其 3.18.2 隔离复制件、当前固定 native op 上验证了
  “同页先 BEGIN/Bash 等待 90s，随后自动 END/发送恢复，无强制 hydrate/reload”。
  公共跨版本仍受版本与 SHA-256 门禁；本工作树没有重跑 GUI，也不把该结果外推到
  其他版本、其他复制件或运行中实例。
- 旧的独立 engine 同步方案不能保证活动 DWF 的实时内容，并可能 cold-resume 把外部
  run 误标 stopped；正式 0.5 因此把两者分开：默认仍是 engine，只有显式选择才新建
  desktop_native 会话，绝不在同一个 SID 上跨引擎接管。
