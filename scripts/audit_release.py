"""Reject local state, private locations and secret-shaped data before publishing."""
import argparse
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {'connection.json','sessions.json','installation.json','verification.json',
    'provider_config.json','config.json','.env','credentials.json','auth.json',
    'notifications.json','subscriptions.json','inbox.json','outbox.json','host_capability.json'}
PATTERNS = {
    'absolute Windows path': re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:[^\s\"\'<>]+)'),
    'private provider ID': re.compile(r'new-provider-\d+'),
    'GitHub token': re.compile(r'\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    'API key': re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b'),
    'private session ID': re.compile(r'\bsess_[a-f0-9]{8}-[a-f0-9-]{27,}\b'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}


def inspect(name, data):
    p = Path(name)
    errors = []
    if (p.name in FORBIDDEN_NAMES
            or (p.name.startswith('notifications.') and p.suffix == '.json')
            or p.suffix in ('.pyc','.log','.sqlite','.db','.bak') or '__pycache__' in p.parts):
        errors.append(name+': local state or backup file')
    if p.suffix in ('.md','.py','.json','.toml','.yml','.yaml','.html','.txt') or p.name in ('LICENSE','.gitignore'):
        text = data.decode('utf-8')
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                # Do not echo a potential credential in the audit failure.
                errors.append(name+': '+label)
    return errors


def audit(root=ROOT, archives=True):
    errors, count = [], 0
    for p in root.rglob('*'):
        relative = p.relative_to(root)
        # `.zcode` is the local client's own state directory, not release input.
        if not p.is_file() or any(x in relative.parts for x in ('.git','__pycache__','.venv','dist','build','.zcode')) or p.suffix=='.pyc':
            continue
        count += 1
        errors += inspect(relative.as_posix(), p.read_bytes())
    if archives:
        for p in (root/'dist').glob('*.zip'):
            with zipfile.ZipFile(p) as archive:
                for entry in archive.infolist():
                    if entry.is_dir():
                        continue
                    parts = Path(entry.filename).parts
                    if entry.filename.startswith(('/','\\')) or '..' in parts:
                        errors.append(p.name+': unsafe archive path')
                    count += 1
                    errors += inspect(p.name+'/'+entry.filename, archive.read(entry))
    return errors, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    args = parser.parse_args()
    errors, count = audit(args.root.resolve())
    print(json.dumps({'ok':not errors,'files_checked':count,'errors':errors},ensure_ascii=False,indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
