"""Keep native routing evidence separate from actual GUI verification."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
from zcode_bridge.service import Bridge


class NativeSummaryTests(unittest.TestCase):
    def summary(self, transport, snapshot):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {'ZCODE_BRIDGE_STATE':str(Path(temporary)/'state')}):
            bridge = Bridge()
            bridge.sessions['sess_summary'] = {
                'workspace':temporary, 'transport':transport,
                **({'native_source':'desktop-native-host'} if transport == 'desktop_native' else {})}
            visibility = {'indexed':False, 'workspace_registered':True,
                          'live_updates':False, 'connection':'independent_engine',
                          'note':'independent engine has no desktop live events'}
            with patch('zcode_bridge.service.session_visibility', return_value=visibility):
                return bridge.summary(snapshot)

    def test_native_summary_preserves_facts_without_legacy_engine_or_gui_claims(self):
        snapshot = {
            'session':{'sessionId':'sess_summary'}, 'projection':{}, 'runtime':{},
            'settings':{'model':{'current':{}, 'available':[{'private':'catalogue'}]}},
            'ownedSID':'sess_summary',
            'desktopIndexFacts':{'sessions':[{'sessionId':'sess_summary', 'exists':False}]},
            'desktopListEvent':{'synced':False, 'announced':False},
            'projectAssignment':{'status':'assigned', 'projectId':'project_fixture', 'source':'workspace'},
            'guiVerified':True,
        }
        result = self.summary('desktop_native', snapshot)
        self.assertEqual(result['transport'], 'desktop_native')
        self.assertEqual(result['desktop']['connection'], 'desktop_native')
        self.assertFalse(result['desktop']['indexed'])
        self.assertFalse(result['desktop']['gui_verified'])
        self.assertFalse(result['guiVerified'])
        self.assertNotIn('live_updates', result['desktop'])
        self.assertNotIn('independent_engine', json.dumps(result))
        self.assertEqual(result['desktopIndexFacts'], snapshot['desktopIndexFacts'])
        self.assertEqual(result['desktopListEvent'], snapshot['desktopListEvent'])
        self.assertEqual(result['projectAssignment'], snapshot['projectAssignment'])
        result['desktopIndexFacts']['sessions'][0]['exists'] = True
        self.assertFalse(snapshot['desktopIndexFacts']['sessions'][0]['exists'])
        self.assertNotIn('available', result['settings']['model'])
        self.assertIn('available', snapshot['settings']['model'])

    def test_engine_summary_keeps_legacy_visibility(self):
        result = self.summary('engine', {
            'session':{'sessionId':'sess_summary'}, 'projection':{}, 'runtime':{},
            'settings':{}})
        self.assertEqual(result['desktop']['connection'], 'independent_engine')
        self.assertFalse(result['desktop']['live_updates'])
        self.assertNotIn('guiVerified', result)


if __name__ == '__main__':
    unittest.main()
