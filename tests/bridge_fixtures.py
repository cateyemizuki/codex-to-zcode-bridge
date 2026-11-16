"""In-memory RPC fixtures for retained history and generic job-state tests.

Production no longer opens the old SDK transport. Existing state-machine
regressions still feed synthetic legacy snapshots through a fake client; this
fixture never creates a process. Desktop-only entry-point tests import the real
Bridge directly instead.
"""
import copy
from unittest.mock import patch

from zcode_bridge.service import Bridge as ProductionBridge
from zcode_bridge.storage import config


class MockBridge(ProductionBridge):
    def __init__(self):
        super().__init__()
        self.desktop_native.ensure_desktop = None

    def backend(self, deadline=None):
        if self.client is None:
            raise AssertionError('An in-memory test client must be supplied')
        return self.client

    @staticmethod
    def _select_transport(value=None):
        return value or 'engine'

    def _desktop_models(self, args):
        # Catalog shape fixture only: rpc is replaced by each test's fake.
        from zcode_bridge import service
        cfg = service.config()
        selection = copy.deepcopy(cfg.get('default_model') or {})
        effort = cfg.get('default_thought_level')
        if selection.get('providerId') and selection.get('modelId') and effort and effort != 'auto':
            selection['options'] = {**selection.get('options', {}), 'reasoningLevel': effort}
        workspace = args.get('workspace') or cfg['default_workspace']
        snapshot = self.rpc('session/create', {
            'workspace': {'workspacePath': workspace, 'workspaceKey': workspace},
            'mode': 'plan', 'persistence': 'deferred', 'titleGenerationEnabled': False,
            **({'model': selection} if selection.get('providerId') and selection.get('modelId') else {})})
        self.model_catalog = {(m['ref']['providerId'], m['ref']['modelId']): copy.deepcopy(m)
                              for m in snapshot['settings']['model']['available']}
        self.rpc('session/close', {'sessionId': snapshot['session']['sessionId'], 'expectedPersistence': 'deferred'})
        return snapshot['settings']['model']

    def _delegate(self, args, meta, deadline=None):
        with patch('zcode_bridge.superagent.TRANSPORTS', ('engine', 'desktop_native')):
            return super()._delegate(args, meta, deadline)
