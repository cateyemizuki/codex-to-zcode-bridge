# 原版 ZCode Desktop 的 Host 接入：显式安装与回滚

本文件说明 `scripts/desktop_native_install.py`：一个**显式 opt-in、默认不执行**的
安装入口，作用于用户正常使用的**原版 ZCode Desktop 安装目录**，而不是完整复制件。
默认插件安装（`python install.py`）、`python build.py` 与普通启动都不会调用它。

它沿用 0.5.1 的 Host 接入机制，使用 0.5.2 的 shim、项目 resolver 与 native 适配器，让原版 Desktop 的
host 进程按 native own-engine 路径创建/读取实时会话，同时保持用户原有登录、
`appData/ZCode` profile、`session` 子目录、数据库和配置来源不变。

## 1. 适用版本与内置哈希

只接受下面这一组字节；没有 `--manifest`、跳过哈希、任意 RPC 或注入入口：

| 对象 | 值 |
| --- | --- |
| Desktop 版本 | `3.18.2` |
| `resources/app.asar` | `384840277` 字节，SHA-256 `4c0b8510a1ff7f1142714a1dc37139058254b784ad72b70ba781f09334b0c131` |
| `out/host/index.js`（asar 内） | SHA-256 `e6346359c9b659b7f33595565cde04b1badaca02babeee6d46f4bdbc0639f105` |
| `out/main/index.js`（asar 内） | SHA-256 `e0f1d059554f61684d2f5d720852a28fec7c645293499d84c4637a874727f78e` |
| `resources/glm/zcode.cjs` | SHA-256 `08726b5ef69e1e9072828baa4656713cb3aac93bb096938681278c5600312b2c` |
| `ZCode.exe` | SHA-256 `e853398ea2023e3e472fd2799389ad02445df5c2bfd36b2ed1f1e3dfe3dc958e` |

`preflight` 另外要求：目标含 `resources/app.asar`、`resources/glm/zcode.cjs`、
`.zcode-install-manifest`；`resources/app` 与 `resources/app.asar.original` 都不存在；
host 中 `ANCHOR_EXPOSE`、`ANCHOR_API` 各出现恰好一次；resources 与安装根可写。

**engine 原件逐字节不变**：`resources/glm/zcode.cjs` 只校验 SHA-256，不写、不备份、
不替换。native own-engine 会话不需要副本工具里的强制 `hydratePublisher` 补丁，因此
不能为了沿用副本流程而修改 engine。

## 2. 原版与副本的边界

- 原版（本文件）：沿用用户正常 profile（见第 7 节），只把 `resources/app.asar`
  换成展开目录，并加载本版本的固定补丁。
- 副本（`scripts/desktop_sync_copy.py`）：仍只处理完整复制件，并继续拒绝目标根
  仍含 `resources/app.asar` 的目录。本入口不复用副本的 ownedSID/runtime/generation，
  使用独立的 control/discovery/ownership 状态；旧 SID 不迁入新 Host ownership。

## 3. 命令

所有命令输出 JSON；拒绝时 `ok=false`、退出码 `2`。

```bash
# 只读检查：版本、字节数、SHA-256、锚点、目录归属、可写性、是否在运行
python scripts/desktop_native_install.py preflight --target <installed-zcode>

# 在私有 state 与同盘 staging 里构建替换树；不修改目标原有文件
python scripts/desktop_native_install.py prepare --target <installed-zcode> \
  --workspace <workspace> [--workspace <workspace> ...] [--dry-run]

# 目标未运行时才切换；不改 engine
python scripts/desktop_native_install.py apply --target <installed-zcode> [--dry-run]

# 对账 ledger 与当前文件
python scripts/desktop_native_install.py status [--target <installed-zcode>]

# 恢复到记录的原件；拒绝覆盖安装后被改过的内容
python scripts/desktop_native_install.py rollback [--target <installed-zcode>] [--dry-run]
```

状态目录：环境变量 `ZCODE_DESKTOP_NATIVE_STATE`；未设置时用
`%LOCALAPPDATA%/ZCodeDesktopNativeHost`。它不复用副本的 `ZCodeDesktopSyncShim`。
暂存目录固定为 `<installed-zcode>/.zcode-native-staging`（与原版同盘，保证重命名原子）。

`prepare` 前若状态目录已存在本工具 ledger 未记录的 control、ownership、discovery
或 host 状态，会拒绝而不是覆盖：这些文件可能属于另一个安装（包括副本），直接覆盖
会让原版运行时加载别人的 control 或 owned SID。需要时改用另一个 `--state-dir`，
或先回滚拥有该状态的安装。

