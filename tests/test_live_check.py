import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'superagent_live_check.py'
SPEC = importlib.util.spec_from_file_location('superagent_live_check', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _args(**overrides):
    values = {
        'workspace': str(Path(__file__).resolve().parents[1]),
        'source_thread': 'chat-live',
        'task': None,
        'message': None,
        'idempotency_key': 'key-live',
        'permission': 'always_ask',
        'profile': None,
        'model_provider': None,
        'model_id': None,
        'thought_level': None,
        'workflow_mode': None,
        'label': None,
        'timeout': 5.0,
        'keep_session': False,
        'ask_roundtrip': True,
        'question_wait': 30.0,
        'check_answer': 'CHECK-A',
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class LiveCheckQuestionTests(unittest.TestCase):
    def test_default_ask_roundtrip_uses_the_main_session(self):
        self.assertFalse(MODULE.QUESTION_TASK.lstrip().startswith('/workflow'))
        self.assertIn('AskUserQuestion', MODULE.QUESTION_TASK)
        self.assertIn(MODULE.QUESTION_MARKER, MODULE.QUESTION_TASK)
        self.assertIn('CHECK-A', MODULE.QUESTION_TASK)
        self.assertIn('CHECK-B', MODULE.QUESTION_TASK)

    def test_finds_only_the_real_check_question(self):
        pending = [{'request_id': 'req-1', 'tool_name': 'AskUserQuestion',
                    'questions': [{'question': MODULE.QUESTION_MARKER + '：选哪个？',
                                   'options': [{'label': 'CHECK-A', 'value': 'CHECK-A'},
                                               {'label': 'CHECK-B', 'value': 'CHECK-B'}]}],
                    'options': [{'option_id': 'opt-a', 'label': 'CHECK-A'},
                                {'option_id': 'opt-b', 'label': 'CHECK-B'}]}]
        found = MODULE._find_check_question(pending)
        self.assertEqual(found['request_id'], 'req-1')
        self.assertEqual(MODULE._check_answer(found, 'CHECK-A'),
                         {'option_id': 'opt-a'})
        self.assertEqual(MODULE._check_answer(found, 'CHECK-B'),
                         {'option_id': 'opt-b'})
        self.assertIsNone(MODULE._check_answer(found, 'CHECK-C'))
        self.assertIsNone(MODULE._find_check_question([
            {'request_id': 'req-2', 'questions': [{'question': '别的'}]}]))

    def test_falls_back_to_response_when_no_option_id(self):
        request = {'request_id': 'req-1',
                   'questions': [{'question': 'LIVE-CHECK-QUESTION：选哪个？',
                                  'options': [{'label': 'CHECK-A', 'value': 'CHECK-A'},
                                              {'label': 'CHECK-B', 'value': 'CHECK-B'}]}]}
        answer = MODULE._check_answer(request, 'CHECK-B')
        self.assertIn('response', answer)
        self.assertEqual(answer['response']['action'], 'accept')
        self.assertEqual(answer['response']['content']['answers'],
                         {'LIVE-CHECK-QUESTION：选哪个？': 'CHECK-B'})

    def test_live_roundtrip_uses_real_request_and_option(self):
        calls = []
        state = {'inspect': 0}

        def fake_call(name, arguments, context, timeout):
            calls.append((name, dict(arguments or {})))
            if name == 'zcode_delegate':
                return {'agent_id': 'agent-1', 'session_id': 'sess-1',
                        'send': {'state': 'accepted'},
                        'subscription': {'armed': True}}
            if name == 'zcode_agent_inspect':
                state['inspect'] += 1
                if state['inspect'] == 1:
                    return {'pending': [], 'verdict': 'running'}
                return {'pending': [], 'verdict': 'running'}
            if name == 'zcode_agent_answer':
                return {'answered': True}
            if name == 'zcode_agent_message':
                return {'state': 'queued'}
            if name == 'zcode_agent_cancel':
                return {'cancelled': True}
            return {}

        pending = [{'request_id': 'req-1', 'tool_name': 'AskUserQuestion',
                    'questions': [{'question': 'LIVE-CHECK-QUESTION：是否继续？',
                                   'options': [{'label': '继续'}]}],
                    'options': [{'option_id': 'opt-yes', 'label': '继续'}]}]
        with patch.object(MODULE, 'await_question',
                          return_value={'state': 'answered', 'echoed': True}) as waiter:
            report = MODULE.live(_args(), caller=fake_call)
        self.assertTrue(report['ok'])
        waiter.assert_called_once()
        self.assertEqual(calls[0][0], 'zcode_delegate')
        self.assertEqual(calls[0][1]['task'], MODULE.QUESTION_TASK)
        names = [step['step'] for step in report['steps']]
        self.assertIn('question_roundtrip', names)
        self.assertIn('cancel', names)

    def _question_call(self, echo='CHECK-A', seen=None):
        seen = seen if seen is not None else {'inspect': 0, 'messages': 0}

        def fake_call(name, arguments, context, timeout):
            if name == 'zcode_agent_inspect':
                seen['inspect'] += 1
                if seen['inspect'] == 1:
                    return {'pending': [], 'last_message_id': 'm-before'}
                return {'pending': [{'request_id': 'req-9',
                                     'questions': [{'question': 'LIVE-CHECK-QUESTION：选哪个？',
                                                    'options': [{'label': 'CHECK-A'},
                                                                {'label': 'CHECK-B'}]}],
                                     'options': [{'option_id': 'opt-a', 'label': 'CHECK-A'},
                                                 {'option_id': 'opt-b', 'label': 'CHECK-B'}]}],
                        'last_message_id': 'm-before'}
            if name == 'zcode_agent_answer':
                self.assertEqual(arguments['request_id'], 'req-9')
                self.assertEqual(arguments['option_id'], 'opt-a')
                return {'answered': True}
            if name == 'zcode_messages':
                seen['messages'] += 1
                text = ('QUESTION-ANSWER: ' + echo) if echo else '只是普通回复'
                return {'messages': [{'id': 'm-after', 'role': 'assistant',
                                      'parts': [{'type': 'text', 'text': text}]}]}
            return {}

        return fake_call

    def test_await_question_waits_for_echo_and_accepts_it(self):
        with patch.object(MODULE.time, 'sleep', return_value=None):
            result = MODULE.await_question(
                self._question_call('CHECK-A'), 'agent-1', 'sess-1', {}, 5.0, 30.0,
                'CHECK-A')
        self.assertEqual(result['state'], 'answered')
        self.assertTrue(result['echoed'])

    def test_await_question_rejects_missing_or_wrong_echo(self):
        with patch.object(MODULE.time, 'sleep', return_value=None):
            result = MODULE.await_question(
                self._question_call('CHECK-B'), 'agent-1', 'sess-1', {}, 5.0, 30.0,
                'CHECK-A')
        self.assertEqual(result['state'], 'answer_not_echoed')
        self.assertFalse(result['echoed'])
        self.assertEqual(result['answer'], {'answered': True})


if __name__ == '__main__':
    unittest.main()
