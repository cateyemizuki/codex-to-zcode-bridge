import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import storage
from zcode_bridge.superagent import (CONTRACT_VERSION, MAX_MESSAGE_CHARS,
                                     MAX_QUEUE_ITEMS, MAX_REPORT_BYTES, RESULT_FIELDS,
                                     SuperagentError, SuperagentStore,
                                     _installation_roots, bounded_report_payload,
                                     build_task_prompt, resolve_profile, resolve_workspace)

ALPHA = {'providerId': 'acme', 'modelId': 'alpha'}
BETA = {'providerId': 'acme', 'modelId': 'beta'}
CATALOG = {'available': [
    {'ref': dict(ALPHA), 'label': 'Alpha', 'providerLabel': 'Acme',
     'reasoning': {'levels': [{'value': 'low'}, {'value': 'high'}], 'defaultLevel': 'low'}},
    {'ref': dict(BETA), 'label': 'Beta', 'providerLabel': 'Acme',
     'reasoning': {'levels': [{'value': 'max'}], 'defaultLevel': 'max'}},
]}


class SuperagentFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.default_workspace = root / 'work'
        self.profile_workspace = root / 'profile-work'
        self.other_workspace = root / 'other'
        for path in (self.default_workspace, self.profile_workspace, self.other_workspace):
            path.mkdir()
        self.plugin_root = root / 'plugin'
        (self.plugin_root / 'zcode_bridge').mkdir(parents=True)
        self.defaults = {
            'default_model': dict(ALPHA),
            'default_thought_level': 'high',
            'default_workflow_mode': 'onDemand',
            'default_workspace': str(self.default_workspace),
            'superagent_profiles': {},
            'superagent_default_profile': '',
        }
        self.profiles = {'fast': {
            'model': dict(BETA),
            'thought_level': 'max',
            'workspace': str(self.profile_workspace),
            'workflow_mode': 'alwaysOn',
        }}
        self.store = SuperagentStore(Path(root) / 'state' / 'superagent.json')

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, explicit=None, bound='', defaults=None, profiles=None):
        return resolve_profile(
            explicit=explicit or {}, bound_profile=bound,
            profiles=self.profiles if profiles is None else profiles,
            defaults=self.defaults if defaults is None else defaults,
            catalog=CATALOG)

    def source(self, thread='thr_a', origin='turn_metadata'):
        return {'thread_id': thread, 'thread_origin': origin, 'request_id': 'req-1',
                'client_name': 'test', 'client_version': '1', 'host_id': 'host-1'}

    def create(self, thread='thr_a', key='idem-1', resolved=None, task='do the work',
               origin='turn_metadata'):
        return self.store.create_job(
            self.source(thread, origin), resolved or self.resolve(),
            caller_permission_mode='always_ask', idempotency_key=key, task_text=task)


