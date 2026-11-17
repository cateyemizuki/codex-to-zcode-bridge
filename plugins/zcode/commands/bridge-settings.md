---
description: Inspect and configure the local Codex to ZCode Bridge without affecting unrelated sessions.
---

Read `zcode_settings` and `zcode_models` to inspect this machine's configuration. The four completion-notification operation values (poll interval, start timeout, expiry hours, wake attempts) are part of `zcode_settings`; they tune observation and delivery only and never grant wake or auto-continue authorization. Use `zcode_open_settings` for a supported embedded panel. If the host cannot render it, explain the available tool-based settings; open the external browser fallback only on an explicit user request. Creating, reading, acknowledging or cancelling a completion subscription is not a settings action and must stay in the originating chat; the panel is read-only and offers no cancel. Do not recursively create another session to run this command.

Workflow authorization is configured separately through `workflow_authorization_mode`: `automatic` (default) lets the ZCode-side service choose the actual session-scoped workflow consent without a Codex connection; `manual` leaves the full native choices to the user. This does not grant notification wake or answer unrelated questions. Read the runtime policy state before claiming it is active; an older native host must be updated and reloaded after ongoing work ends.
