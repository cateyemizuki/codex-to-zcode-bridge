"""Shared public tool catalogue; both plugin hosts get exactly this surface."""

STRING = {'type': 'string'}
BOOL = {'type': 'boolean'}
INTEGER = {'type': 'integer'}
OBJECT = {'type': 'object'}
MODEL = {'type': 'object', 'properties': {'providerId': STRING, 'modelId': STRING},
         'required': ['providerId', 'modelId'], 'additionalProperties': False}
CALLER = {'type': 'string', 'enum': ['full_access', 'review_important_actions', 'ask_before_writes', 'always_ask', 'read_only'],
          'description': 'Actual current GPT host/session permission. Full access -> ZCode yolo. Never infer full access from old session settings.'}
SID = {'session_id': STRING}
WRITE = {**SID, 'caller_permission_mode': CALLER}
NOTIFY_CONSENT = {'type': 'object', 'properties': {
    'completion_notify': BOOL, 'auto_continue': BOOL, 'user_authorized': BOOL},
    'required': ['user_authorized'], 'additionalProperties': False}
NOTIFY_WRITE = {**WRITE, 'user_authorized': BOOL}


def tool(name, description, properties=None, required=(), read_only=False):
    return {'name': name, 'description': description,
        'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False},
        'annotations': {'readOnlyHint': read_only, 'destructiveHint': False, 'openWorldHint': not read_only}}