class ProfileResolutionTests(SuperagentFixture):
    def test_explicit_then_profile_then_default_sources(self):
        resolved = self.resolve({'model': dict(ALPHA), 'thought_level': 'low'}, bound='fast')
        self.assertEqual(resolved['model'], ALPHA)
        self.assertEqual(resolved['thought_level'], 'low')
        self.assertEqual(resolved['workspace'], str(self.profile_workspace.resolve()))
        self.assertEqual(resolved['workflow_mode'], 'alwaysOn')
        self.assertEqual(resolved['sources']['model'], 'explicit')
        self.assertEqual(resolved['sources']['thought_level'], 'explicit')
        self.assertEqual(resolved['sources']['workspace'], 'profile')

        inherited = self.resolve(bound='fast')
        self.assertEqual(inherited['model'], BETA)
        self.assertEqual(inherited['thought_level'], 'max')
        self.assertEqual(inherited['workflow_mode'], 'alwaysOn')
        self.assertEqual(inherited['workspace'], str(self.profile_workspace.resolve()))
        self.assertEqual(inherited['sources']['model'], 'profile')

        defaulted = self.resolve()
        self.assertEqual(defaulted['model'], ALPHA)
        self.assertEqual(defaulted['thought_level'], 'high')
        self.assertEqual(defaulted['workspace'], str(self.default_workspace.resolve()))
        self.assertEqual(defaulted['sources']['model'], 'default')

    def test_default_profile_is_used_when_nothing_is_bound(self):
        defaults = {**self.defaults, 'superagent_default_profile': 'fast'}
        resolved = self.resolve(defaults=defaults)
        self.assertEqual(resolved['profile'], 'fast')
        self.assertEqual(resolved['model'], BETA)

    def test_unknown_profile_fails_explicitly(self):
        with self.assertRaises(SuperagentError) as caught:
            self.resolve(bound='missing')
        self.assertEqual(caught.exception.code, 'profile_unknown')

    def test_unknown_model_is_never_silently_replaced(self):
        with self.assertRaises(SuperagentError) as caught:
            self.resolve({'model': {'providerId': 'acme', 'modelId': 'not-there'}})
        self.assertEqual(caught.exception.code, 'model_not_in_catalog')

    def test_missing_model_requires_a_real_selection(self):
        defaults = {**self.defaults, 'default_model': {'providerId': '', 'modelId': ''}}
        with self.assertRaises(SuperagentError) as caught:
            self.resolve(defaults=defaults)
        self.assertEqual(caught.exception.code, 'model_required')

    def test_unsupported_thought_level_is_rejected(self):
        with self.assertRaises(SuperagentError) as caught:
            self.resolve({'thought_level': 'max'})
        self.assertEqual(caught.exception.code, 'thought_level_unsupported')
        self.assertIn('max', str(caught.exception))

    def test_auto_uses_the_catalogue_default(self):
        resolved = self.resolve({'thought_level': 'auto'})
        self.assertEqual(resolved['thought_level'], 'auto')
        self.assertEqual(resolved['effective_thought_level'], 'low')

    def test_workspace_must_exist(self):
        with self.assertRaises(SuperagentError) as caught:
            self.resolve({'workspace': str(Path(self.tmp.name) / 'missing')})
        self.assertEqual(caught.exception.code, 'workspace_required')
        with self.assertRaises(SuperagentError) as caught:
            self.resolve({'workspace': ''}, defaults={**self.defaults, 'default_workspace': ''})
        self.assertEqual(caught.exception.code, 'workspace_required')

    def test_installation_directory_is_not_a_workspace(self):
        inside = self.plugin_root / 'zcode_bridge' / 'work'
        inside.mkdir(parents=True)
        with self.assertRaises(SuperagentError) as caught:
            resolve_profile(explicit={'workspace': str(inside)}, defaults=self.defaults,
                            profiles={}, catalog=CATALOG, install_roots=[self.plugin_root])
        self.assertEqual(caught.exception.code, 'workspace_required')
        self.assertIn('安装目录', str(caught.exception))


class ConfigurationTests(SuperagentFixture):
    def test_profiles_are_validated_and_roundtrip(self):
        workspace = str(self.default_workspace)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {'ZCODE_BRIDGE_STATE': temporary, 'ZCODE_WORKSPACE': workspace}):
            saved = storage.save_config({
                'superagent_profiles': {'fast': {'model': dict(BETA), 'thought_level': 'max'}},
                'superagent_default_profile': 'fast',
            })
            self.assertEqual(saved['superagent_default_profile'], 'fast')
            self.assertEqual(storage.config()['superagent_profiles']['fast']['model'], BETA)
            with self.assertRaises(ValueError):
                storage.save_config({'superagent_profiles': {'bad': {'model': {'providerId': 'x'}}}})
            with self.assertRaises(ValueError):
                storage.save_config({'superagent_default_profile': 'missing'})
            with self.assertRaises(ValueError):
                storage.save_config({'superagent_profiles': {'bad': {'extra': True}}})

    def test_defaults_publish_no_paths_or_model_ids(self):
        published = storage.defaults()
        self.assertEqual(published['superagent_profiles'], {})
        self.assertEqual(published['superagent_default_profile'], '')
        self.assertFalse(published['desktop_sync_enabled'])
        self.assertEqual(published['desktop_sync_control_file'], '')
        self.assertEqual(published['desktop_sync_min_interval_seconds'], 5)
        self.assertEqual(published['superagent_stall_seconds'], 600)
        self.assertEqual(published['superagent_read_failure_threshold'], 5)

    def test_desktop_sync_and_diagnostics_settings_validate(self):
        workspace = str(self.default_workspace)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {'ZCODE_BRIDGE_STATE': temporary, 'ZCODE_WORKSPACE': workspace}):
            saved = storage.save_config({
                'desktop_sync_enabled': True,
                'desktop_sync_control_file': str(Path(temporary) / 'control.json'),
                'desktop_sync_min_interval_seconds': 7,
                'superagent_stall_seconds': 120,
                'superagent_read_failure_threshold': 3,
            })
            self.assertTrue(saved['desktop_sync_enabled'])
            self.assertEqual(saved['desktop_sync_min_interval_seconds'], 7)
            self.assertEqual(saved['superagent_stall_seconds'], 120)
            for changes in ({'desktop_sync_enabled': 'true'},
                            {'desktop_sync_control_file': False},
                            {'desktop_sync_min_interval_seconds': 0},
                            {'superagent_stall_seconds': 10},
                            {'superagent_read_failure_threshold': 1}):
                with self.assertRaises(ValueError):
                    storage.save_config(changes)


