---
description: Inspect and configure the local Codex to ZCode Bridge without affecting unrelated sessions.
---

Read `zcode_settings` and `zcode_models` to inspect this machine's configuration. The four completion-notification operation values (poll interval, start timeout, expiry hours, wake attempts) are part of `zcode_settings`; they tune observation and delivery only and never grant wake or auto-continue authorization. Use `zcode_open_settings` for a supported embedded panel. If the host cannot render it, explain the available tool-based settings; open the external browser fallback only on an explicit user request. Creating, reading, acknowledging or cancelling a completion subscription is not a settings action and must stay in the originating chat; the panel is read-only and offers no cancel. Do not recursively create another session to run this command.
