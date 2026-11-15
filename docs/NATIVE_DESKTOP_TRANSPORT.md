# Experimental native Desktop transport

This opt-in transport is for **new sessions owned by the patched Desktop's own engine**.
It does not attach, resume or migrate sessions created by an independent Bridge engine.
It is separate from the existing `probe` and `sync` operations, which remain compatible.

Adapter implementers should use the short [Native Host integration contract](NATIVE_HOST_INTEGRATION.md).

The implementation is `runtime/desktop_sync/native_transport.mjs`. The installer embeds
that module into the already version/hash-gated host patch. It uses public
`ZCodeAgentService` methods and the host's native command-envelope helper. It does not
expose the service, a raw RPC method, a connection ID, an evaluator or a process launcher.
The original installation, engine patch gates, byte-exact backup and restore workflow
are unchanged. These tools do not start Desktop or perform GUI acceptance.

## Enable in a private configuration

Prepare a copy and private `control.json` using the existing Desktop shim instructions.
Native operations are disabled by default. For an explicitly chosen private config:

```text
python scripts/desktop_sync_shim.py native-config --config <private-control.json> --mode enabled
python scripts/desktop_sync_shim.py native-call --config <private-control.json> --request-file <private-request.json>
python scripts/desktop_sync_shim.py native-config --config <private-control.json> --mode disabled
```

The running shim rereads only this opt-in field on native requests, so disabling it does
not restart or stop a task. The patched host must include this transport version. The
workspace must exactly match the existing configured workspace allowlist. Use a separate
private config and ownership file for each Desktop instance. Never include these files
or request files in releases; they can contain local IDs and prompt text.

All native requests include `op`, `workspace` and the **current**
`caller_permission_mode`: `full_access`, `review_important_actions`, `ask_before_writes`,
`always_ask` or `read_only`. These map to native `yolo`, `edit`, `build`, `build` and
`plan`. The mode is never inferred from a prior session. Missing permission fails before
any native call. A send under weaker permissions changes and verifies the idle native
session's mode first. A differing mode on an active session fails with
`permission_change_while_running`; it does not send under the old access level.

## Request contract

| Operation | Additional request fields | Native method / behavior |
| --- | --- | --- |
| `native_model_catalog` | `request_id`, optional `model:{providerId,modelId}`, optional explicit `thought_level` | Deferred no-input plan probe; reads requested model row's native `reasoning`; only switches the probe if metadata is missing and explicit effort is supplied; finally `closeSession` |
| `native_create` | `request_id`, `model`, explicit `thought_level` string or null, optional `project_id` | Validates real model/effort via a probe, then `createSession` with exact selection and current caller mode; no first prompt |
| `native_send` | `request_id`, `session_id`, `text` | Checks actual model/effort and current permission; empty newly created sessions use native `sendPrompt`/`session/send` once, later durable sessions use v4 `sendText`; returns admission, not completion |
| `native_snapshot` | `session_id` | Native `readSession`, `runtimePolicy:existing-only` |
| `native_messages` | `session_id`, optional `after_message_id`, `limit` | Native `readSessionMessages`, forward direction |
| `native_events` | `session_id`, optional `after_seq`, `limit` | Native `readSessionEvents`; forwards native events, not synthesized frames |
| `native_lifecycle` | `session_id` | Native lifecycle observation only; never activates or restores the runtime |
| `native_resolve` | `request_id`, `session_id`, `interaction_id`, `answer` | Native v4 `resolveInteraction`; use actual interaction and option IDs |
| `native_cancel` | `request_id`, `session_id` | Native v4 foreground `stop`; `work_id` background cancellation is explicitly unsupported |
| `native_workflows` | `session_id`, optional `limit` | Native `conversationWorkflowRunsV4` |
| `native_workflow_events` | `session_id`, `run_id`, optional `after_sequence`, `limit` | Verifies run belongs to the owned parent, then native journal page |
| `native_children` | `session_id`, optional `limit` | Native `listSessionSubagents`, with verified parent; grants no child write ownership |
| `native_child_messages` | owned parent `session_id`, `child_session_id`, optional `run_id`, `after_message_id`, `limit` | Verified ordinary child or DWF actor content as a direct array; no independent-engine fallback |
| `native_child_snapshot` | owned parent `session_id`, `child_session_id`, optional `run_id` | Verified child snapshot only when already ready; no automatic resume |
| `native_topology` | `session_id`, `run_id`, optional `after_sequence`, `limit` | Verified native journal and subagent facts; does not invent a graph or activity status |
| `native_pending` | `request_id` | Private request outcome lookup; without a request ID reports unsupported, not an invented question queue |