class JobStoreTests(SuperagentFixture):
    def test_idempotency_returns_the_same_job_and_never_duplicates(self):
        first = self.create(key='same-key')
        second = self.create(key='same-key')
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(first['job']['job_id'], second['job']['job_id'])
        self.assertEqual(first['job']['agent_id'], second['job']['agent_id'])
        self.assertFalse(second['conflict'])
        self.assertEqual(len(self.store.list('thr_a')), 1)

    def test_same_key_from_another_thread_is_a_different_job(self):
        first = self.create(thread='thr_a', key='shared')
        second = self.create(thread='thr_b', key='shared')
        self.assertTrue(second['created'])
        self.assertNotEqual(first['job']['job_id'], second['job']['job_id'])

    def test_conflicting_replay_reuses_the_job_and_flags_conflict(self):
        first = self.create()
        different = self.resolve({'model': dict(BETA), 'thought_level': 'max'})
        replay = self.create(key='idem-1', resolved=different)
        self.assertFalse(replay['created'])
        self.assertTrue(replay['conflict'])
        self.assertEqual(replay['job']['job_id'], first['job']['job_id'])

    def test_generated_key_is_persisted_and_returned(self):
        created = self.create(key=None)
        self.assertTrue(created['idempotency_key'].startswith('idem_'))
        stored = self.store.get(created['job']['job_id'], 'thr_a')
        self.assertEqual(stored['idempotency_key'], created['idempotency_key'])
        replay = self.create(key=created['idempotency_key'])
        self.assertFalse(replay['created'])
        blank = self.create(thread='thr_blank', key='')
        self.assertTrue(blank['idempotency_key'].startswith('idem_'))
        self.assertTrue(blank['created'])

    def test_permission_and_source_origin_are_recorded(self):
        with self.assertRaises(SuperagentError) as caught:
            self.store.create_job(self.source(), self.resolve(),
                                  caller_permission_mode=None, task_text='x')
        self.assertEqual(caught.exception.code, 'caller_permission_required')
        with self.assertRaises(SuperagentError) as caught:
            self.store.create_job(self.source(), self.resolve(),
                                  caller_permission_mode='yolo', task_text='x')
        self.assertEqual(caught.exception.code, 'caller_permission_required')

        job = self.create()['job']
        self.assertEqual(job['permission']['declared'], 'always_ask')
        self.assertEqual(job['permission']['source'], 'explicit_caller')
        self.assertEqual(job['source']['thread_origin'], 'turn_metadata')
        for origin in ('openai_thread_id', 'env_fallback'):
            row = self.create(thread='thr_' + origin, key='key-' + origin, origin=origin)['job']
            self.assertEqual(row['source']['thread_origin'], origin)

    def test_unvalidated_config_cannot_create_a_job(self):
        resolved = self.resolve()
        resolved.pop('catalog_validated')
        with self.assertRaises(SuperagentError) as caught:
            self.store.create_job(self.source(), resolved,
                                  caller_permission_mode='always_ask', task_text='x')
        self.assertEqual(caught.exception.code, 'catalog_unavailable')

    def test_reads_and_mutations_are_thread_scoped(self):
        job = self.create()['job']
        self.assertIsNone(self.store.get(job['job_id'], 'thr_other'))
        self.assertEqual(self.store.list('thr_other'), [])
        self.assertIsNone(self.store.set_status(job['job_id'], 'thr_other', 'running'))
        self.assertIsNone(self.store.record_progress(job['job_id'], 'thr_other', kind='x'))

    def test_restart_reads_persisted_state(self):
        created = self.create()
        job_id = created['job']['job_id']
        self.store.set_status(job_id, 'thr_a', 'running', 'dispatched')
        self.assertEqual(self.store.record_read_failure(job_id, 'thr_a', 'TimeoutError'), 1)
        self.assertEqual(self.store.record_read_failure(job_id, 'thr_a', 'TimeoutError'), 2)
        queued = self.store.enqueue_message(job_id, 'thr_a', 'follow up', 'msg-key')

        reopened = SuperagentStore(self.store.path)
        job = reopened.get(job_id, 'thr_a')
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['read_failures']['consecutive'], 2)
        self.assertEqual(job['queue'][0]['message_id'], queued['message_id'])
        self.assertEqual(reopened.take_next_message(job_id, 'thr_a')['content'], 'follow up')

    def test_unknown_status_and_fields_are_rejected(self):
        job = self.create()['job']
        with self.assertRaises(SuperagentError) as caught:
            self.store.set_status(job['job_id'], 'thr_a', 'invented')
        self.assertEqual(caught.exception.code, 'status_invalid')
        with self.assertRaises(SuperagentError) as caught:
            self.store.update_job(job['job_id'], 'thr_a', agent_id='changed')
        self.assertEqual(caught.exception.code, 'field_not_mutable')


