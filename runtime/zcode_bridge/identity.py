"""Message identity and visibility across ZCode 3.17 and 3.18.

ZCode 3.18 returns ``messages[].info.messageId`` while 3.17 used
``messages[].info.id``.  Every anchor, cursor and reconciliation path must use
the same fallback order.  The helper is intentionally dependency-free so both
``service`` and ``notifications`` can import it without a cycle.
"""
import hashlib
import json


def message_identity(message):
    """Return a stable message id across ZCode 3.17 (info.id) and 3.18 (info.messageId)."""
    info = message.get('info') if isinstance(message, dict) else None
    info = info if isinstance(info, dict) else {}
    candidates = [info.get('id'), info.get('messageId')]
    if isinstance(message, dict):
        candidates.extend([message.get('id'), message.get('messageId')])
    return next((value for value in candidates if value not in (None, '')), None)


def message_input_ids(message):
    """Read actual input-id echoes, including native V4 user intent metadata.

    Only the fixed identifier fields are used. Prompt text, timestamps and
    assistant-authored metadata never substitute for a user input echo.
    """
    if not isinstance(message, dict):
        return set()
    info = message.get('info') if isinstance(message.get('info'), dict) else {}
    result = {str(info[key]) for key in ('inputId', 'clientInputId', 'requestId', 'userInputId')
              if info.get(key)}
    result.update(str(message[key]) for key in ('inputId', 'clientInputId') if message.get(key))
    if (info.get('role') or message.get('role')) == 'user':
        metadata = info.get('metadata') if isinstance(info.get('metadata'), dict) else {}
        for key in ('inputIntent', 'conversationInputIntent'):
            intent = metadata.get(key)
            if isinstance(intent, dict) and isinstance(intent.get('sourceCommandId'), str) and intent['sourceCommandId']:
                result.add(intent['sourceCommandId'])
    return result


def is_visible_message(message):
    """True when a transcript entry may be shown or used as an anchor.

    ZCode 3.18 emits synthetic ``total_tokens`` user records with
    ``info.synthetic`` and ``semantics.uiVisibility == 'hidden'``.  They must
    never become subscription anchors, completion evidence or pagination
    cursors, and they are already excluded from compact transcripts.
    """
    info = message.get('info') if isinstance(message, dict) else None
    info = info if isinstance(info, dict) else {}
    if info.get('synthetic') or info.get('visibility') == 'model-only':
        return False
    semantics = info.get('semantics')
    return not (isinstance(semantics, dict) and semantics.get('uiVisibility') == 'hidden')


def message_kind(message):
    """Return ``semantics.kind`` for a transcript entry, or an empty string."""
    info = message.get('info') if isinstance(message, dict) else None
    info = info if isinstance(info, dict) else {}
    semantics = info.get('semantics')
    return str(semantics.get('kind') or '') if isinstance(semantics, dict) else ''


def is_assistant_response(message):
    """True for a real assistant reply, never a timeline/metadata event.

    ZCode 3.18 keeps model-change and similar rows as assistant-role
    ``timeline_event`` entries.  Only ``assistant_response`` rows may anchor a
    completed turn; old shapes without ``semantics.kind`` remain accepted.
    """
    info = message.get('info') if isinstance(message, dict) else None
    info = info if isinstance(info, dict) else {}
    role = info.get('role') or (message.get('role') if isinstance(message, dict) else '')
    if role != 'assistant' or not is_visible_message(message):
        return False
    return message_kind(message) in ('', 'assistant_response')


def message_activity_digest(message):
    """Return a body-free fingerprint of one message's visible activity.

    Streaming engines may extend the same message id while tools or text
    advance.  Only role, ordered part types, tool names/states and text lengths
    are hashed; the text itself is never returned or persisted.  An empty
    string means there is no visible, comparable activity.
    """
    if not isinstance(message, dict):
        return ''
    info = message.get('info') if isinstance(message.get('info'), dict) else {}
    role = str(info.get('role') or message.get('role') or '')
    parts = message.get('parts') if isinstance(message.get('parts'), list) else []
    rows = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get('type') or '')
        text = part.get('text')
        if isinstance(text, str):
            rows.append([kind, 'text', len(text),
                         hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]])
            continue
        tool = part.get('tool') if isinstance(part.get('tool'), dict) else {}
        name = (part.get('toolName') or part.get('name')
                or tool.get('name') or tool.get('toolName') or '')
        state = (part.get('state') or part.get('status')
                 or tool.get('state') or tool.get('status') or '')
        rows.append([kind, str(name)[:48], str(state)[:24]])
    if not rows:
        return ''
    payload = json.dumps([role, rows], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]
