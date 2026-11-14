"""Message identity and visibility across ZCode 3.17 and 3.18.

ZCode 3.18 returns ``messages[].info.messageId`` while 3.17 used
``messages[].info.id``.  Every anchor, cursor and reconciliation path must use
the same fallback order.  The helper is intentionally dependency-free so both
``service`` and ``notifications`` can import it without a cycle.
"""


def message_identity(message):
    """Return a stable message id across ZCode 3.17 (info.id) and 3.18 (info.messageId)."""
    info = message.get('info') if isinstance(message, dict) else None
    info = info if isinstance(info, dict) else {}
    candidates = [info.get('id'), info.get('messageId')]
    if isinstance(message, dict):
        candidates.extend([message.get('id'), message.get('messageId')])
    return next((value for value in candidates if value not in (None, '')), None)


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