class QueueTests(SuperagentFixture):
    def test_queue_is_bounded_and_idempotent(self):
        job_id = self.create()['job']['job_id']
        first = self.store.enqueue_message(job_id, 'thr_a', 'one', 'k-1')
        self.assertEqual(first['state'], 'queued')
        duplicate = self.store.enqueue_message(job_id, 'thr_a', 'changed text', 'k-1')
        self.assertEqual(duplicate['state'], 'duplicate')
        self.assertEqual(duplicate['message_id'], first['message_id'])
        self.assertEqual(self.store.queue_size(job_id, 'thr_a'), 1)

        for index in range(1, MAX_QUEUE_ITEMS):
            self.store.enqueue_message(job_id, 'thr_a', 'item ' + str(index))
        self.assertEqual(self.store.queue_size(job_id, 'thr_a'), MAX_QUEUE_ITEMS)
        with self.assertRaises(SuperagentError) as caught:
            self.store.enqueue_message(job_id, 'thr_a', 'overflow')
        self.assertEqual(caught.exception.code, 'queue_full')

        item = self.store.take_next_message(job_id, 'thr_a')
        self.assertEqual(item['content'], 'one')
        self.assertEqual(self.store.queue_size(job_id, 'thr_a'), MAX_QUEUE_ITEMS - 1)

    def test_message_body_is_bounded(self):
        job_id = self.create()['job']['job_id']
        with self.assertRaises(SuperagentError) as caught:
            self.store.enqueue_message(job_id, 'thr_a', 'x' * (MAX_MESSAGE_CHARS + 1))
        self.assertEqual(caught.exception.code, 'message_too_large')

    def test_delivered_key_is_not_requeued(self):
        job_id = self.create()['job']['job_id']
        self.store.enqueue_message(job_id, 'thr_a', 'one', 'once')
        item = self.store.take_next_message(job_id, 'thr_a')
        self.store.mark_message_delivered(job_id, 'thr_a', item['message_id'],
                                          item['idempotency_key_hash'])
        replay = self.store.enqueue_message(job_id, 'thr_a', 'one again', 'once')
        self.assertEqual(replay['state'], 'duplicate')
        self.assertTrue(replay['delivered'])
        self.assertEqual(self.store.queue_size(job_id, 'thr_a'), 0)

    def test_summary_does_not_expose_message_text(self):
        job = self.create()['job']
        secret = 'PRIVATE-FOLLOW-UP-BODY'
        self.store.enqueue_message(job['job_id'], 'thr_a', secret)
        summary = self.store.job_summary(self.store.get(job['job_id'], 'thr_a'))
        rendered = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertEqual(summary['queue']['count'], 1)
        self.assertNotIn('content', rendered)


