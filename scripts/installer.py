"""Install this portable plugin using paths resolved on the current machine."""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MARKET = 'codex-to-zcode-bridge-plugins'
PLUGIN = 'codex-to-zcode-bridge'


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def command(args):
    result = subprocess.run([str(a) for a in args],capture_output=True,text=True,encoding='utf-8',
        errors='replace',timeout=120,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode:
        raise RuntimeError('Installation command failed: '+str(args[0])+'\n'+(result.stderr or result.stdout)[-2000:])
    return json.loads(result.stdout)


def default_root():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'CodexToZCodeBridge/marketplace'


def main(source=None):
    source = Path(source).resolve() if source else Path(__file__).resolve().parent
    packaged = source/'_host.json'
    package_host = json.loads(packaged.read_text(encoding='utf-8'))['host'] if packaged.is_file() else None
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',choices=['codex','zcode','both','mcp'],default=package_host or 'both')
    parser.add_argument('--install-root',type=Path,default=default_root())
    parser.add_argument('--zcode-executable')
    parser.add_argument('--engine-entry')
    parser.add_argument('--node-executable')
    parser.add_argument('--workspace')
    parser.add_argument('--model-provider')
    parser.add_argument('--model-id')
    parser.add_argument('--codex',default=shutil.which('codex') or '')
    parser.add_argument('--no-register',action='store_true',help='Generate the local MCP and marketplace files without editing host registration')
    parser.add_argument('--dry-run',action='store_true',help='Print the plan without writing or launching anything')
    args = parser.parse_args()
    hosts = ['codex','zcode'] if args.target=='both' else [package_host or 'codex'] if args.target=='mcp' else [args.target]
    if package_host and any(h!=package_host for h in hosts):
        parser.error('This archive contains only the '+package_host+' plugin; use the matching archive or full repository')
    root = args.install_root.expanduser().resolve()
    if root==source or source.is_relative_to(root) or root.is_relative_to(source):
        parser.error('Choose an installation directory outside the source checkout')
    sources = {host:source if package_host else source/'plugins'/host for host in hosts}
    for host, folder in sources.items():
        if not (folder/'bridge.py').is_file():
            parser.error('Missing built '+host+' package; run python build.py first')
    sys.path.insert(0,str(next(iter(sources.values()))))
    from zcode_bridge.storage import config, save_config
    cfg = config() if not args.dry_run else {}
    changes = {k:getattr(args,k) for k in ('zcode_executable','engine_entry','node_executable') if getattr(args,k)}
    if args.workspace:
        changes['default_workspace'] = str(Path(args.workspace).expanduser().resolve())
    if args.model_provider or args.model_id:
        if not (args.model_provider and args.model_id):
            parser.error('--model-provider and --model-id must be supplied together')
        changes['default_model'] = {'providerId':args.model_provider,'modelId':args.model_id}
    if args.zcode_executable:
        resources = Path(args.zcode_executable).resolve().parent/'resources'
        changes.setdefault('engine_entry',str(resources/'glm/zcode.cjs'))
        if (resources/'config/provider/zcode-builtin.json').is_file():
            changes['builtin_provider_file'] = str(resources/'config/provider/zcode-builtin.json')
    plan = {'target':args.target,'install_root':str(root),'hosts':hosts,
        'registration':not args.no_register and args.target!='mcp','python':sys.executable,
        'model_selection':'explicit local configuration required; no bundled model IDs'}
    if args.dry_run:
        print(json.dumps(plan,ensure_ascii=False,indent=2))
        return
    backup = root/'backups'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for host, folder in sources.items():
        destination = root/'plugins'/host
        identity = destination/'_host.json'
        if destination.exists() and not identity.is_file():
            raise ValueError('Installation target is not owned by this installer: '+str(destination))
        if identity.is_file():
            shutil.copytree(destination,backup/host,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copytree(folder,destination,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        write(destination/'.mcp.json',{'mcpServers':{'zcode_bridge':{
            'command':sys.executable,'args':[str(destination/'bridge.py'),'mcp']}}})
    if changes:
        cfg = save_config(changes)
    if 'codex' in hosts:
        write(root/'.agents/plugins/marketplace.json',{'name':MARKET,'interface':{'displayName':'Codex to ZCode Bridge'},
            'plugins':[{'name':PLUGIN,'source':{'source':'local','path':'./plugins/codex'},
                'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Developer Tools'}]})
    if 'zcode' in hosts:
        write(root/'.claude-plugin/marketplace.json',{'name':MARKET,'plugins':[{'name':PLUGIN,'source':'./plugins/zcode'}]})
    registrations = {}
    if plan['registration']:
        if 'codex' in hosts:
            if not args.codex:
                raise RuntimeError('Codex CLI was not found. Files are prepared; use --codex or --no-register and the manual instructions.')
            entries = command([args.codex,'plugin','marketplace','list','--json']).get('marketplaces',[])
            existing = next((e for e in entries if e.get('name')==MARKET),None)
            if existing and Path(existing['root']).resolve()!=root:
                raise ValueError('Marketplace name already points elsewhere; existing registration was preserved')
            if not existing:
                command([args.codex,'plugin','marketplace','add',root,'--json'])
            registrations['codex'] = command([args.codex,'plugin','add',PLUGIN+'@'+MARKET,'--json'])
            # Fresh personal catalogues are also discoverable by ChatGPT desktop.
            # Never overwrite another marketplace or a colliding plugin entry.
            personal = Path.home()/'.agents/plugins/marketplace.json'
            if root.is_relative_to(Path.home().resolve()):
                catalog = json.loads(personal.read_text(encoding='utf-8')) if personal.exists() else {'name':MARKET,'interface':{'displayName':'Codex to ZCode Bridge'},'plugins':[]}
                if catalog.get('name')==MARKET:
                    entry={'name':PLUGIN,'source':{'source':'local','path':'./'+(root/'plugins/codex').relative_to(Path.home()).as_posix()},
                        'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Developer Tools'}
                    colliding = next((p for p in catalog.get('plugins',[]) if p.get('name')==PLUGIN),None)
                    if colliding and colliding.get('source')!=entry['source']:
                        raise ValueError('A different personal plugin has the same name; it was preserved')
                    catalog['plugins']=[p for p in catalog.get('plugins',[]) if p.get('name')!=PLUGIN]+[entry]
                    if personal.is_file():
                        backup.mkdir(parents=True,exist_ok=True)
                        shutil.copy2(personal,backup/'personal-marketplace.json')
                    write(personal,catalog)
        if 'zcode' in hosts:
            engine, node = Path(cfg['engine_entry']), cfg['node_executable'] or shutil.which('node')
            if not node or not engine.is_file():
                raise RuntimeError('ZCode engine or Node was not found. Use --zcode-executable / --engine-entry, or --no-register.')
            cli=[node,str(engine)]
            entries=command([*cli,'plugins','marketplace','list','--json'])
            existing=next((e for e in entries if e.get('name')==MARKET),None)
            if existing and existing.get('source',{}).get('path') and Path(existing['source']['path']).resolve()!=root:
                raise ValueError('ZCode marketplace already points elsewhere; existing registration was preserved')
            if not existing:
                command([*cli,'plugins','marketplace','add',root,'--json'])
            else:
                command([*cli,'plugins','marketplace','update',MARKET,'--json'])
            installed=command([*cli,'plugins','list','--json'])
            action='update' if any(p.get('id')==PLUGIN+'@'+MARKET for p in installed) else 'install'
            registrations['zcode']=command([*cli,'plugins',action,PLUGIN+'@'+MARKET,'--json'])
    result={**plan,'registrations':registrations,'mcp':{host:json.loads((root/'plugins'/host/'.mcp.json').read_text(encoding='utf-8')) for host in hosts}}
    write(root/'installation.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
