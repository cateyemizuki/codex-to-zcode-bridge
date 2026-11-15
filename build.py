"""Build the two self-contained plugins from one canonical runtime."""
import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = '0.5.1'
MARKET = 'codex-to-zcode-bridge-plugins'
REPOSITORY = 'https://github.com/cateyemizuki/codex-to-zcode-bridge'


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')


def build(destination):
    for host in ('codex', 'zcode'):
        target = destination/'plugins'/host
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT/'runtime/zcode_bridge', target/'zcode_bridge', dirs_exist_ok=True,
            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copy2(ROOT/'runtime/bridge.py', target/'bridge.py')
        shutil.copy2(ROOT/'scripts/installer.py', target/'install.py')
        shutil.copy2(ROOT/'LICENSE', target/'LICENSE')
        for name in ('README.md','AGENTS.md'):
            shutil.copy2(ROOT/'docs'/host/name, target/name)
        write(target/'_host.json', {'host':host, 'version':VERSION})
        identity = {'name':'codex-to-zcode-bridge','version':VERSION,
            'description':'Local ZCode sessions, models, questions, permissions and dynamic workflows.',
            'author':{'name':'cateyemizuki'}, 'repository':REPOSITORY, 'license':'MIT'}
        # Supported compatibility manifests are deliberate: some clients still
        # fail to load local MCP components when a root portable manifest wins.
        if host == 'codex':
            write(target/'.codex-plugin/plugin.json', {**identity, 'mcpServers':'./.mcp.json',
                'interface':{'displayName':'Codex to ZCode Bridge','shortDescription':'本机 ZCode 会话、模型与动态工作流',
                    'longDescription':'纯 MCP 桌面插件，提供原生设置和内嵌配置面板，不依赖 GPT 侧 skill。',
                    'developerName':'cateyemizuki','category':'Developer Tools',
                    'capabilities':['Read','Write','Interactive'],'brandColor':'#72E2B5'}})
        else:
            write(target/'.zcode-plugin/plugin.json', {**identity, 'mcpServers':'./.mcp.json',
                'skills':'./skills', 'commands':'./commands'})
            shutil.copytree(ROOT/'docs/zcode/skills', target/'skills', dirs_exist_ok=True)
            shutil.copytree(ROOT/'docs/zcode/commands', target/'commands', dirs_exist_ok=True)
        bootstrap = ('import runpy,sys;from pathlib import Path;'
            'p=Path(sys.argv[1])/"bridge.py";sys.path.insert(0,str(p.parent));'
            'sys.argv=[str(p),*sys.argv[2:]];runpy.run_path(str(p),run_name="__main__")')
        arguments = ['-m','zcode_bridge','mcp'] if host=='codex' else ['-c',bootstrap,'${CLAUDE_PLUGIN_ROOT}','mcp']
        write(target/'.mcp.json', {'mcpServers':{'zcode_bridge':{'command':'python','args':arguments}}})
        # ZCode resolves its plugin-root variable. The current Codex MCP loader
        # does not, so direct marketplace installs first install this Python
        # distribution; the normal installer pins a local script for either host.
        (target/'pyproject.toml').write_text(
            '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
            '[project]\nname = "codex-to-zcode-bridge"\nversion = "'+VERSION+'"\nrequires-python = ">=3.10"\n'
            'description = "Local ZCode MCP bridge"\nlicense = {file = "LICENSE"}\n\n'
            '[tool.setuptools.packages.find]\nwhere = ["."]\ninclude = ["zcode_bridge*"]\n\n'
            '[tool.setuptools.package-data]\nzcode_bridge = ["ui/*.html"]\n', encoding='utf-8', newline='\n')
    write(destination/'.agents/plugins/marketplace.json', {'name':MARKET,
        'interface':{'displayName':'Codex to ZCode Bridge'},'plugins':[{'name':'codex-to-zcode-bridge',
            'source':{'source':'local','path':'./plugins/codex'},
            'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Developer Tools'}]})
    write(destination/'.claude-plugin/marketplace.json', {'name':MARKET,'plugins':[{
        'name':'codex-to-zcode-bridge','version':VERSION,'source':'./plugins/zcode',
        'description':'Codex to ZCode Bridge administration and local MCP tools.'}]})


def inventory(root):
    return {str(p.relative_to(root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--archives', action='store_true')
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            build(stage)
            expected = inventory(stage)
            actual = {}
            for prefix in ('plugins','.agents','.claude-plugin'):
                actual.update({prefix+'/'+k:v for k,v in inventory(ROOT/prefix).items()})
            if actual != expected:
                names = sorted(k for k in set(actual)|set(expected) if actual.get(k)!=expected.get(k))
                raise SystemExit('Generated packages differ: '+', '.join(names))
        print('Both generated plugins match the canonical sources')
        return
    build(ROOT)
    if args.archives:
        dist = ROOT/'dist'
        dist.mkdir(exist_ok=True)
        for host in ('codex','zcode'):
            with zipfile.ZipFile(dist/f'codex-to-zcode-bridge-{host}-{VERSION}.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
                for path in (ROOT/'plugins'/host).rglob('*'):
                    if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                        archive.write(path, path.relative_to(ROOT/'plugins'/host))
        checksums = [hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name for p in sorted(dist.glob('*.zip'))]
        (dist/'SHA256SUMS').write_text('\n'.join(checksums)+'\n',encoding='utf-8',newline='\n')
    print('Built Codex/ChatGPT and ZCode plugins', VERSION)


if __name__ == '__main__':
    main()