class QueueClaimTests(SuperagentFixture):
    """The send claim is durable before any side effect, and never auto-resumed."""

    def test_claim_persists_and_restart_marks_uncertain_without_resend(self):
        job_id = self.create()['job']['job_id']
        queued = self.store.enqueue_message(job_id, 'thr_a', 'send me once', 'claim-1')
        claimed = self.store.claim_next_message(job_id, 'thr_a')
        self.assertEqual(claimed['message_id'], queued['message_id'])
        self.assertEqual(claimed['state'], 'in_flight')
        self.assertTrue(claimed['claim_id'].startswith('claim_'))
        self.assertEqual(claimed['attempts'], 1)
        # A second claim in the same process finds nothing to take.
        self.assertIsNone(self.store.claim_next_message(job_id, 'thr_a'))

        # Simulate a crash after the claim but before the outcome was saved.
        reopened = SuperagentStore(self.store.path)
        job = reopened.get(job_id, 'thr_a')
        self.assertEqual(len(job['queue']), 1)
        item = job['queue'][0]
        self.assertEqual(item['state'], 'uncertain')
        self.assertEqual(item['last_error_type'], 'restart_in_flight')
        self.assertNotIn(item['state'], ('queued', 'in_flight'))
        self.assertIsNone(reopened.claim_next_message(job_id, 'thr_a'))
        self.assertIsNone(reopened.peek_next_message(job_id, 'thr_a'))
        self.assertEqual([row['kind'] for row in job['events']],
                         ['job.message_uncertain'])

    def test_only_one_concurrent_claim_of_the_same_message_succeeds(self):
        job_id = self.create()['job']['job_id']
        queued = self.store.enqueue_message(job_id, 'thr_a', 'single winner')
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(self.store.claim_message(
                job_id, 'thr_a', queued['message_id']))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [row for row in results if row is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]['state'], 'in_flight')
        item = self.store.get(job_id, 'thr_a')['queue'][0]
        self.assertEqual(item['state'], 'in_flight')
        self.assertEqual(item['attempts'], 1)

    def test_two_queued_items_are_claimed_in_order_without_double_claim(self):
        job_id = self.create()['job']['job_id']
        first = self.store.enqueue_message(job_id, 'thr_a', 'one')
        second = self.store.enqueue_message(job_id, 'thr_a', 'two')
        claimed = self.store.claim_next_message(job_id, 'thr_a')
        self.assertEqual(claimed['message_id'], first['message_id'])
        self.assertIsNone(self.store.claim_message(job_id, 'thr_a',
                                                   first['message_id']))
        following = self.store.claim_next_message(job_id, 'thr_a')
        self.assertEqual(following['message_id'], second['message_id'])

    def test_claimed_message_completes_after_acceptance(self):
        job_id = self.create()['job']['job_id']
        queued = self.store.enqueue_message(job_id, 'thr_a', 'accepted', 'ok-key')
        claimed = self.store.claim_next_message(job_id, 'thr_a')
        self.assertEqual(claimed['message_id'], queued['message_id'])
        self.store.complete_message(job_id, 'thr_a', claimed['message_id'])
        job = self.store.get(job_id, 'thr_a')
        self.assertEqual(job['queue'], [])
        self.assertEqual(len(job['delivered_messages']), 1)
        self.assertIsNone(self.store.claim_next_message(job_id, 'thr_a'))
        replay = self.store.enqueue_message(job_id, 'thr_a', 'accepted again', 'ok-key')
        self.assertEqual(replay['state'], 'duplicate')
        self.assertTrue(replay['delivered'])

    def test_definite_rejection_is_kept_and_not_retried_as_queued(self):
        job_id = self.create()['job']['job_id']
        queued = self.store.enqueue_message(job_id, 'thr_a', 'rejected')
        claimed = self.store.claim_next_message(job_id, 'thr_a')
        self.store.mark_message_state(job_id, 'thr_a', claimed['message_id'],
                                      'not_accepted', 'send_not_accepted')
        job = self.store.get(job_id, 'thr_a')
        self.assertEqual(job['queue'][0]['state'], 'not_accepted')
        self.assertEqual(job['queue'][0]['last_error_type'], 'send_not_accepted')
        self.assertIsNone(self.store.claim_next_message(job_id, 'thr_a'))
        summary = self.store.job_summary(job)
        self.assertEqual(summary['queue']['positions'][0]['state'], 'not_accepted')
        self.assertEqual(summary['queue']['positions'][0]['message_id'],
                         queued['message_id'])

    def test_queued_item_is_not_recovered_as_uncertain(self):
        job_id = self.create()['job']['job_id']
        self.store.enqueue_message(job_id, 'thr_a', 'still queued')
        reopened = SuperagentStore(self.store.path)
        self.assertEqual(reopened.get(job_id, 'thr_a')['queue'][0]['state'], 'queued')
        self.assertIsNotNone(reopened.claim_next_message(job_id, 'thr_a'))


