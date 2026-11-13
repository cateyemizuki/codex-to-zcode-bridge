"""Entrypoint shared by MCP plugin hosts and direct local automation."""
import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['mcp', 'daemon', 'call', 'settings'])
    parser.add_argument('tool', nargs='?')
    parser.add_argument('--arguments', default='{}')
    parser.add_argument('--arguments-file')
    args = parser.parse_args()
    if args.command == 'mcp':
        from zcode_bridge.mcp import serve
        return serve()
    if args.command == 'daemon':
        from zcode_bridge.transport import serve
        return serve()
    from zcode_bridge.transport import local_call
    if args.command == 'settings':
        result = local_call('zcode_open_settings')
        print('Codex to ZCode Bridge settings opened' if result.get('opened') else 'Open failed; use zcode_open_settings with open_browser=false')
        return
    if not args.tool:
        parser.error('call requires a tool name')
    raw = Path(args.arguments_file).read_text(encoding='utf-8-sig') if args.arguments_file else args.arguments
    result = local_call(args.tool, json.loads(raw))
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


if __name__ == '__main__':
    main()