`prepare --dry-run` 只做检查与计划输出，不创建 staging、不写 ledger、不改目标。
`prepare` 至少需要一个 `--workspace`，按解析后的绝对路径精确 allowlist。

## 4. 改动的文件、备份与切换

`prepare` 只写私有 state 与 staging：

- 用现有标准库 ASAR 读取逻辑展开 `resources/app.asar`（含 `app.asar.unpacked`
  中的原生模块），得到 `staging/app`；
- 对 `staging/app/out/host/index.js` 应用 `desktop_sync_shim.make_plan` 生成的补丁；
- 把 `staging/app/out/main/index.js` 换成 bootstrap，并把原件逐字节保存为
  `staging/app/out/main/index.native-original.js`；
- 在 `staging/app/out/main/zcode-native-control.json` 写入 control 绝对路径指针；
- 对整棵 staged 树计算确定性摘要（相对路径 + 大小 + SHA-256，覆盖所有文件），
  逐字节校验后写入 ledger。

`apply` 只做两次同盘原子重命名，且它们是本工具唯一拥有的目标改动：

1. `resources/app.asar` → `resources/app.asar.original`（原件完整保留，同一字节）；
2. `staging/app` → `resources/app`。

apply 前先按**可执行文件路径**确认没有进程从目标 `ZCode.exe` 运行（不按进程名
一概而论），再复核 asar/exe/engine 仍为原件、staged 树摘要未变。任一步失败会尽力
撤销本次两次重命名，并把 ledger 记为 `partial`；不会自动结束任何进程。

engine 不在本次改动内，apply 前后都复核其 SHA-256 仍等于原件。

## 5. 控制文件、workspace allowlist 与 token

`prepare` 用 `desktop_sync_shim.configure` 在私有 state 生成 control 文件，再用
`configure_native` 启用 native transport。control 内含随机 token，token 只由 host
进程内部读取：

- 不出现在 CLI 输出、错误信息、ledger、pointer 或 bootstrap；
- 只写私有 control（权限尽量收窄到当前用户）；
- `allowedWorkspaces` 是显式、精确匹配的 allowlist，不做前缀或通配放行。

本机绝对路径只写入私有 ledger/control、生成的 pointer 与已安装 bootstrap；公共
源码、测试与本文档不含任何本机路径。

## 6. 普通快捷方式与 Bridge 启动如何加载 control

两条路径都不需要额外环境变量：

- 普通快捷方式 / 直接运行 `ZCode.exe`：Electron 按 `package.json` 的 `main`
  加载 `resources/app/out/main/index.js`，也就是 bootstrap。bootstrap 读取同目录
  `zcode-native-control.json` 中的绝对 control 路径，**以该 pointer 为准**写入
  `ZCODE_DESKTOP_SYNC_CONFIG`（覆盖继承来的旧值），然后加载逐字节原样的
  `index.native-original.js`。host shim 在该环境下启动私有 control 服务。
- Bridge 启动：`runtime/zcode_bridge/service.py` 只 `Popen` 配置的 EXE，不传
  control 环境。由于 control 路径来自已安装的 pointer 而不是环境，Bridge 启动与
  快捷方式启动走同一条加载路径。若进程已存在，Bridge 只报告 `already_running`。

bootstrap 只做两件事：读 pointer 设定环境变量、加载原 main。它不 import
electron、不调用 `app.setPath`、不读 token、不写文件。pointer 缺失或损坏时它会
删除继承的 `ZCODE_DESKTOP_SYNC_CONFIG`（避免静默挂到别的实例/副本的 control），
但普通启动仍然继续；此时没有可选 control 通道，不会降级使用外来 control。

## 7. 正常 profile 与 bootstrap 边界

原版的真实默认 profile 是 `appData/ZCode`，其中 session 目录为其下 `session`
子目录。本方案**不新建**另一个登录账户或隔离 profile，不复制凭证/数据库，也不
照搬副本流程的隔离 `user`/`sessionData`/`home`：

- bootstrap 不设置 `userData`、`sessionData`、`home`，也不改任何 profile 路径；
- profile 仍由原 main bundle 按正常规则解析；
- 因此登录状态、最近项目与会话数据仍来自用户正常使用的那一份。

## 8. 自动更新后的失效检查

对已 apply 的安装，Desktop 自动更新可能重新写回 `resources/app.asar`。Electron
会优先加载 asar 而不是展开目录，此时本次接入实际失效：

- `status` 会报告 `resources/app.asar exists again (auto-update or manual change)`
  并把状态降为 `modified`；
- `apply` 只接受已 prepare、原件与暂存树校验均通过的状态；已有展开目录、
  `app.asar.original` 或原件哈希变化都会导致拒绝，不能用它覆盖升级结果；
