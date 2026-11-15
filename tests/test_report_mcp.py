import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from zcode_bridge import report_mcp


class ReportMcpTests(unittest.TestCase):
    def test_tools_list_exposes_only_the_report_tool(self):
        tools = report_mcp.handle({'method': 'tools/list'})['tools']
        self.assertEqual([tool['name'] for tool in tools], ['zcode_agent_report'])
        rendered = str(tools).lower()
        for forbidden in ('zcode_delegate', 'zcode_agent_message', 'zcode_agent_cancel',
                          'zcode_agent_inspect', 'zcode_send'):
            self.assertNotIn(forbidden, rendered)
        schema = tools[0]['inputSchema']
        self.assertEqual(schema['required'], ['agent_id', 'result'])

    def test_initialize_is_minimal(self):
        result = report_mcp.handle({'method': 'initialize', 'params': {}})
        self.assertEqual(result['serverInfo']['name'], 'codex-to-zcode-bridge-report')
        self.assertEqual(result['capabilities'], {'tools': {}})

    def test_call_forwards_only_report_arguments(self):
        with patch.object(report_mcp, 'local_call',
                          return_value={'recorded': True, 'accepted_as': 'unverified_hint'}) as call:
            result = report_mcp.handle({'method': 'tools/call', 'params': {
                'name': 'zcode_agent_report',
                'arguments': {'agent_id': 'agent_1', 'result': {'摘要': 'done'}},
            }})
        call.assert_called_once_with('zcode_agent_report',
                                     {'agent_id': 'agent_1', 'result': {'摘要': 'done'}})
        self.assertTrue(result['structuredContent']['recorded'])

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            report_mcp.handle({'method': 'tools/call', 'params': {
                'name': 'zcode_delegate', 'arguments': {}}})
        self.assertIn('unknown tool', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