class SendEvidenceTests(SuperagentFixture):
    """Admission is persisted from real send results, never from reports."""

    def test_report_hint_cannot_upgrade_uncertain_or_failed_send(self):
        job_id = self.create()['job']['job_id']
        self.store.set_status(job_id, 'thr_a', 'send_uncertain', 'send_uncertain')
        self.store.set_send_state(job_id, 'thr_a', 'uncertain', 'send_uncertain',
                                  'TimeoutError')
        self.store.record_report(job_id, {'state': 'unverified_hint',
                                          'reported_status': 'completed'})
        job = self.store.get(job_id, 'thr_a')
        self.assertEqual(job['send']['state'], 'uncertain')
        self.assertTrue(job['send']['retry_required'])
        self.assertFalse(job['send']['accepted'])
        self.assertEqual(job['status'], 'send_uncertain')
        self.assertNotIn('job.reported', [row['kind'] for row in job['events']])

        failed_id = self.create(key='idem-failed')['job']['job_id']
        self.store.set_status(failed_id, 'thr_a', 'failed', 'send_not_accepted')
        self.store.set_send_state(failed_id, 'thr_a', 'failed', 'send_not_accepted')
        self.store.record_report(failed_id, {'state': 'unverified_hint'})
        failed_job = self.store.get(failed_id, 'thr_a')
        self.assertEqual(failed_job['send']['state'], 'failed')
        self.assertEqual(failed_job['status'], 'failed')

    def test_accepted_send_evidence_is_never_downgraded(self):
        job_id = self.create()['job']['job_id']
        self.store.set_status(job_id, 'thr_a', 'running', 'send_accepted')
        self.store.set_send_state(job_id, 'thr_a', 'accepted', 'send_accepted')
        self.store.set_send_state(job_id, 'thr_a', 'uncertain', 'later_message')
        job = self.store.get(job_id, 'thr_a')
        self.assertEqual(job['send']['state'], 'accepted')
        self.assertTrue(job['send']['accepted'])
        self.assertFalse(job['send']['retry_required'])
        self.store.record_report(job_id, {'state': 'unverified_hint'})
        self.assertEqual(self.store.get(job_id, 'thr_a')['send']['state'], 'accepted')
        self.store.set_status(job_id, 'thr_a', 'reported', 'unverified_hint')
        self.assertEqual(self.store.job_summary(
            self.store.get(job_id, 'thr_a'))['send']['state'], 'accepted')



