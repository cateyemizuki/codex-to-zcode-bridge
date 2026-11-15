#!/usr/bin/env python3
"""Prepare, launch and restore an isolated full copy of ZCode Desktop.

This is the experimental entry point for the desktop sync shim. It copies an
installed Desktop tree (executable, resources and ``app.asar``) to a separate
directory, extracts ``resources/app.asar`` to ``resources/app``, patches the
host bundle and the engine reconcile path, and can restore every touched file
byte for byte.

The installed application is only read. The launcher snapshots and restores the
HKCU shell-integration values that Desktop rewrites on startup; file writes stay
inside the copy and the private state directory.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

if os.name == "nt":
    import winreg

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop_sync_shim as shim  # noqa: E402

HOST_REL = "resources/app/out/host/index.js"
ENGINE_REL = "resources/glm/zcode.cjs"
ASAR_REL = "resources/app.asar"
ASAR_BACKUP_REL = "resources/app.asar.original"
APP_REL = "resources/app"
MAIN_REL = "resources/app/out/main/index.js"
MAIN_ORIGINAL_REL = "resources/app/out/main/index.sync-original.js"
BOOTSTRAP_SOURCE = HERE.parent / "runtime/desktop_sync/bootstrap.mjs"
BOOTSTRAP_MARKER = "ZCODE_DESKTOP_SYNC_BOOTSTRAP_V1"


class CopyError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _asar_header(path: Path):
    handle = path.open("rb")
    try:
        prefix = handle.read(16)
        if len(prefix) != 16:
            raise CopyError("app.asar is truncated")
        string_size = struct.unpack("<I", prefix[12:16])[0]
        if string_size <= 0 or string_size > 64 * 1024 * 1024:
            raise CopyError("app.asar header size is invalid")
        raw = handle.read(string_size)
        try:
            header = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise CopyError(f"app.asar header is not valid JSON: {error}") from error
        base = 16 + string_size + ((4 - string_size % 4) % 4)
        return handle, header, base
    except Exception:
        handle.close()
        raise


def asar_entry(path: Path, name: str) -> bytes:
    handle, header, base = _asar_header(path)
    try:
        node = header
        for part in name.split("/"):
            node = node["files"][part]
        if "files" in node or "offset" not in node:
            raise CopyError(f"asar entry is not a file: {name}")
        handle.seek(base + int(node["offset"]))
        return handle.read(int(node["size"]))
    finally:
        handle.close()


def extract_asar(path: Path, destination: Path) -> int:
    handle, header, base = _asar_header(path)
    unpacked = path.with_name(path.name + ".unpacked")
    count = 0

    def write_file(parts, node):
        nonlocal count
        relative = Path(*parts)
        target = (destination / relative).resolve()
        try:
            target.relative_to(destination.resolve())
        except ValueError as error:
            raise CopyError("asar entry escapes the destination") from error
        target.parent.mkdir(parents=True, exist_ok=True)
        if node.get("unpacked"):
            source = unpacked.joinpath(*parts)
            if not source.is_file():
                raise CopyError(f"unpacked asar entry is missing: {source}")
            shutil.copy2(source, target)
        else:
            handle.seek(base + int(node["offset"]))
            data = handle.read(int(node["size"]))
            target.write_bytes(data)
        count += 1

    def walk(node, parts):
        for name, child in node.get("files", {}).items():
            if not isinstance(child, dict):
                raise CopyError("unsupported asar entry shape")
            if "files" in child:
                walk(child, parts + [name])
            else:
                write_file(parts + [name], child)

    try:
        walk(header, [])
    finally:
        handle.close()
    return count


def copy_tree(source: Path, destination: Path, hardlink: bool) -> None:
    for base, dirs, files in os.walk(source):
        relative = Path(base).relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = Path(base) / name
            dst = target_dir / name
            if hardlink:
                try:
                    os.link(src, dst)
                    continue
                except OSError:
                    pass
            shutil.copy2(src, dst)


def bootstrap_source() -> str:
    try:
        text = BOOTSTRAP_SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        raise CopyError(f"bootstrap source is missing: {error}") from error
    if text.count(BOOTSTRAP_MARKER) != 1:
        raise CopyError("bootstrap source must contain exactly one version marker")
    return text


def main_entry_hash(asar: Path) -> str:
    try:
        return sha256_bytes(asar_entry(asar, "out/main/index.js"))
    except (CopyError, KeyError, OSError) as error:
        raise CopyError(f"app.asar has no readable main entry: {error}") from error


def install_bootstrap(destination: Path, original_sha256: str) -> dict:
    """Wrap the extracted main entry so isolated Electron paths exist early.

    Electron in a copied Desktop tree cannot resolve appData/userData through
    the path service, and the application's telemetry module calls
    ``app.getPath('userData')`` during module evaluation. Its rejected promise is
    awaited by the app-ready handler, so the primary window is never created.
    The wrapper sets the two isolated directories the launcher already passes,
    then loads the byte-identical original main entry.
    """
    main = destination / MAIN_REL
    original_path = destination / MAIN_ORIGINAL_REL
    if not main.is_file():
        raise CopyError(f"copy has no application main entry: {MAIN_REL}")
    data = main.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CopyError("application main entry is not valid UTF-8") from error
    if BOOTSTRAP_MARKER in text:
        raise CopyError("bootstrap marker already present; restore before reinstalling")
    if original_path.exists():
        raise CopyError("bootstrap original already exists")
    if sha256_bytes(data) != original_sha256:
        raise CopyError("application main entry does not match app.asar; refusing to wrap it")
    wrapper = bootstrap_source().encode("utf-8")
    shim._atomic_write(original_path, data)
    if sha256_file(original_path) != original_sha256:
        raise CopyError("bootstrap original verification failed")
    shim._atomic_write(main, wrapper)
    if sha256_file(main) != sha256_bytes(wrapper):
        raise CopyError("bootstrap verification failed; restore from the copy")
    return {
        "path": str(main),
        "originalPath": str(original_path),
        "marker": BOOTSTRAP_MARKER,
        "originalSha256": original_sha256,
        "bootstrapSha256": sha256_bytes(wrapper),
    }


def verify_bootstrap(destination: Path) -> dict:
    main = destination / MAIN_REL
    original_path = destination / MAIN_ORIGINAL_REL
    if not main.is_file() or not original_path.is_file():
        raise CopyError("copy has no sync startup entry; run prepare on an empty copy first")
    try:
        text = main.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CopyError(f"startup entry is not readable UTF-8: {error}") from error
    if BOOTSTRAP_MARKER not in text:
        raise CopyError("startup entry is not the sync bootstrap; restore the copy first")
    report = {
        "path": str(main),
        "originalPath": str(original_path),
        "bootstrapSha256": sha256_file(main),
        "originalSha256": sha256_file(original_path),
    }
    backup = destination / ASAR_BACKUP_REL
    if backup.is_file():
        expected = main_entry_hash(backup)
        if report["originalSha256"] != expected:
            raise CopyError("bootstrap original does not match app.asar.original; restore the copy first")
        report["asarMainSha256"] = expected
    return report


def verify_patched_copy(destination: Path) -> dict:
    """Refuse to launch when the copy lost the host/engine sync patches."""
    host = destination / HOST_REL
    engine = destination / ENGINE_REL
    try:
        host_text = host.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CopyError(f"host bundle is not readable UTF-8: {error}") from error
    if shim.SHIM_MARKER not in host_text:
        raise CopyError("host bundle has no sync shim; run prepare on an empty copy first")
    try:
        engine_bytes = engine.read_bytes()
    except OSError as error:
        raise CopyError(f"engine bundle is not readable: {error}") from error
    if b"hydratePublisher(s,void 0,!0)" not in engine_bytes:
        raise CopyError("engine bundle has no reconcile hydration patch; run prepare first")
    return {
        "hostShimMarker": shim.SHIM_MARKER,
        "hostSha256": sha256_file(host),
        "engineReconcilePatch": True,
        "engineSha256": sha256_bytes(engine_bytes),
    }


def read_version_from_asar(path: Path) -> str:
    try:
        package = json.loads(asar_entry(path, "package.json").decode("utf-8"))
    except (KeyError, ValueError) as error:
        raise CopyError("app.asar has no readable package.json") from error
    version = package.get("version")
    if not isinstance(version, str) or not version:
        raise CopyError("app.asar package.json has no version")
    return version


def expected_engine_hash(version: str, manifest: dict | None) -> str:
    if manifest is not None:
        if manifest.get("desktopVersion") != version:
            raise CopyError("manifest version does not match the application version")
        value = manifest.get("targets", {}).get(ENGINE_REL)
        if not isinstance(value, str):
            raise CopyError(f"manifest does not list {ENGINE_REL}")
        builtin = shim.SUPPORTED_ENGINE.get(version)
        if builtin and value != builtin:
            raise CopyError(
                f"manifest engine hash conflicts with the built-in hash for Desktop {version}"
            )
        return value
    value = shim.SUPPORTED_ENGINE.get(version)
    if not value:
        raise CopyError(
            f"unsupported Desktop version {version}; only {', '.join(sorted(shim.SUPPORTED_ENGINE))} is built in"
        )
    return value


def expected_host_hash(version: str, manifest: dict | None) -> str:
    if manifest is not None:
        targets = manifest.get("targets", {})
        value = targets.get(HOST_REL)
        if not isinstance(value, str):
            # Allow the host installer's app-relative target as well.
            value = targets.get(shim.TARGET)
        if not isinstance(value, str):
            raise CopyError(f"manifest does not list {HOST_REL}")
        return value
    value = shim.SUPPORTED.get(version, {}).get(shim.TARGET)
    if not value:
        raise CopyError(
            f"unsupported Desktop version {version}; only {', '.join(sorted(shim.SUPPORTED))} is built in"
        )
    return value


def host_manifest(version: str, manifest: dict | None, host_path: Path) -> dict:
    value = expected_host_hash(version, manifest)
    return {"desktopVersion": version, "targets": {shim.TARGET: value}}


REGISTRY_KEYS = (
    r"Software\Classes\zcode",
    r"Software\Classes\zcode\shell",
    r"Software\Classes\zcode\shell\open",
    r"Software\Classes\zcode\shell\open\command",
    r"Software\Classes\Directory\shell\ZCode.OpenInZCode",
    r"Software\Classes\Directory\shell\ZCode.OpenInZCode\command",
    r"Software\Classes\Drive\shell\ZCode.OpenInZCode",
    r"Software\Classes\Drive\shell\ZCode.OpenInZCode\command",
)


def snapshot_shell_registry() -> dict:
    if os.name != "nt":
        return {"platform": os.name, "keys": {}}
    keys = {}
    for path in REGISTRY_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                values = []
                index = 0
                while True:
                    try:
                        name, value, kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    values.append({"name": name, "type": kind, "value": value})
                    index += 1
                keys[path] = {"exists": True, "values": values}
        except FileNotFoundError:
            keys[path] = {"exists": False, "values": []}
        except OSError as error:
            keys[path] = {"exists": False, "values": [], "error": str(error)}
    return {"platform": os.name, "keys": keys}


def _delete_tree(path: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(path + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def restore_shell_registry(snapshot: dict) -> dict:
    if os.name != "nt" or not isinstance(snapshot, dict) or snapshot.get("platform") != "nt":
        return {"ok": True, "skipped": True}
    restored = []
    keys = snapshot.get("keys", {})
    # Delete keys that did not exist before, deepest first.
    for path in sorted(keys, key=lambda value: value.count("\\"), reverse=True):
        info = keys[path]
        if not info.get("exists"):
            _delete_tree(path)
            restored.append({"path": path, "deleted": True})
            continue
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            wanted = {item["name"] for item in info.get("values", [])}
            index = 0
            while True:
                try:
                    name, _, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                if name not in wanted:
                    winreg.DeleteValue(key, name)
                    continue
                index += 1
            for item in info.get("values", []):
                winreg.SetValueEx(key, item["name"], 0, item["type"], item["value"])
        restored.append({"path": path, "deleted": False})
    return {"ok": True, "restored": restored}


def registry_snapshot_path(state_dir: Path) -> Path:
    return state_dir / "registry-before.json"


def load_registry_snapshot(state_dir: Path) -> dict | None:
    path = registry_snapshot_path(state_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def registry_mentions(destination: Path) -> bool:
    if os.name != "nt":
        return False
    needle = str(destination.resolve()).lower().replace("/", "\\")
    snapshot = snapshot_shell_registry()
    for info in snapshot.get("keys", {}).values():
        for item in info.get("values", []):
            value = item.get("value")
            if isinstance(value, str) and needle in value.lower().replace("/", "\\"):
                return True
    return False


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def spawn_registry_guard(state_dir: Path, pid: int) -> int | None:
    if os.name != "nt":
        return None
    snapshot = registry_snapshot_path(state_dir)
    if not snapshot.is_file():
        return None
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_registry-guard",
         "--state-dir", str(state_dir), "--pid", str(pid)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return process.pid


def prepare(
    source: Path,
    destination: Path,
    state_dir: Path,
    manifest: dict | None = None,
    hardlink: bool = False,
    dry_run: bool = False,
) -> dict:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    executable = source / "ZCode.exe"
    asar = source / ASAR_REL
    if not executable.is_file() or not asar.is_file():
        raise CopyError("source does not look like a packaged ZCode Desktop install")
    if destination.exists() and any(destination.iterdir()):
        raise CopyError("destination is not empty")
    version = read_version_from_asar(asar)
    engine_expected = expected_engine_hash(version, manifest)
    host_expected = expected_host_hash(version, manifest)
    source_engine = source / ENGINE_REL
    if not source_engine.is_file():
        raise CopyError(f"source is missing {ENGINE_REL}")
    shim.verify_engine(source_engine, engine_expected)
    if sha256_bytes(asar_entry(asar, "out/host/index.js")) != host_expected:
        raise CopyError("host bundle in app.asar does not match the expected SHA-256")
    main_hash = main_entry_hash(asar)
    plan = {
        "ok": True,
        "dryRun": dry_run,
        "source": str(source),
        "destination": str(destination),
        "desktopVersion": version,
        "asarSha256": sha256_file(asar),
        "executableSha256": sha256_file(executable),
        "engineExpectedSha256": engine_expected,
        "mainOriginalSha256": main_hash,
        "hardlink": hardlink,
    }
    if dry_run:
        plan["files"] = sum(len(files) for _, _, files in os.walk(source))
        plan["bytes"] = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        plan["registryRedirected"] = registry_mentions(destination)
        return plan
    if registry_mentions(destination):
        raise CopyError(
            "HKCU shell integration already points at the destination; restore the registry first"
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    if not registry_snapshot_path(state_dir).exists():
        shim._atomic_write(
            registry_snapshot_path(state_dir),
            (json.dumps(snapshot_shell_registry(), indent=2) + "\n").encode(),
        )

    destination.mkdir(parents=True, exist_ok=True)
    copy_tree(source, destination, hardlink)
    target_asar = destination / ASAR_REL
    if sha256_file(target_asar) != plan["asarSha256"]:
        raise CopyError("copied app.asar hash differs from the source")
    extracted = destination / APP_REL
    if extracted.exists():
        shutil.rmtree(extracted)
    count = extract_asar(target_asar, extracted)
    bootstrap = install_bootstrap(destination, main_hash)
    target_engine = destination / ENGINE_REL
    engine_plan = shim.engine_plan(target_engine, engine_expected)
    backup = state_dir / "backup"
    shim._atomic_write(backup / ENGINE_REL, engine_plan.original)
    shim._atomic_write(target_engine, engine_plan.patched)
    if sha256_file(target_engine) != engine_plan.patched_sha256:
        raise CopyError("engine patch verification failed")

    host_app = destination / APP_REL
    host_manifest_file = state_dir / "host-manifest.json"
    host_manifest_file.parent.mkdir(parents=True, exist_ok=True)
    host_data = host_manifest(version, manifest, host_app / "out/host/index.js")
    shim._atomic_write(host_manifest_file, (json.dumps(host_data, indent=2) + "\n").encode())
    host_report = shim.install(host_app, state_dir / "host", shim.load_manifest(host_manifest_file))

    backup_asar = destination / ASAR_BACKUP_REL
    if backup_asar.exists():
        raise CopyError("asar backup already exists")
    target_asar.rename(backup_asar)
    state = {
        "kind": "zcode-desktop-sync-copy",
        "formatVersion": 1,
        "desktopVersion": version,
        "source": str(source),
        "destination": str(destination),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "asar": {
            "original": str(backup_asar),
            "extracted": str(extracted),
            "originalSha256": plan["asarSha256"],
        },
        "executable": {
            "path": str(destination / "ZCode.exe"),
            "sha256": plan["executableSha256"],
        },
        "host": {
            "path": host_report["target"],
            "originalSha256": host_report["originalSha256"],
            "patchedSha256": host_report["patchedSha256"],
        },
        "engine": {
            "path": str(engine_plan.target),
            "originalSha256": engine_plan.original_sha256,
            "patchedSha256": engine_plan.patched_sha256,
        },
        "bootstrap": bootstrap,
        "extractedFiles": count,
    }
    shim._atomic_write(state_dir / "copy-state.json", (json.dumps(state, indent=2) + "\n").encode())
    plan.update({
        "extractedFiles": count,
        "hostPatchedSha256": host_report["patchedSha256"],
        "enginePatchedSha256": engine_plan.patched_sha256,
        "bootstrapSha256": bootstrap["bootstrapSha256"],
        "state": str(state_dir / "copy-state.json"),
        "mainEntry": str(extracted / "out/main/index.js"),
    })
    return plan


def _load_state(state_dir: Path) -> dict:
    try:
        state = json.loads((state_dir / "copy-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CopyError(f"copy state is not readable: {error}") from error
    if state.get("kind") != "zcode-desktop-sync-copy":
        raise CopyError("copy state has an unexpected kind")
    return state


def restore(state_dir: Path, force: bool = False) -> dict:
    state_dir = state_dir.expanduser().resolve()
    state = _load_state(state_dir)
    destination = Path(state["destination"]).resolve()
    host = Path(state["host"]["path"]).resolve()
    engine = Path(state["engine"]["path"]).resolve()
    host_expected = state["host"]["originalSha256"]
    if (state_dir / "host" / "shim-state.json").exists():
        shim.restore(state_dir / "host", force)
    elif host.is_file() and sha256_file(host) != host_expected:
        raise CopyError("host bundle does not match the original and has no restore state")
    if host.is_file() and sha256_file(host) != host_expected:
        raise CopyError("restored host bundle hash mismatch")

    engine_expected = state["engine"]["originalSha256"]
    engine_backup = state_dir / "backup" / ENGINE_REL
    if engine_backup.is_file():
        original = engine_backup.read_bytes()
        if sha256_bytes(original) != engine_expected:
            raise CopyError("engine backup hash mismatch")
        current = engine.read_bytes() if engine.is_file() else b""
        if sha256_bytes(current) == engine_expected:
            pass
        elif sha256_bytes(current) != state["engine"]["patchedSha256"] and not force:
            raise CopyError(f"target changed after installation: {engine}")
        else:
            shim._atomic_write(engine, original)
    if not engine.is_file() or sha256_file(engine) != engine_expected:
        raise CopyError("restored engine hash mismatch")

    extracted = destination / APP_REL
    backup_asar = destination / ASAR_BACKUP_REL
    target_asar = destination / ASAR_REL
    if extracted.exists():
        shutil.rmtree(extracted)
    if backup_asar.is_file():
        if target_asar.exists():
            raise CopyError("both app.asar and app.asar.original exist")
        backup_asar.rename(target_asar)
    if target_asar.is_file() and sha256_file(target_asar) != state["asar"]["originalSha256"]:
        raise CopyError("restored app.asar hash does not match the original")
    executable = Path(state["executable"]["path"])
    if not executable.is_file() or sha256_file(executable) != state["executable"]["sha256"]:
        raise CopyError("executable changed; refusing to report a clean restore")

    report = {
        "ok": True,
        "restored": True,
        "destination": str(destination),
        "hostSha256": host_expected,
        "engineSha256": sha256_file(engine),
        "asarSha256": sha256_file(target_asar) if target_asar.is_file() else None,
        "executableSha256": sha256_file(executable),
        "restoredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    snapshot = load_registry_snapshot(state_dir) or load_registry_snapshot(destination / ".lab")
    if snapshot is not None:
        report["registryRestored"] = restore_shell_registry(snapshot)
    shim._atomic_write(state_dir / "last-restore.json", (json.dumps(report, indent=2) + "\n").encode())
    (state_dir / "copy-state.json").unlink(missing_ok=True)
    shutil.rmtree(state_dir / "host", ignore_errors=True)
    shutil.rmtree(state_dir / "backup", ignore_errors=True)
    (state_dir / "host-manifest.json").unlink(missing_ok=True)
    return report


def launch(
    destination: Path,
    user_data: Path,
    home: Path,
    workspace: Path | None,
    sync_config: Path | None,
    personal_provider_config: Path | None = None,
    execute: bool = False,
    smoke: bool = False,
    timeout: float = 20.0,
) -> dict:
    destination = destination.expanduser().resolve()
    user_data = user_data.expanduser().resolve()
    home = home.expanduser().resolve()
    executable = destination / "ZCode.exe"
    if not executable.is_file():
        raise CopyError("copy has no ZCode.exe")
    app_main = destination / APP_REL / "out/main/index.js"
    if not app_main.is_file():
        raise CopyError("copy has no extracted application main entry")
    bootstrap = verify_bootstrap(destination)
    patches = verify_patched_copy(destination)
    user_data.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    # Do not inherit the caller's ZCODE_* provider/session overrides.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("ZCODE_") and key not in ("HOME", "USERPROFILE")}
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "ZCODE_DESKTOP_USER_DATA_DIR": str(user_data),
        "ZCODE_DESKTOP_HOME_DIR": str(home),
        "ZCODE_DESKTOP_SESSION_DATA_DIR": str(home / "session"),
        "ZCODE_ENV": "production",
        "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(destination / "resources/config/provider/zcode-builtin.json"),
    })
    if personal_provider_config is not None:
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(personal_provider_config.expanduser().resolve())
    if sync_config is not None:
        env["ZCODE_DESKTOP_SYNC_CONFIG"] = str(sync_config.expanduser().resolve())
    command = [str(executable), "--user-data-dir", str(user_data)]
    if workspace is not None:
        command += ["--open-workspace", str(workspace.expanduser().resolve())]
    if smoke:
        command.append("--version")
    report = {
        "ok": True,
        "executable": str(executable),
        "command": command,
        "env": {key: value for key, value in env.items() if key.startswith("ZCODE_") or key in ("HOME", "USERPROFILE")},
        "entry": bootstrap,
        "patches": patches,
        "executed": execute,
    }
    if not execute:
        return report
    state_dir = destination / ".lab"
    state_dir.mkdir(parents=True, exist_ok=True)
    snapshot = load_registry_snapshot(state_dir)
    if snapshot is None:
        if registry_mentions(destination):
            raise CopyError("HKCU shell integration already points at the destination; restore the registry first")
        snapshot = snapshot_shell_registry()
        shim._atomic_write(
            registry_snapshot_path(state_dir),
            (json.dumps(snapshot, indent=2) + "\n").encode(),
        )
    if smoke:
        try:
            completed = subprocess.run(command, env=env, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=timeout)
            report.update({
                "exitCode": completed.returncode,
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            })
        except subprocess.TimeoutExpired as error:
            report.update({"timedOut": True, "stdout": (error.stdout or "")[-2000:],
                           "stderr": (error.stderr or "")[-2000:]})
        report["registryRestored"] = restore_shell_registry(snapshot)
        return report
    process = subprocess.Popen(command, env=env)
    report["pid"] = process.pid
    # Desktop rewrites HKCU shell integration during startup. Give it a short
    # window, restore the original values, then keep a guard alive for the app.
    time.sleep(6)
    report["registryRestored"] = restore_shell_registry(snapshot)
    report["registryGuardPid"] = spawn_registry_guard(state_dir, process.pid)
    return report


def registry_guard(state_dir: Path, pid: int) -> int:
    snapshot = load_registry_snapshot(state_dir)
    if snapshot is None:
        return 2
    while _process_alive(pid):
        restore_shell_registry(snapshot)
        time.sleep(2)
    restore_shell_registry(snapshot)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path, default=shim.default_state_dir() / "copy")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare_parser = commands.add_parser("prepare", help="copy and patch an installed Desktop")
    prepare_parser.add_argument("--source", type=Path, required=True)
    prepare_parser.add_argument("--destination", type=Path, required=True)
    prepare_parser.add_argument("--manifest", type=Path)
    prepare_parser.add_argument("--hardlink", action="store_true")
    prepare_parser.add_argument("--dry-run", action="store_true")
    prepare_parser.add_argument("--experimental", action="store_true")

    restore_parser = commands.add_parser("restore", help="restore the copied application")
    restore_parser.add_argument("--force", action="store_true")

    bootstrap_parser = commands.add_parser(
        "bootstrap", help="wrap the extracted main entry of an already prepared copy"
    )
    bootstrap_parser.add_argument("--destination", type=Path, required=True)

    launch_parser = commands.add_parser("launch", help="show or run the isolated launch entry")
    launch_parser.add_argument("--destination", type=Path, required=True)
    launch_parser.add_argument("--user-data-dir", type=Path)
    launch_parser.add_argument("--home", type=Path)
    launch_parser.add_argument("--workspace", type=Path)
    launch_parser.add_argument("--sync-config", type=Path)
    launch_parser.add_argument("--personal-provider-config", type=Path)
    launch_parser.add_argument("--execute", action="store_true")
    launch_parser.add_argument("--smoke", action="store_true")
    launch_parser.add_argument("--timeout", type=float, default=20.0)

    guard_parser = commands.add_parser("_registry-guard")
    guard_parser.add_argument("--state-dir", type=Path, required=True)
    guard_parser.add_argument("--pid", type=int, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            if not args.dry_run and not args.experimental:
                raise CopyError("prepare copies a third-party application; run --dry-run first, then --experimental")
            manifest = shim.load_manifest(args.manifest) if args.manifest else None
            result = prepare(args.source, args.destination, args.state_dir, manifest, args.hardlink, args.dry_run)
        elif args.command == "restore":
            result = restore(args.state_dir, args.force)
        elif args.command == "bootstrap":
            destination = args.destination.expanduser().resolve()
            backup = destination / ASAR_BACKUP_REL
            if not backup.is_file():
                raise CopyError("copy has no app.asar.original; run prepare first")
            result = install_bootstrap(destination, main_entry_hash(backup))
        elif args.command == "_registry-guard":
            return registry_guard(args.state_dir, args.pid)
        elif args.command == "launch":
            root = args.destination.expanduser().resolve()
            user_data = args.user_data_dir or (root / ".lab/user-data")
            home = args.home or (root / ".lab/home")
            result = launch(root, user_data, home, args.workspace, args.sync_config,
                            args.personal_provider_config, args.execute, args.smoke, args.timeout)
        else:  # pragma: no cover
            raise CopyError(f"unknown command {args.command}")
    except (CopyError, shim.ShimError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
