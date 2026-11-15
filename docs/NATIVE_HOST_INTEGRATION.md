# Native Host adapter contract (Desktop 3.18.2)

This is the fixed integration boundary for an adapter using the experimental Host
transport. It does not attach an independent engine's sessions. See
[NATIVE_DESKTOP_TRANSPORT.md](NATIVE_DESKTOP_TRANSPORT.md) for implementation and SQL evidence.

## Request and response

Use the existing authenticated loopback control connection (NDJSON): authenticate with
the private config token first, then send one request object. The private native opt-in
must be enabled. Do not read provider credentials or expose this connection as raw RPC.

Every request has `op`, the exact allowed absolute `workspace`, and the **current**
`caller_permission_mode`. Writes/catalog probes also require a unique `request_id`
matching `[A-Za-z0-9_-]{8,128}`. Owned-session operations require `session_id`.

Success: `{ok:true, source:"desktop-native-host", result:<shape below>}`.
Failure: `{ok:false, source, error:<string or native integer>, detail, nativeError?, nativeAck?, request?}`.
Disabled/not-ready control-server errors can omit `source`. Check `ok` before reading
`result`; do not convert an error to an empty successful result.

## Fixed operations

All result shapes are **inside `result`**. Optional parameters are marked `?`.

| Operation | Extra parameters | Result shape / source |
| --- | --- | --- |
| `native_model_catalog` | `request_id`, `model?`, `thought_level?` | `{available:[ModelRow],selected,requestedModel?,thoughtLevels:[string] or null,thoughtLevelsSource,selectionStatus,catalogueProbe}`; native deferred plan probe |
| `native_create` | `request_id`, `model:{providerId,modelId}`, explicit `thought_level:string or null`, `project_id?` | `{sessionId,ownedSID,workspace,model,thoughtLevel,mode,runtime,snapshot,desktopIndexFacts,guiVerified:false,catalogueProbe}`; public `Et.createSession` |
| `native_send` | `request_id`, `text` | `{status,sessionId,commandId,model,thoughtLevel,mode,...}`; first input adds `admissionProtocol:"session/send",nativeAdmission:{accepted,sessionId,stateRevision?}`; later sends retain native v4 ACK fields |
| `native_snapshot` | none | Native **snapshot object**, with `session`, `settings`, `projection`, `runtime`, `messages`, etc.; `Et.readSession` |
| `native_messages` | `after_message_id?`, `limit?` | **Message array directly**, e.g. `[{info,parts}]`; `Et.readSessionMessages` has already unwrapped wire `{messages}` |
| `native_events` | `after_seq?`, `limit?` | **Event array directly**, e.g. `[{seq,type,payload,...}]`; `Et.readSessionEvents` already unwraps wire `{events}` |
| `native_lifecycle` | none | `{sessionId,state,generation,revision,lastError}`; `Et.getSessionLifecycle`, observation only |
| `native_resolve` | `request_id`, `interaction_id`, `answer`, `user_approved?` | Native v4 ACK; `answer` only admits `optionId`, `freeText`, `action`, `content`; `user_approved` is the explicit current-turn user approval for allow-like permission choices |
| `native_cancel` | `request_id` | Native v4 foreground `stop` ACK; not a terminal-completion claim |
| `native_workflows` | `limit?` | `{runs:[NativeRun]}`; native workflow summaries, default 16 / maximum 64 |
| `native_workflow_events` | `run_id`, `after_sequence?`, `limit?` | `{events:[NativeWorkflowEvent],hasMore}`; verifies run appears in owned parent's first 64 summaries |
| `native_children` | `limit?` | `{verifiedParentSessionId,writeOwnershipGranted:false,native:{revision,childSessionIds,running,ended:{total,items,...}}}` |
| `native_child_messages` | `child_session_id`, `run_id?`, `after_message_id?`, `limit?` | **Message array directly** for a verified ordinary child or DWF actor; `session_id` is the owned parent |
| `native_child_snapshot` | `child_session_id`, `run_id?` | Verified child's **snapshot object** if already ready, otherwise native `-32004`; no resume |
| `native_topology` | `run_id`, `after_sequence?`, `limit?` | `{runId,verifiedParentSessionId,events:<native journal page>,children:<native subagent response>,representation}`; raw facts, not a synthesized graph |
| `native_pending` | `request_id` (no SID needed) | `{request:<durable request metadata>,automaticRetry:false}`; request outcomes, **not** a question queue |