The `native_resolve.answer` object admits only `optionId`, `freeText`, `action` and
`content`. `action` uses the native values `accept`, `decline`, `cancel`. The transport
re-reads the owned session and only answers an interaction that is really pending; a
permission answer must use one of that request's real option IDs. `fullAccess` maps to
yolo in 3.18.2 and is refused for any caller below `full_access`; other allow-like
choices below `full_access` require the separate `user_approved` flag. In read-only
mode only an explicit deny/cancel is accepted; granting permissions is refused.
`AskUserQuestion` stays a user-input interaction, not a generic permission approval.
The native snapshot remains the source for actual pending interaction state.

Example private create request (the identifiers below are documentation placeholders):

```json
{
  "op": "native_create",
  "workspace": "<allowed-absolute-workspace>",
  "request_id": "unique-create-request-001",
  "caller_permission_mode": "ask_before_writes",
  "model": {"providerId": "<from-native-catalog>", "modelId": "<from-native-catalog>"},
  "thought_level": "<exact-native-effort>"
}
```

`thought_level:null` is valid only if the selected model exposes no native effort
choices. There is no fallback model or invented `auto` effort. The send operation uses
the selection verified at creation; if Desktop has changed it, send fails with a model
or effort mismatch rather than replacing the user's selection. This MVP does not expose
session model/mode setters, a background-task cancel RPC or a general saved-workflow
catalog RPC. Unsupported capabilities remain explicit.

`native_messages.result` and `native_events.result` are direct arrays; the public Et
methods already unwrap their protocol response objects. `native_snapshot.result` is
the snapshot itself. Runtime-dependent reads can return native `-32004` with safe
`nativeError.data.code:"runtimeNotReady"`. Use lifecycle observation and persisted
message reads as separate facts; restore an owned session through the user's native
Desktop UI when required. The transport never automatically resumes a session on a
read error, and an unloaded lifecycle is not a terminal-success signal.

Child reads require the owned parent plus explicit child ID. Membership must be in
the native `childSessionIds` array; `running` and `ended.items` are status/pagination
views, not substitute authorization lists. The exact child's native summary must
also match the parent, local workspace and `subagent_child` kind. Engine identity
is checked throughout. Reads never add child write ownership. An unlisted DWF actor
requires `run_id`: its already-ready owned parent's native run summaries, matching
`run-launched.parentSessionId`, actual `actor-created.actor`, native exact SID
mapping and an actual SDK row with `workflow_child`/parent/workspace must all agree.
Journal scanning is bounded to 8 pages of 100 events. Missing or malformed evidence
is unsupported/rejected; no name or SID prefix substitutes for the proof and no
foreign-engine fallback or runtime activation is allowed. The short integration
contract documents the source functions and exact verification sequence.

The catalog preserves each native model row's `reasoning.levels` and `defaultLevel`.
The default is informational and is never selected automatically. `thoughtLevels`
describes the requested model, with `thoughtLevelsSource` identifying the native row or
selected native snapshot. `selected` remains the actual probe selection;
`requestedModel` identifies the catalog target and need not equal `selected`.
If explicit effort is still needed, `selectionStatus` is `requires-selection`.
When row metadata is missing for a different model and no effort is supplied,
`thoughtLevels:null` means unknown, not that the model has no reasoning support.
A fallback `setModel` includes only the caller's explicit non-null
`model.options.reasoningLevel`; its actual snapshot model/effort and available levels
are checked. `native_create` already requires an explicit effort or null and passes it
through this validation. A failed prior request is never retried by this change.

For ordinary sends, Desktop 3.18.2's strict native `Wc` selection schema explicitly
preserves `options.reasoningLevel`. The separate strict `modelExecution` schema carries
execution scope, authorization and subagent policy; it has no reasoning-level field.
The transport builds the exact selection shape from the verified model and effort,
including when a snapshot stores effort separately from its current model reference.
This compatibility safeguard does **not** diagnose or establish that a real send failure
has been fixed; a snapshot that already contains the same option produces the same
selection. Actual dispatch still requires a separate native/GUI acceptance check.

### First input and the native input-ledger foreign key

In Desktop 3.18.2, legacy `session/create` constructs an in-memory runtime whose
`persistence` label may be `immediate`, while Core's `sessionPersisted` is still false.
V4 `handleCommand` calls `admitCommandInput` before executing `sendText`.
`admitInputCommand` only ensures the session row first when the runtime label is
`deferred`; for an empty `immediate` runtime it reaches the native `F0n` insert directly.
That insert writes `session_input.session_id`, whose foreign key references
`session.id`. With no session row it deterministically fails before any model input
can start. The command client ID is inside the JSON payload, not a foreign key or a
required registered-client row.

The public `Et.sendPrompt` method uses `session/send`. Its native `dPi` handler starts
`rjc`/`app.sendInput` without this earlier V4 ledger insert. Core's first turn then calls
`ensureSessionPersisted` before persisting the user message and emitting `TurnStarted`.
The transport uses that existing native first-input route only for sessions created
by this version in the same verified engine runtime, with durable `firstInput:not-sent`
ownership metadata, an explicitly empty message list, `turnCount:0`, idle native state,
no active turn and no pending request/background job. It passes the verified
model/effort, stable input/query IDs and the snapshot state revision. It changes no
SQL table and does not create a hidden bootstrap prompt.