- `rollback` 在 `app.asar` 与本安装的展开目录或 `app.asar.original` 并存时，
  将其视为升级或手工修改冲突并拒绝覆盖。若 apply 补偿或中断恢复后，仅剩
  `app.asar`，其哈希精确匹配 ledger 记录的原件，且展开目录与备份均不存在，
  则允许 rollback 收尾并记录 `alreadyReverted`，不会重写该原件；
- 仅 prepare、尚未 apply 时，保留原始 `app.asar` 是正常状态，rollback 可以
  丢弃本次暂存与派生状态。升级后应先检查并处理冲突，恢复干净安装布局后再
  `preflight` 核对版本与全部哈希。不要用旧 ledger 假定仍然健康。

## 9. 冲突拒绝与恢复

`status` 输出 `prepared` / `applied` / `partial` / `modified` / `rolled_back` /
`not_installed`。以下情况明确报告为冲突，而不是健康：

- 安装后出现新的 `resources/app.asar`（自动更新）；
- 展开目录被增删文件或改字节，整树摘要不符；
- `resources/app.asar.original` 缺失或哈希不符；
- `ZCode.exe` 被升级器/手工替换；
- engine 被外部修改（只报告，绝不写回）。

`rollback` 的条件：

- 展开目录整树摘要必须与 prepare 记录完全一致；
- `resources/app.asar.original` 哈希必须与记录一致；
- `resources/app.asar` 不得与本次的展开目录或 `app.asar.original` 同时存在；
- 存在进程从目标 `ZCode.exe` 运行时拒绝。进程报告的 ExecutablePath 为空或无法
  判定时同样拒绝，不把“路径未知”当作“目标未运行”。

只有全部满足才把展开目录移入 staging 内的临时目录并删除、再把
`app.asar.original` 改名回 `app.asar`，完成后逐字节校验。若 apply 的补偿或一次
中断的 rollback 已经把 `app.asar` 还原为记录原件（备份与展开目录都不存在），
`rollback` 不会再误报“自动更新”，而是校验该 asar 仍是原件、清理可证明属于本次
安装的残留 staging 并记 `rolled_back`。移动/删除前把 resolved 路径限制在 ledger
记录的 target/staging 内并检查 ownership 标记；没有 `--force`。
engine 不属于本次改动，rollback 绝不写它；若它已被外部修改，保留现场并在结果中
报告。`prepare` 之后、`apply` 之前执行 `rollback` 只会丢弃本次 staging 与派生
状态，目标原有文件不动。

`partial` 表示 apply/rollback 未完成；`status` 报告阶段、步骤与失败原因，按提示
重新 `rollback` 即可只撤销本次拥有的改动。若补偿已完成（`app.asar` 已是记录原件、
备份与展开目录都不存在），`status` 会注明 rollback 可以收尾而不再把它列为冲突。

## 10. 新会话的项目归属与侧栏分组

侧栏并不会因为缺少项目归属而“丢失”会话：未带项目的任务仍会出现在折叠的
“任务”分组/未归项目区。这里要解决的是**新建 native 会话按调用方指定的 workspace
关联到正确的既有项目**，而不是补一条界面通知或猜一个项目名。

真实机制（3.18.2）：

- `Et.createSession` 执行 `projectId: y.projectId ?? null`，省略不会推断项目；
  只有调用方显式传入时才会交给 `prepareSessionCreation`。
- `prepareSessionCreation` 只把 `projectId` 透传给 `reserveCreatedTask`，自身不做
  项目解析；`reserveCreatedTask` 对项目的校验只在任务行尚不存在、且未传
  `additionalDirectories` 时执行，因此不能把它当成 resolver 的替代。
- `BV.prepareSession` 在缺少项目 ID 时会新建 conversation/draft 目录，**不能**用来
  解析指定 workspace。
- 任务行上的 `handoff_pending` 是正常创建就会写入的 ownership 位，既不是删除标记
  也不是待办；本方案不读改也不清除它。

补丁在模块顶层的补丁闭包内注入一个固定、只读的 resolver，并作为工厂入参
`resolveProjectId` 传给 native transport（位置在调用方参数之后，控制通道无法覆盖）。
它只做三件事：`await T.getProjectRepository()`、同步
`readTree({includeTasks:false})`、在 `projects` 中精确匹配
`hostId === "local" && !deleted && roots[].rootPath` 等于 allowlist 规范化后的
workspace。它不写数据库、不创建或改名项目、不改任何既有会话的归属、不按项目名或
父路径猜测，也不新增全局 RPC/eval/通用通道。

四类结果：

下表描述底层 native transport 的决策；高层 MCP 入口当前按指定 workspace 自动解析，不要求用户手写项目 ID。

