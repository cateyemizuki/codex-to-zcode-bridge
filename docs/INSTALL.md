# 安装与排错

## 两种安装方案

手动：克隆仓库，或下载对应侧的 Release ZIP，在解压目录运行 `python install.py`。完整仓库可加 `--target codex`、`--target zcode` 或 `--target both`。

agent 协助：将根 README 的安装提示词交给拥有本机文件和命令能力的 agent。它应先读 `AGENTS.md`，识别自己的客户端，运行 dry-run 后安装，保留你现有配置。安装阶段不要默认创建完成订阅或开启自动续工；这些只在用户明确要求并按当前 chat 授权后使用。

## 路径与本机配置

发布包不保存固定 Python、ZCode、Node 或工作区路径。安装器在本机生成绝对路径的 MCP 文件，以避免不同 Python 环境混淆；这些文件属于本地安装结果。

自动寻找顺序包括显式环境变量、系统注册信息、常见应用目录。找不到时传入 `--zcode-executable "<实际可执行文件>"`；只有引擎位置已知时传 `--engine-entry "<实际 zcode.cjs>"`。`--workspace` 要求现有目录，不会创建任意工作区。

可选环境变量：`ZCODE_INSTALL_DIR`、`ZCODE_EXECUTABLE`、`ZCODE_ENGINE_ENTRY`、`ZCODE_NODE_EXECUTABLE`、`ZCODE_WORKSPACE`、`ZCODE_BUILTIN_PROVIDER_FILE`、`ZCODE_PERSONAL_PROVIDER_FILE`、`ZCODE_MODEL_PROVIDER`、`ZCODE_MODEL_ID`。它们是本机输入，不应放进 Git。

默认状态目录是当前用户本地应用数据目录下的 `CateyeZCodeBridge`；其他平台使用当前用户的 `.local/share`。可用 `ZCODE_BRIDGE_STATE` 指定隔离状态目录。这个目录含连接令牌、会话记录、完成通知的订阅与事件，以及宿主启动上下文，应保留在本机。仓库根的 `.zcode/` 是本机客户端状态，已被忽略，不进入发布包或审计范围。

模型鉴权由本机 ZCode 的模型配置提供。桥接只读取模型元数据，不把密钥作为工具结果返回，也不把 provider 文件复制到安装包。

## 完成通知的本机状态

完成通知使用状态目录中的 `notifications.json`（schema 1），保存订阅、事件、通知本地序号、宿主能力缓存和投递状态。事件只含 session/event ID 和最小状态；来源 thread 绑定与宿主启动上下文只留在本机状态或内存，不写入事件、日志或工具返回。

- 事件默认保留 7 天或最多 500 条；终态订阅默认保留 7 天或最多 100 条，活动订阅默认 6 小时后过期。过期事件不再投递，但已确认或已投递的记录会按上限裁剪。
- 写入使用临时文件替换。崩溃可能丢失最后一次状态跃迁；重启后以引擎证据和固定轮次锚点恢复，不会因为本地时间或重试产生第二个完成事件。
- 文件损坏或 schema 版本不匹配时，旧文件会改名为 `notifications.incompatible.<随机后缀>.json` 隔离，桥接以空存储启动并在状态中报告 `store_incompatible`；不会静默覆盖原文件。
- 桥接重启后，事件和订阅仍在，观察器重新 resume/回放；宿主能力缓存清空。投递中但尚未尝试发送的租约过期会回到 `pending`；已经尝试但未确认的转为 `uncertain`，不自动重发。宿主管道变化时需要重新探测或重新订阅。
- 观察器保存 native 游标。重启后如果引擎流或序号重置，会从 0 重读并按流标识、序号和 payload 去重，避免把旧事件当成新的完成；固定事件 ID 在恢复时保持不变。
- `zcode_notify_inbox` 是权威读取路径。拉取收件箱不是唤醒；toast、MCP 日志、工具返回和 `delivered` 都不代表模型已读或已续工。

完成通知的轮询间隔、轮次启动等待、订阅过期和唤醒尝试次数在原生设置或内嵌面板中调整，默认分别为 1 秒、120 秒、6 小时和 5 次。它们只影响观察和投递，不授予唤醒或自动续工权限；自动续工必须在单个订阅上显式授权。

## 手动注册与直接市场安装

安装器通常会调用客户端 CLI。需要自行注册时先运行 `--no-register`，使用输出中的市场根目录：

```powershell
codex plugin marketplace add "<安装器输出的市场根目录>"
codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins
```

ZCode 侧使用当前安装的引擎 CLI：

```powershell
node "<本机 zcode.cjs>" plugins marketplace add "<市场根目录>" --json
node "<本机 zcode.cjs>" plugins install codex-to-zcode-bridge@codex-to-zcode-bridge-plugins --json
```

