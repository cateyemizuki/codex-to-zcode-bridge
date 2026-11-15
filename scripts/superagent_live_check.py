#!/usr/bin/env python3
"""Explicit live smoke test for the high-level superagent API.

Preflight (the default) only reads the bridge version, the workspace and the
real model catalogue.  Creating a session requires all of:

    --workspace <existing absolute directory>
    --source-thread <current chat/thread id>
    --confirm-live

The live path performs delegate -> inspect -> message -> inspect -> cancel and
prints JSON evidence.  With --ask-roundtrip the delegated ZCode main session
asks a harmless native AskUserQuestion, the script waits for the real pending
request in inspect, answers that exact request_id/option, and confirms the
session continued before cleanup.  A native DWF child has no AskUserQuestion
tool, so its contract is to return structured needs_input to the main session,
which relays the question; this entry does not claim a child asked directly.
It never opens a desktop project, focuses a window, retries an uncertain send
or decides that a child self-report is verified.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from zcode_bridge import __version__, storage, transport


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get('ok') else 2


def _models(workspace):
    result = transport.local_call('zcode_models', {'workspace': workspace}, timeout=120)
    rows = result.get('available') if isinstance(result, dict) else []
    summary = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        ref = row.get('ref') or {}
        levels = [level.get('value') for level in (row.get('reasoning') or {}).get('levels') or ()
                  if isinstance(level, dict) and level.get('value')]
        summary.append({'providerId': ref.get('providerId') or '',
                        'modelId': ref.get('modelId') or '',
                        'label': row.get('label') or '',
                        'levels': levels})
    return summary


def preflight(workspace):
    cfg = storage.config()
    path = str(Path(workspace or cfg.get('default_workspace') or '').expanduser().resolve())
    result = {
        'ok': False,
        'mode': 'preflight',
        'bridge_version': __version__,
        'workspace': path,
        'workspace_exists': Path(path).is_dir() if path else False,
        'models': [],
        'model_count': 0,
        'live_requires': ['--workspace', '--source-thread', '--confirm-live'],
        'note': 'preflight 不创建持久会话；live 需要显式确认。',
    }
    if not result['workspace_exists']:
        result['error'] = 'workspace_required: 默认工作区不存在，请显式传入 --workspace'
        return result
    try:
        models = _models(path)
    except Exception as error:
        result['error'] = 'model_catalogue_failed: ' + str(error)[:500]
        return result
    result['models'] = models
    result['model_count'] = len(models)
    result['ok'] = True
    return result


def _call(name, arguments, context, timeout):
    return transport.local_call(name, arguments, context=context, timeout=timeout)


QUESTION_MARKER = 'LIVE-CHECK-QUESTION'
CHECK_ANSWERS = ('CHECK-A', 'CHECK-B')
# The delegated ZCode main session asks directly.  A native DWF child has no
# AskUserQuestion tool of its own; its contract is to return structured
# needs_input to the main session, which relays the question to the parent.
QUESTION_TASK = (
    '先使用原生 AskUserQuestion 向调用方提一个问题：'
    + QUESTION_MARKER + '：请选择 CHECK-A 或 CHECK-B？'
    '选项只有 CHECK-A 和 CHECK-B，然后等待回答。\n'
    '收到回答后，只回复 QUESTION-ANSWER: <你实际收到的选项值>，不要修改任何文件。\n'
)


def _find_check_question(pending):
    """Return the real pending request containing our check question."""
    for row in pending or ():
        if not isinstance(row, dict):
            continue
        if QUESTION_MARKER in json.dumps(row, ensure_ascii=False, default=str):
            return row
    return None


def _check_answer(request, wanted):
    """Build an answer for the real request using the chosen actual option."""
    wanted_value = str(wanted or '').strip()
    for option in request.get('options') or ():
        label = str(option.get('label') or '')
        value = str(option.get('value') or '')
        if wanted_value and wanted_value in (label, value) and option.get('option_id'):
            return {'option_id': str(option['option_id'])}
    questions = request.get('questions') or ()
    if questions:
        first = questions[0]
        for choice in first.get('options') or ():
            if not isinstance(choice, dict):
                continue
            label = str(choice.get('label') or '')
            value = str(choice.get('value') or '')
            if wanted_value and wanted_value in (label, value):
                return {'response': {'action': 'accept', 'content': {'answers': {
                    str(first.get('question') or QUESTION_MARKER):
                    value or label}}}}
    return None


def _echoed_answer(call, session_id, before_id, wanted, context, timeout):
    """Wait for a new visible assistant reply echoing the chosen answer."""
    arguments = {'session_id': session_id, 'limit': 50, 'max_chars': 20000}
    if before_id:
        arguments['after_message_id'] = before_id
    try:
        result = call('zcode_messages', arguments, context, timeout)
    except Exception:
        return False
    marker = 'QUESTION-ANSWER: ' + str(wanted)
    for message in result.get('messages') or ():
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        for part in message.get('parts') or ():
            if isinstance(part, dict) and marker in str(part.get('text') or ''):
                return True
    return False


def await_question(call, agent_id, session_id, context, timeout, wait_seconds,
                   wanted='CHECK-A'):
    """Wait for the real question, answer it, then require the echoed reply."""
    deadline = time.time() + max(10, wait_seconds)
    seen = []
    while time.time() < deadline:
        inspected = call('zcode_agent_inspect', {'agent_id': agent_id}, context, timeout)
        pending = inspected.get('pending') or []
        seen = pending
        request = _find_check_question(pending)
        if request is not None:
            answer = _check_answer(request, wanted)
            if answer is None:
                return {'state': 'question_without_matching_option',
                        'request': request, 'wanted': wanted}
            before_id = str(inspected.get('last_message_id') or '')
            answer.update({'agent_id': agent_id,
                           'request_id': request.get('request_id')})
            answered = call('zcode_agent_answer', answer, context, timeout)
            echo_deadline = time.time() + max(10, wait_seconds)
            while time.time() < echo_deadline:
                if _echoed_answer(call, session_id, before_id, wanted,
                                  context, timeout):
                    follow = call('zcode_agent_inspect', {'agent_id': agent_id},
                                  context, timeout)
                    return {'state': 'answered', 'echoed': True,
                            'wanted': wanted, 'request': request,
                            'answer': answered, 'inspect_after_answer': follow}
                time.sleep(2)
            return {'state': 'answer_not_echoed', 'echoed': False,
                    'wanted': wanted, 'request': request, 'answer': answered}
        if pending:
            return {'state': 'pending_without_check_question', 'pending': pending}
        time.sleep(2)
    return {'state': 'timeout_waiting_for_question', 'pending': seen}


def live(args, caller=None):
    call = caller or _call
    workspace = str(Path(args.workspace).expanduser().resolve())
    source = str(args.source_thread or os.environ.get('CODEX_THREAD_ID') or '').strip()
    context = {'thread_id': source, 'thread_origin': 'live_check_explicit',
               'request_id': 'live-check-' + uuid.uuid4().hex[:12]}
    task = args.task or (QUESTION_TASK if args.ask_roundtrip
                         else '只回复 LIVE-CHECK-OK，不要修改任何文件。')
    followup = args.message or 'LIVE-CHECK-FOLLOWUP：只回复收到，不要修改任何文件。'
    key = args.idempotency_key or ('live-' + time.strftime('%Y%m%d%H%M%S') + '-'
                                   + uuid.uuid4().hex[:8])
    delegate_args = {
        'task': task,
        'workspace': workspace,
        'caller_permission_mode': args.permission,
        'idempotency_key': key,
        'label': args.label or 'live-check',
    }
    if args.profile:
        delegate_args['profile'] = args.profile
    if args.model_provider or args.model_id:
        if not (args.model_provider and args.model_id):
            return {'ok': False, 'mode': 'live', 'error': 'model_required: providerId 与 modelId 必须同时提供'}
        delegate_args['model'] = {'providerId': args.model_provider,
                                  'modelId': args.model_id}
    if args.thought_level:
        delegate_args['thought_level'] = args.thought_level
    if args.workflow_mode:
        delegate_args['workflow_mode'] = args.workflow_mode

    report = {'ok': False, 'mode': 'live', 'agent_id': None, 'session_id': None,
              'source_thread': source, 'workspace': workspace,
              'idempotency_key': key, 'steps': []}
    agent_id = None
    try:
        delegate = call('zcode_delegate', delegate_args, context, args.timeout)
        report['steps'].append({'step': 'delegate', 'result': delegate})
        agent_id = delegate.get('agent_id')
        report['agent_id'] = agent_id
        report['session_id'] = delegate.get('session_id')
        inspected = call('zcode_agent_inspect', {'agent_id': agent_id}, context,
                         args.timeout)
        report['steps'].append({'step': 'inspect', 'result': inspected})
        send_state = (delegate.get('send') or {}).get('state')
        verdict = str(inspected.get('verdict') or '')
        question_ok = True
        if args.ask_roundtrip and send_state == 'accepted':
            asked = await_question(call, agent_id, report['session_id'], context,
                                   args.timeout, args.question_wait,
                                   args.check_answer)
            report['steps'].append({'step': 'question_roundtrip', 'result': asked})
            question_ok = (asked.get('state') == 'answered'
                           and asked.get('echoed') is True)
            if not question_ok:
                report['ok'] = False
                report['question_state'] = asked.get('state')
        if send_state == 'accepted' and verdict not in (
                'verified', 'failed', 'cancelled', 'expired', 'stalled',
                'unobservable') and question_ok:
            message = call('zcode_agent_message', {
                'agent_id': agent_id, 'content': followup,
                'caller_permission_mode': args.permission,
                'idempotency_key': key + '-message',
            }, context, args.timeout)
            report['steps'].append({'step': 'message', 'result': message})
            report['steps'].append({'step': 'inspect_after_message', 'result':
                                    call('zcode_agent_inspect', {'agent_id': agent_id},
                                         context, args.timeout)})
        else:
            report['steps'].append({'step': 'message',
                                    'skipped': 'send_not_accepted_or_terminal',
                                    'send_state': send_state, 'verdict': verdict})
        report['ok'] = send_state == 'accepted' and question_ok
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)[:800]
    finally:
        if agent_id and not args.keep_session:
            try:
                cancelled = call('zcode_agent_cancel', {
                    'agent_id': agent_id,
                    'caller_permission_mode': args.permission,
                    'reason': 'live-check-cleanup',
                }, context, args.timeout)
                report['steps'].append({'step': 'cancel', 'result': cancelled})
                if not cancelled.get('cancelled'):
                    report['ok'] = False
                    report['cleanup'] = 'not_cancelled'
            except Exception as error:
                report['ok'] = False
                report['cleanup_error'] = type(error).__name__ + ': ' + str(error)[:500]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='ZCode superagent live check (preflight by default; explicit confirmation required to create a session).')
    parser.add_argument('--workspace', help='Existing absolute workspace. Required for live.')
    parser.add_argument('--source-thread', default='',
                        help='Current chat/thread id. Required for live (or CODEX_THREAD_ID).')
    parser.add_argument('--confirm-live', action='store_true',
                        help='Explicitly allow creating one real ZCode session and sending the task.')
    parser.add_argument('--task', help='Task text for the live delegation.')
    parser.add_argument('--message', help='Follow-up text for the live message step.')
    parser.add_argument('--permission', default='always_ask',
                        choices=['full_access', 'review_important_actions',
                                 'ask_before_writes', 'always_ask', 'read_only'])
    parser.add_argument('--profile')
    parser.add_argument('--model-provider')
    parser.add_argument('--model-id')
    parser.add_argument('--thought-level')
    parser.add_argument('--workflow-mode', choices=['disabled', 'onDemand', 'alwaysOn'])
    parser.add_argument('--idempotency-key')
    parser.add_argument('--label')
    parser.add_argument('--ask-roundtrip', action='store_true',
                        help='Run a native DWF child AskUserQuestion -> inspect pending -> zcode_agent_answer regression before cleanup.')
    parser.add_argument('--question-wait', type=float, default=180.0,
                        help='Seconds to wait for the real pending question (default 180).')
    parser.add_argument('--check-answer', default='CHECK-A', choices=list(CHECK_ANSWERS),
                        help='Actual option to answer and verify echoed as QUESTION-ANSWER.')
    parser.add_argument('--timeout', type=float, default=120.0)
    parser.add_argument('--keep-session', action='store_true',
                        help='Do not cancel the session in cleanup (debug only).')
    args = parser.parse_args(argv)

    if args.confirm_live:
        source = str(args.source_thread or os.environ.get('CODEX_THREAD_ID') or '').strip()
        if not args.workspace:
            return _json({'ok': False, 'mode': 'live',
                          'error': 'workspace_required: live 必须显式给 --workspace'})
        if not Path(args.workspace).expanduser().is_dir():
            return _json({'ok': False, 'mode': 'live',
                          'error': 'workspace_required: 工作区不存在'})
        if not source:
            return _json({'ok': False, 'mode': 'live',
                          'error': 'source_required: live 必须给 --source-thread 或 CODEX_THREAD_ID'})
        try:
            return _json(live(args))
        except Exception as error:
            return _json({'ok': False, 'mode': 'live',
                          'error': type(error).__name__ + ': ' + str(error)[:800]})
    try:
        return _json(preflight(args.workspace))
    except Exception as error:
        return _json({'ok': False, 'mode': 'preflight',
                      'error': type(error).__name__ + ': ' + str(error)[:800]})


if __name__ == '__main__':
    raise SystemExit(main())
