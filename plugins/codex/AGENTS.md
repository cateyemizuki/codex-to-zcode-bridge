# GPT / Codex 侧 agent 指引

- 安装当前侧：先 `python install.py --dry-run`，再 `python install.py`。使用本机检测值，不引入其他机器的路径或模型编号。
- 使用 `zcode_*` MCP 工具；本侧是纯 MCP 插件，不需要 GPT skill。
- 先读取真实模型。未知默认模型时让用户选择；每次变更按当前会话的真实权限传入。
- 需要独立子代理模型/强度时通常使用原生动态工作流。自然语言说明阶段、依赖、模型、强度和验收，不复制完整 ZCode skill。
- 保留 session ID，等待终态，读取真实问题和授权。不要把 accepted 当成完成。
- 分别报告核心持久化与桌面索引；不自动打开工作区抢焦点，不重启无关任务。
- 当前目录是生成的独立分发包。开发请修改仓库 `runtime/` 后重新运行 `build.py`，勿仅改本包再丢失改动。
- 根仓库 [AGENTS.md](https://github.com/cateyemizuki/codex-to-zcode-bridge/blob/main/AGENTS.md) 提供其他工具环境的分类说明。
