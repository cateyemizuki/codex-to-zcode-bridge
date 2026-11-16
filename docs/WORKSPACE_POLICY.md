# Desktop 工作区准入规则

0.5.3 起，新建的 Desktop 私有控制配置默认使用黑名单，只禁用 C 盘根目录及其全部子路径；其他盘的现存工作区可直接使用，无需逐个登记。规则同时由 native/sync 的 Python 客户端和 Desktop shim 检查。

规则限制 Desktop 桥接可以选择的**工作区根目录**。它不限制模型在任务中读取哪些文件，也不为独立 `engine` 后端提供文件访问隔离。

## 新配置

```powershell
python scripts/desktop_sync_shim.py --state-dir <private-state> configure
```

`workspacePolicy.mode` 默认是 `blacklist`，`workspacePolicy.blockedPaths` 默认只含 C 盘根路径。`blockedPaths` 接受绝对路径列表，每项都匹配自身和子目录；显式传空列表表示没有屏蔽路径。native 的独立启用开关、loopback 认证和会话 ownership 检查继续生效。

若需要精确白名单，显式选择并逐项提供现存工作区：

```powershell
python scripts/desktop_sync_shim.py --state-dir <private-state> configure --policy-mode allowlist --workspace <workspace>
```

白名单只匹配该工作区本身，子目录需要单独列出。`prepare --workspace` 保留兼容，但新控制配置的默认模式仍是黑名单。

## 迁移现有配置

旧配置含 `allowedWorkspaces` 且没有 `workspacePolicy` 时，继续使用原来的精确白名单；升级插件不会静默扩大旧规则。用户明确选择切换时执行：

```powershell
python scripts/desktop_sync_shim.py workspace-policy --config <private-control.json> --mode blacklist
```

此命令只更新工作区规则，默认只屏蔽 C 盘；保留现有 token、端口、discovery、nativeTransport 和 ownership 路径。旧 `allowedWorkspaces` 可保留用于以后切回白名单，黑名单模式不使用它放行目录。

客户端与 0.5.3 shim 在每次请求时重新读取规则。已加载旧 shim 的 Desktop 进程仍使用旧实现，需要加载新版 shim 后才支持黑名单；只升级 MCP 包不能使旧 Desktop 进程获得新逻辑。迁移命令本身不修改应用或重启任务。

## 路径与错误

- Windows 路径比较忽略大小写，统一正反斜杠并处理 `.`、`..` 和扩展路径前缀。
- 黑名单按目录边界匹配；同名前缀的相邻目录不会被一并禁用。
- 同时检查原始绝对路径和解析后的真实路径，指向屏蔽目录的 junction/symlink 也被拒绝。
- 相对规则、未知模式、损坏或无法读取的规则会拒绝访问；不会自动退回全开放。
- 被拦截的调用返回 `workspace_not_allowed`，不会自动切换后端。

`desktop_sync_probe.py --op status/probe` 输出规则模式及条目数量，不输出 token。这里的“通过准入”只表示路径规则允许，实际工作区仍须存在，后端仍须可用。