熟悉 Python 环境的用户也可以直接从仓库市场安装 Codex 侧：先 `python -m pip install ./plugins/codex`，让客户端使用的 `python` 能导入 `zcode_bridge`；再 `codex plugin marketplace add cateyemizuki/codex-to-zcode-bridge` 和 `codex plugin add codex-to-zcode-bridge@codex-to-zcode-bridge-plugins`。当前 Codex 版本不会给 MCP 服务展开插件根目录变量，因此这一步 Python 包准备不能省略；普通用户也可使用安装器，自动生成本机脚本路径。

ZCode 侧使用自己的市场命令。其宿主支持插件根目录变量，会启动包内服务，无需额外 pip 安装；两侧可共用同一个控制服务。

## ChatGPT 桌面列表未发现插件

新聊天或重载插件后再检查。全新的个人插件目录可由安装器登记；已存在且属于其他市场的个人目录会保留，不会被覆盖。可以让 agent 按 [官方本地市场规则](https://developers.openai.com/plugins/build/plugins) 将输出的插件路径合并进已有个人目录，并保留原来的市场名与其他条目。自定义安装位置在用户目录之外时，优先通过 CLI 注册本地市场，不写越出个人市场根目录的相对路径。

客户端需支持本地 stdio MCP；原生设置和 MCP App 还需要相应客户端功能。功能不可用时，可以继续通过 MCP 工具配置，或明确要求打开本地浏览器配置页。未提供网页版连接。完成通知的宿主唤醒还要求 Codex Desktop 提供第一方 `codex-app-tools` 标准 MCP 服务；这是版本相关能力，运行时探测，缺失或握手失败时事件仍可在收件箱读取。

## 升级与移除

从新版仓库或 ZIP 重新执行安装器。相同安装位置会先备份原插件再更新；已有配置与 provider 凭据保留。两侧使用同一个控制服务，升级不要强制结束正在运行的会话。已运行的旧控制服务可能仍使用旧代码；先等该服务管理的任务结束，再重新载入桥接服务。可从状态返回的 `bridge_version` 检查实际版本；这不需要重启 ZCode 桌面。

升级到带完成通知的版本时，旧服务进程不会自动获得新的观察器和投递线程。重新载入桥接服务后：持久事件和订阅仍在，待送队列保留，宿主能力缓存清空；已确认或已投递的唤醒不会重放。新订阅会记录当前 chat 的授权；不要从旧版本或旧会话推断自动续工授权。若宿主管道在重启后变化，先重新探测，仍失败时在来源 chat 重新订阅。真实端到端唤醒仍需在真实 Codex Desktop chat 上验收。

移除时先用对应客户端的插件管理功能卸载该插件，再删除你确认属于此安装的目录。安装器不会自动删除用户工作区、ZCode 数据库或模型配置，也不会删除状态目录中的通知事件。不同市场名的旧开发版本需单独检查，避免两个版本同时暴露重复工具。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 找不到引擎、Node 或 CLI | 检查实际安装路径，使用相应参数；先 `--dry-run` |
| 尚未选择默认模型 | 读取真实模型列表，在面板选择或创建时传入模型 |
| 配置面板暂时连接不上 | 路径设置仍可打开，修正后重试，不重装作者机器的配置 |
| 会话已保存但桌面列表没有 | 检查 `desktop.indexed`；独立引擎不实时推送到已打开项目 |
| ZCode 获得焦点 | 只有明确的工作区打开才应发生，不用该操作刷新列表 |
| 首次动态工作流仍要求授权 | 正常读取并处理真实请求，完全访问不能伪造该步骤 |
| 收件箱有事件但没有自动回到 chat | 收件箱是拉取路径；先看 `zcode_notify_status` 的宿主能力。`unsupported`/`pipe_unavailable` 时事件保留，重新探测或重新订阅，不要把拉取说成已唤醒 |
| 宿主唤醒失败、超时或握手失败 | 事件保留在待送队列并按退避重试；检查本机 Codex Desktop 是否提供第一方 MCP 的 `send_message_to_thread`，跨版本不保证 |
| 订阅显示 `send_uncertain` | 发送可能已经执行；不要自动重发，只有用户明确要求时才用 `retry=true`，可能产生重复 |
| 重启后没有继续收到新事件 | 重新探测并检查订阅是否过期；在来源 chat 重新 `zcode_notify_subscribe` 或重新派发 |
| 一个 session 返回 `session_busy` 或 `session_busy_foreign` | 已有活动订阅持有该 session；等完成或在来源 chat 取消，不能从其他 chat 抢占 |
| 完成通知区域没有取消按钮 | 设计如此；取消必须回到来源 chat 调用 `zcode_notify_cancel`，该区域只做只读聚合 |
| 通知存储不兼容 | 旧文件已被隔离；读取 `zcode_notify_status` 的 `store_incompatible`，确认后再重新订阅 |
| 官方订阅鉴权刷新不可用 | 采用已配置且桥接能使用的个人模型提供方，不复制桌面账号令牌 |