In particular, parse messages as `response["result"]`, not
`response["result"]["messages"]`. Keep the existing direct-array wire contract.
Snapshot `settings.model.available` can be limited to the current model; use the model
catalog operation for selection. Never infer a model or effort from a label/default.

## Permission, model and ownership rules

`full_access → yolo`, `review_important_actions → edit`,
`ask_before_writes/always_ask → build`, `read_only → plan`. Send verifies the actual
model/effort and applies the current mode while idle. An active session with a different
mode fails rather than running under an older grant. In read-only mode resolution only
admits explicit `decline` or `cancel`. Use actual native interaction/option IDs.

`native_resolve` re-reads the owned session's actual pending interaction and refuses a
stale/foreign `interaction_id`. A permission answer must name an `optionId` from that
request's real `options` (or its `fullAccessOption`); unknown options are rejected.
Native 3.18.2 option rows carry `kind: allowOnce|allowAlways|deny|custom`, and the
`fullAccess` option maps to yolo in the engine. That option is therefore allowed only
when the current caller really is `full_access`; no answer or `user_approved` flag can
cross a weaker grant. Every other non-deny choice below `full_access` requires
`user_approved:true`, read-only permits only deny/cancel, and permission answers do not
accept `content`/`freeText`. `AskUserQuestion` remains a real user-input interaction and
is never treated as a generic permission approval.

Catalog rows preserve native `reasoning.levels/defaultLevel`; defaults are informational.
`requires-selection` plus `thoughtLevels:null` means unknown, not no reasoning support.
Explicit null is valid for creation only when the selected model has no native efforts.
Catalog probes are never sendable. Confirmed deferred close may leave a host index
reservation; neither the adapter nor this transport deletes it.

Only this shim's verified normal SIDs are writable/readable, bound to their original
workspace and native engine identity/generation. Child/run facts grant no write
ownership. Foreign SIDs fail before native session calls. Engine replacement yields
`stale_owner`; there is no automatic reattach or migration.

Child reads stay on this native transport. They require the owned parent
`session_id` plus `child_session_id`. Verification uses its actual native
`childSessionIds` array from `{childSessionIds,running,ended:{total,items}}`, then
`Et.listSessions` for that exact child in the same workspace, requiring matching
`parentSessionId` and `sessionKind:"subagent_child"`. Runtime identity is checked
before and after content access. The complete ID array remains authoritative when
an older child is outside the `ended.items` page. Reading never inserts the child
into ownership state and never grants send/resolve/cancel/model permissions.

Native 3.18.2 filters this ordinary list to persisted `subagent_child` records
derived from the parent's actual tools/events. For an unlisted DWF actor, supply
`run_id` for the following separate proof; a bare candidate SID is unsupported:

1. The parent must be owned in the original native runtime and already lifecycle
   `ready`. Its native workflow summaries must contain that exact run ID.
2. Fetch that run's native journal from the same parent. Require a nontruncated
   `run-launched` event with matching `parentSessionId` and a real `actor-created`
   event containing exactly `actor:{siteId:string,ordinal:positive integer}`.
3. Match the requested child using native `Fqe/B7n/rc/h0`'s exact actor mapping,
   never a human name, node instance, caller-supplied actor object or SID prefix.
4. Query the exact candidate through the same SDK's `Et.listSessions`. Require
   `sessionKind:"workflow_child"`, the actual matching parent and local workspace;
   recheck runtime identity before/after content access.