The first response reports `admissionProtocol:"session/send"` and the native
`nativeAdmission` (`accepted`, `sessionId`, `stateRevision`). This is an admission ACK,
not evidence of successful model execution or completion. Later sends use V4 only
after a native message or positive native turn count proves the session was persisted.
An accepted first ACK without that observation returns `first_input_not_yet_observed`
on a subsequent send. Older ownership records are not silently declared eligible for
first-input fallback. A transport loss/crash during the first dispatch persistently
blocks both the same request ID and a fresh ID from resending that first input. An
explicit JSON-RPC rejection remains a known `failed` outcome. All existing ownership,
permission/model validation and diagnostic ACK rules continue to apply.

Known native rejection responses include a `nativeAck` with only bounded scalar
`commandId`, `status`, `reasonCode` and sanitized `message` fields. No arbitrary result,
authorization material or stack object is forwarded. Common credential patterns and an
exact echoed input/free-text answer are redacted. A known ACK rejection remains
`phase:command_acknowledged`, `status:failed`; transport loss after dispatch remains
`unknown`. Diagnostic messages are returned to the caller, not persisted in ownership
state. Keep a private immediate response if diagnosis is needed; replay/pending lookup
returns the durable outcome metadata and does not resend the command.

## Ownership and uncertain outcomes

Before dispatch, each mutating request is atomically recorded in the private ownership
file with its request ID, operation, caller permission, fingerprint and stable command
ID. The file stores no prompts, answers, transcript or provider configuration. A known
native SID is recorded immediately when returned; no SID is guessed or supplied by the
caller. Only successfully verified normal sessions created by this shim are sendable.
Catalogue-only sessions can never be sent to, including if closing a probe fails.

Reusing the same request ID returns its recorded outcome and **never dispatches again**.
Changed input or permission with that ID returns `request_id_conflict`. After a transport
failure following dispatch, the status is `unknown`; lookup with `native_pending` and
keep the same ID. Do not automatically retry under a new ID. A crash can leave `pending`,
which is also not permission to retry. A confirmed ACK means accepted/duplicate/noop,
not that a model turn or workflow completed.

Persistent ownership is additionally bound to the native engine's actual runtime
identity and generation. After an engine replacement these SIDs return `stale_owner`;
the transport does not silently cold-resume them. A foreign SID is rejected before any
native read, workflow query or model command. Child and run facts do not grant write
ownership. The parent/run verification is limited to the first 64 native run summaries;
an older run outside that page fails closed.

The host's public model catalog requires a temporary `mode:plan`, `persistence:deferred`
session. Desktop's `prepareSessionCreation` still calls `reserveCreatedTask` for this
probe. **Its index reservation may remain after confirmed deferred close**, including
a row with no CLI creation timestamp or project ID. A confirmed close is not evidence
that this host-side reservation was removed. `closeSession` is always attempted with
`expectedPersistence:deferred`, and its confirmation is reported. Et exposes no public
deferred-only reservation cancellation, so this transport does not remove index rows,
edit a database, or delete a real session to hide the side effect. A timeout with an
unknown probe SID cannot be safely cleaned up or retried.

Successful create responses label their source `desktop-native-host`, include actual
model/effort/mode/workspace/runtime and native index facts, and set `guiVerified:false`.
Index facts are not evidence that a GUI panel was checked. Normal task execution and
UI streaming use Desktop's existing same-engine events. This MVP adds no conversation
subscription and performs no recurring forced hydration. Message/event reads are native
queries; they are not a replacement stream of fabricated v4 projection frames.

## Verification and rollback

`tests/native_transport.test.mjs` imports the real module and supplies a fake public
service. It exercises native method arguments, exact model/effort validation, probe
cleanup, persisted ownership, unknown outcomes, request reuse, runtime replacement,
foreign-SID isolation, permission downgrade, native journal forwarding and unsupported
capabilities. `tests/test_native_transport.py` runs this suite during the normal Python
tests. No Desktop, engine, GUI or login is started by these tests.

`tests/test_native_input_ledger.py` executes verbatim native base-session DDL, the final
session-input DDL and the `F0n` insert in in-memory SQLite with foreign keys enabled.
It reproduces the precise pre-persistence failure, verifies that normal session
persistence removes it, and confirms that arbitrary client labels are JSON payload
rather than foreign keys. The portable fixture records the source bundle hash.
The ESM behavior suite separately verifies the first-input native method, later V4
route, model/effort/current-permission arguments, eligibility gates and uncertain ACKs.

GUI acceptance and integration with the later Bridge transport remain the root
operator's separate checks. Disable the private opt-in first; restore the backed-up
host through the existing shim restore command when desired. Existing model jobs are
not migrated or stopped by toggling the opt-in.