| 输入 | 结果 |
| --- | --- |
| 唯一 active 匹配 | 把该项目 id（字符串）作为 `projectId` 传给 `createSession` |
| 无匹配 | `projectId = null`，结果中明确 `unassigned`，不建项目 |
| 多个匹配 | 在 catalog 与 createSession **之前**以 `project_ambiguous` 拒绝，不创建任何会话 |
| 显式 `project_id` | 必须存在、`hostId === "local"`、未删除且包含该 workspace：不存在→`project_not_found`，host 非 `local`→`project_resolution_invalid`，已删除→`project_inactive`，不含该 workspace→`project_workspace_mismatch`；任一条不满足都不创建会话 |

项目 id 按字符串严格比较（数据库主键为文本）；workspace 与 `roots[].rootPath` 在
两侧做同样的规范化（分隔符、结尾分隔符、Windows 下的大小写）后比较。

补丁主动列表通知只在**首轮输入被接受且已能读到持久会话事实之后**尝试一次事实
导入及至多一次补发 `workspace_task_list_changed`；引擎自身的正常事件流不受限制。create 阶段不发（此时还没有持久 CLI
事实）；结果里的 `listNotification` 会如实标记 `facts_imported` /
`facts_unavailable` / `failed`。是否已尝试写入会话 ownership 记录的
`projectNotification`，成功或失败都算“已尝试”，后续 send/read/replay 一律不再
尝试；缺事实时不重试、不轮询、不抢焦点、不自动展开分组。通知失败只会变成
结果里的诊断字段，不会把已经 accepted 的 send 变成失败；项目解析失败在创建前拒绝。

**先部署后生效**：resolver 与通知代码在 `make_plan` 时被内联进
`out/host/index.js`。只改仓库源码不影响已经 apply 的实例，必须由本安装工具重新
prepare/apply（已有补丁标记时先 rollback）后才会加载。

## 11. 已验证与待验证的验收

已验证：

- 本版原版 Desktop `3.18.2` 已执行 rollback、preflight、prepare、apply，
  `status=applied`、`conflicts=[]`；整树按本机 ledger 校验。原件 ASAR、EXE 与未改写的 engine 均通过哈希校验。
- 已通过真实 Bridge 工具 `zcode_launch` 启动原版。Windows 原版 GUI 中观察到
  renderer 来自 `resources/app`；已有账号与项目列表正常，没有出现登录页或
  profile 警告，操作系统启动参数中的 `--user-data-dir` 指向正常的 `appData/ZCode`。
- 原版私有 control 的只读探针返回 `ok=true`、`state=ready`、`ready=true`，
  `allowed_workspace_count=4`、`subscribed_workspace_count=0`。该探针未执行 sync
  或 native create/send，只证明控制通道已就绪。
- 完整真实 ASAR 的临时副本两次往返，以及本版 `561` 项全量测试已通过；夹具验证涵盖
  prepare/apply/status/rollback、bootstrap 环境来源及拒绝路径。
- 原版 native 会话的实时流已端到端观察通过：`native_create` 与 `native_send` 都返回
  真实 accepted，该会话在后台真实执行了问答、工具调用与结束；在命令面板中打开同一
  会话后，问题、回答后的状态标记、工具运行状态与最终结束都在同一页面无刷新实时
  出现。因此实时推送链路本身工作正常。
- 本版新 SID 已在原版 GUI 中被动出现于指定项目，返回的项目归属为 assigned；
  真实问答、20 秒工具运行和最终输出均在同一页出现，无手动 reload。完成通知首次
  投递成功，来源 chat 实际收到、读回并 ACK。另行验证不显式传 transport 的默认
  native 派发、原 engine SID 仍按原后端读取，以及直接运行本版后项目与会话保留。
- 普通直接启动后新的 create-only 会话正确匹配既有项目，同时仍如实返回
  `desktopIndexFacts.exists=false`、`desktop.indexed=false`，没有把预留行冒充持久 CLI 事实。

待验证（下列各项均未实测）：

- 非空 workspaceIdentity、目录大小写或分隔符不同于项目根、歧义项目及显式错误 ID
  的真实 GUI；这些边界已由确定性回归覆盖，但未在真实账户上制造冲突项目。
- 多次跨实例关闭后读取旧 native ownership；当前明确拒绝旧实例接管，不做静默回退。
- 真实自动更新过程；目前只覆盖重新出现 asar 的冲突与已恢复 recorded original
  时允许 rollback 收尾的文件状态。

以上实机结果限于核对过的版本与本机配置，不替代这些未覆盖边界。Codex 当前聊天的
旧 MCP 进程需按升级说明重载；新标准 stdio 进程的环境转发已实测，当前聊天重载激活
另见 [验证记录](VALIDATION.md)。