Journal verification is bounded to 8 pages of 100 events with strictly increasing
sequence cursors. Incomplete/truncated/malformed evidence is rejected. Run lookup
uses the parent's first 64 native run summaries. Native source anchors are
`createOwnedActorSession` (which checks journaled vs minted SID), `Fqe/B7n/rc/h0`,
`mNt` (sets `workflow_child`, parent and inherited working directory), Core `Tec`
(persists those fields), and `sPi/nVn` (filters actual rows by workspace/lineage).
The native progress form `vCe` uses the same SID mapping; raw journal events alone
do not contain a trusted top-level run ID, so the bound native query matters.

Parent/workspace/kind mismatches fail before content is read. **Never fall back
to an independent engine for refused or unavailable native child reads.** No
child receives write ownership. Snapshots use public `Et.readSession`; many Core
children are not ready protocol-session runtimes, so `-32004/runtimeNotReady` is
preserved rather than activating them. An unavailable parent is not restored for
DWF verification.

## Released runtime and saved history

`native_snapshot` and `native_events` need a ready session runtime. Native `-32004`
can include `nativeError.data.code:"runtimeNotReady"`. Lifecycle states are exactly
`unloaded`, `starting`, `ready`, `stopping`, `faulted`; `lastError` is native data.
This observation does not identify why the runtime became unavailable or prove task
completion. `native_lifecycle` never activates it.

Forward `native_messages` can read persisted history while the session runtime is
unloaded. Saved messages are not a live snapshot or an event stream. Do not synthesize
an idle/completed state from their existence. Ask the user to open/restore the owned
session through native Desktop UI when live snapshot/dispatch is required. This
transport does **not** call `resumeSession`, `ensureSessionReady`, restart, or cold-resume
another engine's session on a read error. Do not add such a fallback in the adapter.

## Idempotency and uncertain writes

Persist the mutation request ID before calling the Host. Reuse the identical complete
body for an outcome lookup/replay; a changed body or permission with the same ID returns
`request_id_conflict`. A replay returns `{replay:true,request,automaticRetry:false}`
instead of the original possibly large result. Obtain a confirmed SID from
`request.sessionId`, then read it separately. Never guess a SID.

`pending` or `unknown` is not permission to resend under a new ID. Socket timeout after
dispatch can leave either; use `native_pending` for the same ID. The first-input path
also persistently blocks a fresh ID after its uncertain dispatch. Known v4 ACK rejection
is `failed/command_acknowledged`; a known native JSON-RPC first-input rejection is
`failed/legacy_first_input_rejected`. Only an explicitly rejected new case may be
considered for a separate intentional attempt; there is no automatic write retry.

An accepted ACK is admission, not successful execution or completion. First sends use
native `session/send` only for eligible newly created empty idle SIDs. Later v4 dispatch
requires native messages or positive turn count as persistence evidence. A first ACK
without that observation returns `first_input_not_yet_observed` on another send.

`nativeAck` preserves bounded sanitized rejection scalars only. `nativeError` preserves
the native numeric code/message and safe `data.code/reasonCode/recoverable`; arbitrary
headers, auth, stacks and objects are omitted. Diagnostic messages are not persisted in
ownership state, so retain immediate failure responses privately when needed.

## Unsupported / acceptance boundary

No raw RPC, model/mode setter, arbitrary connection ID, background-task `work_id` cancel,
generic pending-question queue, foreign SID reads, automatic runtime restoration,
cross-engine migration, tool-catalog injection or DWF child-question emulation is
provided. `native_pending` without `request_id` reports unsupported. Do not silently
route an unsupported operation to an independent engine.

Creation/index facts do not prove GUI visibility (`guiVerified` remains false). The root
operator separately observed same-page native streaming and tool execution through
completion without refresh; that acceptance does not change the semantics of an ACK,
an unavailable runtime or this adapter's future operations.
