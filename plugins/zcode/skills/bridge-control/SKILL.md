---
name: bridge-control
description: Inspect the local Codex to ZCode Bridge, configure it, or explicitly administer another bridge session. Use for bridge administration, not ordinary coding.
---

Use the `zcode_*` MCP tools to read configured model IDs, manage another selected session, inspect main and child messages, and answer real pending questions and permissions. Read the current configuration first; all paths and selected models belong to this installation.

For normal coding, use native tools in the current ZCode session. Do not recursively create yourself through the bridge. For child agents with distinct models or reasoning, normally use native dynamic workflows and let ZCode load its own full workflow skill.

Mutations require the initiating host's actual current `caller_permission_mode`; do not infer elevation from an older session. Answer real native requests with their stable request IDs and actual options. Leave genuine user choices to the user.

The bridge uses an independent engine. Do not claim desktop visibility from successful persistence, and do not use native workspace opening for polling: it can bring ZCode to the foreground. Open an external settings browser only when explicitly requested.
