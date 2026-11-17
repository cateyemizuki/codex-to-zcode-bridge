"""ASAR resource reading/extraction only; no application copy or launch commands."""
import json
import shutil
import struct
from pathlib import Path


class CopyError(RuntimeError):
    """Compatibility exception shared with the legacy experimental reader."""


def _asar_header(path: Path):
    handle = path.open('rb')
    try:
        prefix = handle.read(16)
        if len(prefix) != 16:
            raise CopyError('app.asar is truncated')
        size = struct.unpack('<I', prefix[12:16])[0]
        if not 0 < size <= 64 * 1024 * 1024:
            raise CopyError('app.asar header size is invalid')
        try:
            header = json.loads(handle.read(size).decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as error:
            raise CopyError('app.asar header is not valid JSON') from error
        return handle, header, 16 + size + ((4 - size % 4) % 4)
    except Exception:
        handle.close()
        raise


def asar_entry(path: Path, name: str) -> bytes:
    handle, node, base = _asar_header(path)
    try:
        for part in name.split('/'):
            node = node['files'][part]
        if 'files' in node or 'offset' not in node:
            raise CopyError('asar entry is not a file: ' + name)
        handle.seek(base + int(node['offset']))
        return handle.read(int(node['size']))
    finally:
        handle.close()


def extract_asar(path: Path, destination: Path) -> int:
    handle, header, base = _asar_header(path)
    unpacked = path.with_name(path.name + '.unpacked')
    count = 0

    def walk(node, parts):
        nonlocal count
        for name, child in node.get('files', {}).items():
            if not isinstance(child, dict):
                raise CopyError('unsupported asar entry shape')
            entry = parts + [name]
            if 'files' in child:
                walk(child, entry)
                continue
            target = (destination / Path(*entry)).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise CopyError('asar entry escapes the destination')
            target.parent.mkdir(parents=True, exist_ok=True)
            if child.get('unpacked'):
                source = unpacked.joinpath(*entry)
                if not source.is_file():
                    raise CopyError('unpacked asar entry is missing: ' + str(source))
                shutil.copy2(source, target)
            else:
                handle.seek(base + int(child['offset']))
                target.write_bytes(handle.read(int(child['size'])))
            count += 1

    try:
        walk(header, [])
    finally:
        handle.close()
    return count


def read_version_from_asar(path: Path) -> str:
    try:
        package = json.loads(asar_entry(path, 'package.json').decode('utf-8'))
    except (KeyError, ValueError) as error:
        raise CopyError('app.asar has no readable package.json') from error
    version = package.get('version')
    if not isinstance(version, str) or not version:
        raise CopyError('app.asar package.json has no version')
    return version