class ContractTests(SuperagentFixture):
    def test_first_prompt_is_full_and_later_prompt_is_short(self):
        task = '修复 backend 的失败测试，只改这个工作区。'
        job = self.create(task=task)['job']
        first = build_task_prompt(task, job)
        self.assertTrue(first.startswith(task))
        self.assertIn(CONTRACT_VERSION, first)
        self.assertIn(job['agent_id'], first)
        self.assertIn(job['job_id'], first)
        self.assertIn('任务范围与完成条件', first)
        self.assertIn('如何报告与提问', first)
        for field in RESULT_FIELDS:
            self.assertIn(field, first)

        later = build_task_prompt('补充：先跑单元测试。', job, later=True)
        self.assertTrue(later.startswith('补充：先跑单元测试。'))
        self.assertIn(CONTRACT_VERSION, later)
        self.assertNotIn('结构化最终结果字段', later)

    def test_revision_attaches_the_full_contract_again(self):
        task = '实现 X'
        job = self.create(task=task)['job']
        revised = build_task_prompt(task, job, revision=True)
        self.assertIn(CONTRACT_VERSION, revised)
        self.assertIn('修订', revised)
        self.assertIn('结构化最终结果字段', revised)

    def test_workflow_task_keeps_its_first_line(self):
        task = '/workflow 修复 x\n阶段一：分析\n阶段二：实现'
        job = self.create(task=task)['job']
        prompt = build_task_prompt(task, job)
        self.assertTrue(prompt.startswith(task))
        self.assertEqual(prompt.splitlines()[0], '/workflow 修复 x')
        self.assertIn(CONTRACT_VERSION, prompt)

    def test_task_text_is_required(self):
        job = self.create()['job']
        with self.assertRaises(SuperagentError) as caught:
            build_task_prompt('', job)
        self.assertEqual(caught.exception.code, 'task_required')

    def test_contract_relays_needs_input_and_allows_agent_only_report(self):
        job = self.create()['job']
        prompt = build_task_prompt('实现 X', job)
        self.assertIn('needs_input', prompt)
        self.assertIn('AskUserQuestion', prompt)
        self.assertIn('ZCode 主会话', prompt)
        self.assertIn('不要自行代答', prompt)
        self.assertIn('session_id 可省略', prompt)
        self.assertNotIn('There is no tool', prompt)

    def test_configured_workspace_is_required_without_saved_default(self):
        with self.assertRaises(SuperagentError) as caught:
            resolve_profile(profiles=self.profiles, defaults=self.defaults,
                            catalog=CATALOG, configured_workspace='')
        self.assertEqual(caught.exception.code, 'workspace_required')
        resolved = resolve_profile(profiles=self.profiles, defaults=self.defaults,
                                   catalog=CATALOG,
                                   configured_workspace=str(self.other_workspace))
        self.assertEqual(resolved['workspace'], str(self.other_workspace.resolve()))
        self.assertEqual(resolved['sources']['workspace'], 'configured_default')

    def test_source_tree_plugins_are_installation_roots(self):
        repo = Path(__file__).resolve().parents[1]
        roots = _installation_roots()
        self.assertTrue(any(str(repo / 'plugins') in str(root) for root in roots))
        for candidate in (repo / 'plugins', repo / 'plugins' / 'codex',
                          repo / 'plugins' / 'zcode', repo / 'runtime'):
            with self.assertRaises(SuperagentError) as caught:
                resolve_workspace(explicit={'workspace': str(candidate)},
                                  install_roots=roots)
            self.assertEqual(caught.exception.code, 'workspace_required')

    def test_report_payload_is_bounded_and_structured(self):
        payload, truncated = bounded_report_payload({
            '摘要': 'x' * 100000,
            'rows': [{'n': index} for index in range(500)],
            'deep': {'a': {'b': {'c': {'d': {'e': 1}}}}},
        })
        self.assertTrue(truncated)
        self.assertLessEqual(
            len(json.dumps(payload, ensure_ascii=False).encode('utf-8')),
            MAX_REPORT_BYTES)
        self.assertIsInstance(payload, dict)
        self.assertIn('摘要', payload)


if __name__ == '__main__':
    unittest.main()