TOOLS = [
    tool('zcode_status', 'Connect to the real local ZCode engine and report its state. Starts ZCode automatically when configured and absent.', read_only=True),
    tool('zcode_launch', 'Ensure ZCode Desktop is running. Optional workspace opens the native project and may bring ZCode to the foreground; requires allow_focus=true and an explicit user request. Never use workspace opening for polling or automatic refresh.',
         {'workspace':STRING, 'allow_focus':BOOL}),
    tool('zcode_models', 'Read configured ZCode models, exact provider/model IDs and supported reasoning levels; never returns credentials.', {'workspace': STRING}, read_only=True),
    tool('zcode_create_session', 'Create an independent main-agent session in an existing workspace without opening or focusing a desktop project. The returned desktop.indexed reports separate desktop indexing, not visual confirmation. Inherit the current GPT permission mode. Dynamic workflows default to onDemand.',
         {'workspace': STRING, 'model': MODEL, 'thought_level': STRING, 'caller_permission_mode': CALLER,
          'workflow_mode': {'type': 'string', 'enum': ['disabled', 'onDemand', 'alwaysOn']}, 'label': STRING}, ('workspace', 'caller_permission_mode')),
    tool('zcode_sessions', 'List persisted ZCode sessions, optionally scoped to a workspace.', {'workspace': STRING, 'limit': INTEGER}, read_only=True),
    tool('zcode_read_session', 'Read main-agent state, selected model, permissions, pending work and compact visible messages.', {**SID, 'max_chars': INTEGER}, ('session_id',), True),
    tool('zcode_send', 'Send exact content to a selected main session. With notify={completion_notify,auto_continue,user_authorized} the bridge persists an arming subscription before session/send and requires idempotency_key; accepted=true only means the engine accepted the message, never that the turn completed. send_uncertain requires an explicit retry=true. Use natural-language dynamic workflow instructions to choose child models/effort. Reapply current GPT permission on every send.',
         {**WRITE, 'content': STRING, 'notify': NOTIFY_CONSENT, 'idempotency_key': STRING,
          'retry': BOOL}, ('session_id', 'content', 'caller_permission_mode')),
    tool('zcode_messages', 'Read main or child-agent messages using the actual session ID, with cursor pagination. Default excludes reasoning and compresses tool metadata.',
         {**SID, 'after_message_id': STRING, 'limit': INTEGER, 'max_chars': INTEGER, 'raw': BOOL}, ('session_id',), True),
    tool('zcode_subagents', 'Read child-session IDs and child status. Use zcode_messages on each returned childSessionId for its content.',
         {**SID, 'cursor': STRING, 'limit': INTEGER, 'workflow_run_limit':INTEGER}, ('session_id',), True),
    tool('zcode_set_model', 'Change a main session model and optional reasoning level. Child models must be chosen through a dynamic workflow, not ordinary subagent inheritance.',
         {**WRITE, 'model': MODEL, 'thought_level': STRING}, ('session_id', 'model', 'caller_permission_mode')),
    tool('zcode_set_permissions', 'Synchronize the selected session to the actual current GPT permission, including later downgrades.', WRITE, ('session_id', 'caller_permission_mode')),
    tool('zcode_pending', 'Read unanswered native ZCode questions and permission prompts, including first-use dynamic-workflow authorization. Never approve them blindly.', SID, read_only=True),
    tool('zcode_answer', 'Respond on the original live request channel. For permissions choose an actual option_id, or pass its documented response. Genuine user preferences need the user answer.',
         {'request_id': STRING, 'option_id': STRING, 'response': OBJECT, 'caller_permission_mode': CALLER, 'user_approved': BOOL}, ('request_id', 'caller_permission_mode')),
    tool('zcode_wait', 'Wait up to 20 seconds for the same persisted terminal truth used by notifications. waiting, error, paused, accepted and the first idle are never success; the result contains verdict and confirmed. Without an active subscription this returns confirmed=false.',
         {**SID, 'timeout_seconds': {'type': 'number'}, 'subscription_id': STRING}, ('session_id',), True),
    tool('zcode_events', 'Read incremental bridge notifications, or persisted native events when session_id is given. after_cursor is the native sequence number for session reads. Prefer compact messages for prose.', {**SID, 'after_cursor': INTEGER, 'limit': INTEGER}, read_only=True),
    tool('zcode_workflows', 'Read native dynamic-workflow definitions/runs, journal events/topology and node results. events/workspace_nodes/node_result require session_id+run_id; event pagination uses after_sequence. Scripts omitted by default.',
         {'operation': {'type': 'string', 'enum': ['runs', 'list', 'get', 'for_run', 'events', 'workspace_nodes', 'node_result', 'session_runs', 'topology']}, 'params': OBJECT,
          'session_id': STRING, 'workspace': STRING, 'run_id': STRING, 'name': STRING,
          'after_sequence': INTEGER, 'site_id': STRING, 'ordinal':INTEGER,
          'scope': {'type':'string','enum':['project','global']}, 'limit':INTEGER, 'include_script':BOOL}, read_only=True),
    tool('zcode_stop', 'Stop only the specified session turn. When a notification subscription owns the session, only the source chat of that owner may stop it; cancel only that chat\'s subscriptions, never another chat\'s.',
         WRITE, ('session_id', 'caller_permission_mode')),
    tool('zcode_notify_subscribe', 'Attach a durable completion subscription to a running turn or active background work. Binds the session, the current chat source from request _meta, the turn anchor and the explicit user authorization. It does not send a message or change ZCode permissions. auto_continue is rejected in read_only.',
         {**SID, 'caller_permission_mode': CALLER, 'completion_notify': BOOL, 'auto_continue': BOOL,
          'user_authorized': BOOL, 'input_id': STRING, 'user_message_id': STRING,
          'after_message_id': STRING}, ('session_id', 'caller_permission_mode', 'user_authorized')),
    tool('zcode_notify_cancel', 'Cancel this source chat\'s subscription and any undelivered wake. Recorded events are preserved; calling twice is idempotent. A chat cannot cancel another chat\'s subscription.',
         {'subscription_id': STRING, 'session_id': STRING, 'caller_permission_mode': CALLER},
         ('caller_permission_mode',)),
    tool('zcode_notify_inbox', 'Read the minimal persisted events for this source chat, by exact event_id/event_ids or by the local notification cursor. Pulling the inbox is not a host wake. Never returns thread ids, pipe paths, content or model ids.',
         {'event_id': STRING, 'event_ids': {'type': 'array', 'items': STRING}, 'after_cursor': INTEGER,
          'limit': INTEGER, 'session_id': STRING, 'include_delivery': BOOL}, read_only=True),
    tool('zcode_notify_ack', 'Acknowledge this source chat\'s events, or requeue failed/uncertain deliveries when the user asks. Acking an undelivered event cancels that wake; inbox_only events are never requeued.',
         {'event_ids': {'type': 'array', 'items': STRING}, 'up_to_cursor': INTEGER,
          'retry_failed': BOOL, 'caller_permission_mode': CALLER}, ('caller_permission_mode',)),
    tool('zcode_notify_status', 'Read this source chat\'s subscription/delivery state and host capability. Read-only: never probes or wakes. Never returns thread ids, paths or pipe values.',
         {'subscription_id': STRING, 'session_id': STRING}, read_only=True),
    tool('zcode_notify_probe', 'Probe whether the version-specific Codex Desktop host exposes send_message_to_thread through the standard MCP stdio tools/list handshake. Read-only, rate limited, never calls tools/call and never sends a message.',
         {'timeout_seconds': {'type': 'number', 'minimum': 1, 'maximum': 20}}, read_only=True),
    tool('zcode_notify_emit', 'ZCode-side hint for progress, action_required or needs_verification. Requires the current source chat context and an active subscription for that session; with subscription_id it targets only that subscription, otherwise it broadcasts to every active subscription of the session, never to a guessed "most recent" one. It triggers one verification poll and never declares completion.',
         {'session_id': STRING, 'subscription_id': STRING,
          'kind': {'type': 'string', 'enum': ['progress', 'action_required', 'needs_verification']},
          'hint_id': {'type': 'string', 'minLength': 1, 'maxLength': 64},
          'reason_code': {'type': 'string', 'enum': ['needs_input', 'blocked', 'progress', 'verification_requested']},
          'caller_permission_mode': CALLER}, ('session_id', 'kind', 'hint_id', 'reason_code', 'caller_permission_mode')),
    tool('zcode_settings', 'Read bridge settings or update selected fields. Runtime path changes take effect on the next engine connection; model defaults affect new sessions.',
         {'changes': OBJECT, 'caller_permission_mode': CALLER}, read_only=False),
    tool('zcode_open_settings', 'Open the embedded ChatGPT/Codex configuration and session panel. Defaults to in-app MCP UI. open_browser=true explicitly opens the separate local browser fallback.', {'open_browser':BOOL}, read_only=True),
]
