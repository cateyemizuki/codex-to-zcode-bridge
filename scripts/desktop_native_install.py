#!/usr/bin/env python3
"""Explicit opt-in installer for the original (installed) ZCode Desktop host.

This tool is separate from ``install.py`` and from the copy workflow. A normal
plugin install never touches ZCode; reaching this module is the opt-in.

Model
-----
``prepare`` builds the whole replacement tree in a private state directory and
in ``<target>/.zcode-native-staging``: the packaged ``resources/app.asar`` is
expanded with the existing standard-library ASAR reader, the resulting
``out/host/index.js`` gets the reviewed 0.5.1 shim patch, ``out/main/index.js``
becomes a narrow bootstrap that loads the untouched original next to it, and a
pointer file names the private control file. Nothing in the target install is
modified by ``prepare``. The staged tree is hashed deterministically (relative
path + size + SHA-256 for every file) and recorded in a private ledger.

``apply`` refuses while any process runs from the target executable, re-checks
every original byte, then atomically renames ``resources/app.asar`` to
``resources/app.asar.original`` and the staged tree to ``resources/app``. Those
two renames are the only changes this tool owns; ``resources/glm/zcode.cjs``
(the independent engine) is only ever read, never written, backed up or
replaced. The native own-engine sessions do not need the older hydrate patch,
so the engine must stay byte-identical.

``status`` and ``rollback`` compare the recorded hashes and the recorded tree
digest with the install. Rollback refuses to overwrite anything that changed
after installation and never writes the engine.

No command accepts a manifest, a skip-hash flag, an arbitrary path to execute
or a generic RPC surface. All values below are fixed for Desktop 3.18.2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop_sync_copy as copy_tool  # noqa: E402
import desktop_sync_shim as shim  # noqa: E402


DESKTOP_VERSION = "3.18.2"

# Packaged install layout (relative to the install root).
EXE_REL = "ZCode.exe"
ASAR_REL = "resources/app.asar"
ASAR_BACKUP_REL = "resources/app.asar.original"
APP_REL = "resources/app"
RESOURCES_REL = "resources"
ENGINE_REL = "resources/glm/zcode.cjs"
INSTALL_MANIFEST_REL = ".zcode-install-manifest"

# Expanded layout (relative to the expanded application root).
HOST_REL = "out/host/index.js"
MAIN_REL = "out/main/index.js"
MAIN_ORIGINAL_REL = "out/main/index.native-original.js"
POINTER_REL = "out/main/zcode-native-control.json"

BOOTSTRAP_SOURCE = HERE.parent / "runtime/desktop_sync/native_bootstrap.mjs"
BOOTSTRAP_MARKER = "ZCODE_DESKTOP_NATIVE_BOOTSTRAP_V1"
SHIM_MARKER = "ZCODE_DESKTOP_SYNC_SHIM_V1"
POINTER_KIND = "zcode-desktop-native-control-pointer"

STAGING_DIRNAME = ".zcode-native-staging"
LEDGER_FILENAME = "ledger.json"
LEDGER_KIND = "zcode-desktop-native-host"
OWNER = "zcode-desktop-native-host"
FORMAT_VERSION = 1

# Reviewed original bytes. These are the only accepted inputs; there is no
# manifest or override.
ASAR_BYTES = 384840277
ASAR_SHA256 = "4c0b8510a1ff7f1142714a1dc37139058254b784ad72b70ba781f09334b0c131"
HOST_SHA256 = "e6346359c9b659b7f33595565cde04b1badaca02babeee6d46f4bdbc0639f105"
MAIN_SHA256 = "e0f1d059554f61684d2f5d720852a28fec7c645293499d84c4637a874727f78e"
ENGINE_SHA256 = "08726b5ef69e1e9072828baa4656713cb3aac93bb096938681278c5600312b2c"
EXE_SHA256 = "e853398ea2023e3e472fd2799389ad02445df5c2bfd36b2ed1f1e3dfe3dc958e"


class NativeInstallError(RuntimeError):
    """A check or state transition refused to continue."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def default_state_dir() -> Path:
    override = os.environ.get("ZCODE_DESKTOP_NATIVE_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local/share")
    return Path(base) / "ZCodeDesktopNativeHost"


def ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_FILENAME


def _write_json(path: Path, value: dict) -> None:
    shim._atomic_write(path, (json.dumps(value, indent=2, sort_keys=False) + "\n").encode("utf-8"))


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NativeInstallError(f"{what} is not readable JSON: {error}") from error
    if not isinstance(data, dict):
        raise NativeInstallError(f"{what} is not a JSON object")
    return data


def _within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
    except ValueError:
        return False
    return True


def _require_within(path: Path, root: Path, what: str) -> None:
    if not _within(path, root):
        raise NativeInstallError(f"{what} resolves outside the owned target: {path}")


def tree_manifest(root: Path) -> list[dict]:
    """Deterministic relative-path + size + SHA-256 list for a directory tree."""
    root = root.resolve()
    entries: list[dict] = []
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink():
                raise NativeInstallError(f"staged tree contains a symlink: {path}")
            relative = path.relative_to(root).as_posix()
            entries.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    entries.sort(key=lambda item: item["path"])
    return entries


def tree_digest(entries: list[dict]) -> str:
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(canonical)


def tree_facts(root: Path) -> dict:
    entries = tree_manifest(root)
    return {
        "digest": tree_digest(entries),
        "files": len(entries),
        "bytes": sum(item["size"] for item in entries),
    }


def _asar_leaf_count(asar: Path) -> int:
    handle, header, _base = copy_tool._asar_header(asar)
    try:
        count = 0

        def walk(node):
            nonlocal count
            for child in node.get("files", {}).values():
                if not isinstance(child, dict):
                    raise NativeInstallError("unsupported asar entry shape")
                if "files" in child:
                    walk(child)
                else:
                    count += 1

        walk(header)
        return count
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# OS probe: which processes run from the target executable (read-only).


def windows_zcode_processes() -> list[dict]:
    if os.name != "nt":
        return []
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='ZCode.exe'\" | "
        "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=60, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise NativeInstallError(f"cannot query ZCode processes: {error}") from error
    if completed.returncode != 0:
        raise NativeInstallError(f"cannot query ZCode processes: {completed.stderr.strip()[:200]}")
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as error:
        raise NativeInstallError(f"process query returned unexpected output: {error}") from error
    if isinstance(data, dict):
        data = [data]
    processes = []
    for item in data:
        if not isinstance(item, dict):
            continue
        processes.append({
            "pid": item.get("ProcessId"),
            "path": str(item.get("ExecutablePath") or ""),
        })
    return processes


# Replaced by tests with a deterministic stub; never reads anything itself.
PROCESS_PROBE = windows_zcode_processes


def processes_from_target(target: Path, probe=None, strict: bool = True) -> dict:
    """Split probe results into processes from the target and unknown paths.

    ``strict`` callers (apply/rollback) refuse when a ZCode process reports no
    executable path: an unknown path must never be read as "target not running".
    """
    probe = probe or PROCESS_PROBE
    target_exe = os.path.normcase(os.path.abspath(str((target / EXE_REL).resolve(strict=False))))
    running: list[dict] = []
    unknown: list = []
    for item in probe() or ():
        if not isinstance(item, dict):
            unknown.append(None)
            continue
        pid = item.get("pid")
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            unknown.append(pid)
            continue
        if os.path.normcase(os.path.abspath(path)) == target_exe:
            running.append({"pid": pid, "path": path})
    if strict and unknown:
        raise NativeInstallError(
            "cannot determine the executable path of running ZCode process(es) "
            f"{unknown}; refusing to treat the target as stopped"
        )
    return {"running": running, "unknown": unknown}


# ---------------------------------------------------------------------------
# Ledger.


def load_ledger(state_dir: Path) -> dict | None:
    path = ledger_path(state_dir)
    if not path.is_file():
        return None
    ledger = _read_json(path, "native install ledger")
    if ledger.get("kind") != LEDGER_KIND:
        raise NativeInstallError("ledger kind does not match this tool")
    if ledger.get("formatVersion") != FORMAT_VERSION:
        raise NativeInstallError("ledger format version is not supported")
    if ledger.get("owner") != OWNER:
        raise NativeInstallError("ledger ownership marker is missing")
    return ledger


def _foreign_state_artifacts(state_dir: Path) -> list[str]:
    """Private files this tool only creates as part of its own installation."""
    found: list[str] = []
    for name in ("control.json", "host-discovery.json", "native-ownership.json"):
        if (state_dir / name).exists():
            found.append(name)
    if (state_dir / "host").exists():
        found.append("host/")
    return found


def _save_ledger(state_dir: Path, ledger: dict) -> None:
    ledger["updatedAt"] = _now()
    _write_json(ledger_path(state_dir), ledger)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_same_target(ledger: dict, target: Path) -> Path:
    recorded = Path(str(ledger.get("target") or ""))
    if not str(recorded):
        raise NativeInstallError("ledger has no recorded target")
    if os.path.normcase(os.path.abspath(str(recorded))) != os.path.normcase(os.path.abspath(str(target))):
        raise NativeInstallError(
            f"ledger belongs to a different target: {recorded}"
        )
    return recorded


# ---------------------------------------------------------------------------
# preflight


def preflight(target: Path, state_dir: Path) -> dict:
    """Read-only validation of an installed, unmodified Desktop 3.18.2 tree."""
    target = target.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    if not target.is_dir():
        raise NativeInstallError(f"target directory does not exist: {target}")

    exe = target / EXE_REL
    asar = target / ASAR_REL
    engine = target / ENGINE_REL
    manifest = target / INSTALL_MANIFEST_REL
    for path, what in (
        (exe, EXE_REL),
        (asar, ASAR_REL),
        (engine, ENGINE_REL),
        (manifest, INSTALL_MANIFEST_REL),
    ):
        if not path.is_file():
            raise NativeInstallError(f"target is missing {what}")

    app_dir = target / APP_REL
    if app_dir.exists():
        raise NativeInstallError(
            f"{APP_REL} already exists; refusing to install over an expanded or copied application"
        )
    if (target / ASAR_BACKUP_REL).exists():
        raise NativeInstallError(
            f"{ASAR_BACKUP_REL} already exists; the target is already prepared or belongs to another install"
        )

    staging = target / STAGING_DIRNAME
    if staging.exists() and any(staging.iterdir()):
        raise NativeInstallError(f"staging directory is not empty: {staging}")

    existing = load_ledger(state_dir)
    if existing is not None:
        status = existing.get("status")
        if status in ("prepared", "applied") or str(status).startswith("partial"):
            raise NativeInstallError(
                f"this tool already owns a {status} installation recorded in {state_dir}; "
                "run status or rollback first"
            )
        _require_same_target(existing, target)
    if existing is None or existing.get("status") == "rolled_back":
        foreign = _foreign_state_artifacts(state_dir)
        if foreign:
            raise NativeInstallError(
                "state directory already contains files this tool did not record a ledger for: "
                f"{', '.join(foreign)}; choose another --state-dir or roll back the owning installation"
            )

    # The pinned builtins must agree with the reviewed shim module.
    if shim.SUPPORTED.get(DESKTOP_VERSION, {}).get(shim.TARGET) != HOST_SHA256:
        raise NativeInstallError("built-in host hash and shim module disagree; refusing")
    if shim.SUPPORTED_ENGINE.get(DESKTOP_VERSION) != ENGINE_SHA256:
        raise NativeInstallError("built-in engine hash and shim module disagree; refusing")

    version = copy_tool.read_version_from_asar(asar)
    if version != DESKTOP_VERSION:
        raise NativeInstallError(
            f"unsupported Desktop version {version}; this tool only accepts {DESKTOP_VERSION}"
        )

    asar_bytes = asar.stat().st_size
    if asar_bytes != ASAR_BYTES:
        raise NativeInstallError(
            f"app.asar size mismatch: expected {ASAR_BYTES}, found {asar_bytes}; refusing"
        )
    asar_sha = sha256_file(asar)
    if asar_sha != ASAR_SHA256:
        raise NativeInstallError(
            f"app.asar SHA-256 mismatch: expected {ASAR_SHA256}, found {asar_sha}; refusing"
        )

    try:
        host_original = copy_tool.asar_entry(asar, HOST_REL)
        main_original = copy_tool.asar_entry(asar, MAIN_REL)
    except (copy_tool.CopyError, KeyError, OSError) as error:
        raise NativeInstallError(f"app.asar is missing the expected entries: {error}") from error
    host_sha = sha256_bytes(host_original)
    if host_sha != HOST_SHA256:
        raise NativeInstallError(
            f"out/host/index.js SHA-256 mismatch: expected {HOST_SHA256}, found {host_sha}; refusing"
        )
    main_sha = sha256_bytes(main_original)
    if main_sha != MAIN_SHA256:
        raise NativeInstallError(
            f"out/main/index.js SHA-256 mismatch: expected {MAIN_SHA256}, found {main_sha}; refusing"
        )
    try:
        host_text = host_original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NativeInstallError("host bundle is not valid UTF-8") from error
    if SHIM_MARKER in host_text:
        raise NativeInstallError("host bundle already carries the shim marker; refusing")
    expose = host_text.count(shim.ANCHOR_EXPOSE)
    api = host_text.count(shim.ANCHOR_API)
    if expose != 1:
        raise NativeInstallError(f"expose anchor appears {expose} times; refusing")
    if api != 1:
        raise NativeInstallError(f"API anchor appears {api} times; refusing")

    engine_sha = sha256_file(engine)
    if engine_sha != ENGINE_SHA256:
        raise NativeInstallError(
            f"resources/glm/zcode.cjs SHA-256 mismatch: expected {ENGINE_SHA256}, "
            f"found {engine_sha}; refusing"
        )
    exe_sha = sha256_file(exe)
    if exe_sha != EXE_SHA256:
        raise NativeInstallError(
            f"ZCode.exe SHA-256 mismatch: expected {EXE_SHA256}, found {exe_sha}; refusing"
        )

    resources = target / RESOURCES_REL
    if not os.access(str(resources), os.W_OK):
        raise NativeInstallError(f"resources directory is not writable: {resources}")
    if not os.access(str(target), os.W_OK):
        raise NativeInstallError(f"target directory is not writable: {target}")

    probe_info = processes_from_target(target, strict=False)
    return {
        "ok": True,
        "command": "preflight",
        "target": str(target),
        "desktopVersion": version,
        "asar": {"bytes": asar_bytes, "sha256": asar_sha, "entries": _asar_leaf_count(asar)},
        "host": {"sha256": host_sha, "anchors": {"expose": expose, "api": api}},
        "main": {"sha256": main_sha},
        "engine": {"sha256": engine_sha},
        "executable": {"sha256": exe_sha},
        "resourcesWritable": True,
        "running": {
            "running": bool(probe_info["running"]),
            "processes": probe_info["running"],
            "unknownPaths": probe_info["unknown"],
        },
        "stateDir": str(state_dir),
    }


# ---------------------------------------------------------------------------
# prepare


def _bootstrap_bytes() -> bytes:
    try:
        text = BOOTSTRAP_SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        raise NativeInstallError(f"bootstrap source is missing: {error}") from error
    if text.count(BOOTSTRAP_MARKER) != 1:
        raise NativeInstallError("bootstrap source must contain exactly one version marker")
    if "'./index.native-original.js'" not in text:
        raise NativeInstallError("bootstrap source does not load the original main entry")
    return text.encode("utf-8")


def prepare(
    target: Path,
    state_dir: Path,
    workspaces: list[str],
    dry_run: bool = False,
) -> dict:
    target = target.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    report = preflight(target, state_dir)

    staging = target / STAGING_DIRNAME
    staging_app = staging / "app"
    control_path = (state_dir / "control.json").resolve()
    bootstrap = _bootstrap_bytes()

    plan = {
        "ok": True,
        "command": "prepare",
        "dryRun": dry_run,
        "target": str(target),
        "desktopVersion": DESKTOP_VERSION,
        "staging": str(staging),
        "control": str(control_path),
        "asarSha256": report["asar"]["sha256"],
        "hostSha256": report["host"]["sha256"],
        "engineSha256": report["engine"]["sha256"],
        "plannedFiles": report["asar"]["entries"],
        "bootstrapSha256": sha256_bytes(bootstrap),
    }
    if dry_run:
        plan["running"] = report["running"]
        return plan

    if staging.exists():
        if any(staging.iterdir()):
            raise NativeInstallError(f"staging directory is not empty: {staging}")
    staging.mkdir(parents=True, exist_ok=True)
    created_control = False
    try:
        # 1. Expand the reviewed app.asar with the existing standard-library reader.
        copy_tool.extract_asar(target / ASAR_REL, staging_app)
        extracted_host = staging_app / HOST_REL
        extracted_main = staging_app / MAIN_REL
        if sha256_file(extracted_host) != HOST_SHA256:
            raise NativeInstallError("expanded host bundle does not match the reviewed hash")
        if sha256_file(extracted_main) != MAIN_SHA256:
            raise NativeInstallError("expanded main entry does not match the reviewed hash")
        if copy_tool.read_version_from_asar(target / ASAR_REL) != DESKTOP_VERSION:
            raise NativeInstallError("expanded application version is not supported")

        # 2. Reuse the reviewed shim patch for out/host/index.js only.
        shim_plan = shim.make_plan(staging_app)
        if shim_plan.original_sha256 != HOST_SHA256:
            raise NativeInstallError("shim plan target is not the reviewed host bundle")
        host_report = shim.install(staging_app, state_dir / "host", None, False)
        if sha256_file(extracted_host) != host_report["patchedSha256"]:
            raise NativeInstallError("patched host verification failed")

        # 3. Wrap out/main/index.js; keep the original byte-identical next to it.
        main_original = staging_app / MAIN_ORIGINAL_REL
        if main_original.exists():
            raise NativeInstallError("bootstrap original already exists in staging")
        shim._atomic_write(main_original, extracted_main.read_bytes())
        if sha256_file(main_original) != MAIN_SHA256:
            raise NativeInstallError("bootstrap original verification failed")
        shim._atomic_write(extracted_main, bootstrap)
        if sha256_file(extracted_main) != sha256_bytes(bootstrap):
            raise NativeInstallError("bootstrap verification failed")

        # 4. Private control file (token stays here) plus the same-directory pointer.
        shim.configure(state_dir, workspaces)
        created_control = True
        shim.configure_native(control_path, True)
        pointer = {
            "kind": POINTER_KIND,
            "formatVersion": 1,
            "owner": OWNER,
            "controlPath": str(control_path),
        }
        _write_json(staging_app / POINTER_REL, pointer)

        # 5. Deterministic digest of the final staged tree (one pass).
        tree = tree_facts(staging_app)
        if tree["files"] == 0:
            raise NativeInstallError("staged tree is empty")

        ledger = {
            "kind": LEDGER_KIND,
            "formatVersion": FORMAT_VERSION,
            "owner": OWNER,
            "status": "prepared",
            "desktopVersion": DESKTOP_VERSION,
            "target": str(target),
            "createdAt": _now(),
            "paths": {
                "exe": str(target / EXE_REL),
                "asar": str(target / ASAR_REL),
                "asarOriginal": str(target / ASAR_BACKUP_REL),
                "app": str(target / APP_REL),
                "engine": str(target / ENGINE_REL),
                "staging": str(staging),
                "stagingApp": str(staging_app),
                "control": str(control_path),
                "pointer": str(staging_app / POINTER_REL),
            },
            "hashes": {
                "exe": report["executable"]["sha256"],
                "asar": report["asar"]["sha256"],
                "hostOriginal": HOST_SHA256,
                "hostPatched": host_report["patchedSha256"],
                "mainOriginal": MAIN_SHA256,
                "bootstrap": sha256_bytes(bootstrap),
                "engine": report["engine"]["sha256"],
            },
            "tree": tree,
            "shimState": str(state_dir / "host"),
            "steps": [],
            "failure": None,
            "appliedAt": None,
            "rolledBackAt": None,
        }
        _save_ledger(state_dir, ledger)
    except Exception:
        # Nothing in the target changed; remove only this run's staging and control.
        try:
            if staging.exists():
                shutil.rmtree(staging)
        finally:
            if created_control:
                (state_dir / "control.json").unlink(missing_ok=True)
                (state_dir / "host-discovery.json").unlink(missing_ok=True)
                host_state = state_dir / "host"
                if host_state.exists():
                    shutil.rmtree(host_state)
        raise

    plan.update({
        "treeDigest": ledger["tree"]["digest"],
        "treeFiles": ledger["tree"]["files"],
        "treeBytes": ledger["tree"]["bytes"],
        "hostPatchedSha256": ledger["hashes"]["hostPatched"],
        "mainOriginalSha256": ledger["hashes"]["mainOriginal"],
        "ledger": str(ledger_path(state_dir)),
        "wroteTarget": False,
    })
    return plan


# ---------------------------------------------------------------------------
# apply


def _verify_original_unchanged(target: Path, ledger: dict) -> dict:
    exe = target / EXE_REL
    asar = target / ASAR_REL
    engine = target / ENGINE_REL
    if not exe.is_file() or not asar.is_file() or not engine.is_file():
        raise NativeInstallError("target files disappeared after prepare")
    if sha256_file(asar) != ledger["hashes"]["asar"]:
        raise NativeInstallError("app.asar changed after prepare; refusing to apply")
    if sha256_file(engine) != ENGINE_SHA256:
        raise NativeInstallError("engine is no longer the reviewed original; refusing to apply")
    if sha256_file(exe) != ledger["hashes"]["exe"]:
        raise NativeInstallError("ZCode.exe changed after prepare; refusing to apply")
    try:
        host_sha = sha256_bytes(copy_tool.asar_entry(asar, HOST_REL))
        main_sha = sha256_bytes(copy_tool.asar_entry(asar, MAIN_REL))
    except (copy_tool.CopyError, KeyError, OSError) as error:
        raise NativeInstallError(f"app.asar entries are no longer readable: {error}") from error
    if host_sha != HOST_SHA256 or main_sha != MAIN_SHA256:
        raise NativeInstallError("app.asar entries changed after prepare; refusing to apply")
    return {"asar": sha256_file(asar), "engine": sha256_file(engine), "exe": sha256_file(exe)}


def _verify_staging(ledger: dict) -> dict:
    staging_app = Path(ledger["paths"]["stagingApp"])
    staging = Path(ledger["paths"]["staging"])
    target = Path(ledger["target"])
    if not staging_app.is_dir():
        raise NativeInstallError("staged application tree is missing; run rollback and prepare again")
    _require_within(staging_app, staging, "staged application tree")
    _require_within(staging, target, "staging directory")
    if staging.name != STAGING_DIRNAME:
        raise NativeInstallError("staging directory name is not the owned one")
    tree = tree_facts(staging_app)
    if tree["digest"] != ledger["tree"]["digest"] or tree["files"] != ledger["tree"]["files"]:
        raise NativeInstallError("staged tree digest changed; run rollback and prepare again")
    if sha256_file(staging_app / HOST_REL) != ledger["hashes"]["hostPatched"]:
        raise NativeInstallError("staged host patch is missing")
    if sha256_file(staging_app / MAIN_REL) != ledger["hashes"]["bootstrap"]:
        raise NativeInstallError("staged bootstrap is missing")
    if sha256_file(staging_app / MAIN_ORIGINAL_REL) != ledger["hashes"]["mainOriginal"]:
        raise NativeInstallError("staged original main entry is missing")
    pointer = _read_json(staging_app / POINTER_REL, "control pointer")
    if pointer.get("controlPath") != ledger["paths"]["control"]:
        raise NativeInstallError("staged control pointer does not match the ledger")
    return tree


def apply(target: Path, state_dir: Path, dry_run: bool = False, probe=None) -> dict:
    target = target.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    ledger = load_ledger(state_dir)
    if ledger is None:
        raise NativeInstallError("no prepared installation; run prepare first")
    _require_same_target(ledger, target)
    if ledger.get("status") != "prepared":
        raise NativeInstallError(
            f"installation is {ledger.get('status')}; only a prepared installation can be applied"
        )

    app_dir = target / APP_REL
    asar = target / ASAR_REL
    asar_backup = target / ASAR_BACKUP_REL
    if app_dir.exists():
        raise NativeInstallError(f"{APP_REL} already exists; refusing to overwrite it")
    if asar_backup.exists():
        raise NativeInstallError(f"{ASAR_BACKUP_REL} already exists; refusing to overwrite it")

    running_info = processes_from_target(target, probe)
    if running_info["running"]:
        pids = ", ".join(str(item["pid"]) for item in running_info["running"])
        raise NativeInstallError(
            f"ZCode is running from the target (pid {pids}); close it normally before apply"
        )

    hashes = _verify_original_unchanged(target, ledger)
    tree = _verify_staging(ledger)

    report = {
        "ok": True,
        "command": "apply",
        "dryRun": dry_run,
        "target": str(target),
        "asar": hashes["asar"],
        "engine": hashes["engine"],
        "treeDigest": tree["digest"],
        "steps": ["rename app.asar -> app.asar.original", "rename staging/app -> resources/app"],
        "engineWritten": False,
    }
    if dry_run:
        return report

    steps: list[str] = []
    ledger["status"] = "partial"
    ledger["phase"] = "apply-in-progress"
    ledger["steps"] = []
    _save_ledger(state_dir, ledger)
    try:
        os.rename(asar, asar_backup)
        steps.append("asar")
        ledger["steps"] = steps
        _save_ledger(state_dir, ledger)
        os.rename(Path(ledger["paths"]["stagingApp"]), app_dir)
        steps.append("app")
        ledger["steps"] = steps
        _save_ledger(state_dir, ledger)

        if sha256_file(asar_backup) != ledger["hashes"]["asar"]:
            raise NativeInstallError("app.asar.original verification failed")
        if sha256_file(app_dir / HOST_REL) != ledger["hashes"]["hostPatched"]:
            raise NativeInstallError("installed host patch verification failed")
        if sha256_file(app_dir / MAIN_REL) != ledger["hashes"]["bootstrap"]:
            raise NativeInstallError("installed bootstrap verification failed")
        if tree_facts(app_dir)["digest"] != ledger["tree"]["digest"]:
            raise NativeInstallError("installed tree digest verification failed")
        if sha256_file(target / ENGINE_REL) != ENGINE_SHA256:
            raise NativeInstallError("engine changed during apply")
    except Exception as error:
        compensated = _compensate_apply(
            app_dir, asar, asar_backup, steps, Path(ledger["paths"]["stagingApp"])
        )
        ledger["status"] = "partial"
        ledger["phase"] = "apply-failed"
        ledger["steps"] = steps
        ledger["failure"] = f"{type(error).__name__}: {error}"
        ledger["compensated"] = compensated
        _save_ledger(state_dir, ledger)
        raise NativeInstallError(
            f"apply failed ({error}); installation recorded as partial"
            + ("; changes were reverted" if compensated else "; manual rollback is required")
        ) from error

    ledger.update({
        "status": "applied",
        "phase": "applied",
        "steps": steps,
        "failure": None,
        "appliedAt": _now(),
    })
    _save_ledger(state_dir, ledger)
    report.update({"status": "applied", "ledger": str(ledger_path(state_dir))})
    return report


def _compensate_apply(
    app_dir: Path,
    asar: Path,
    asar_backup: Path,
    steps: list[str],
    staging_app: Path,
) -> bool:
    """Best-effort revert of this tool's own two renames. Never touches engine."""
    try:
        if "app" in steps and app_dir.exists() and not staging_app.exists():
            staging_app.parent.mkdir(parents=True, exist_ok=True)
            os.rename(app_dir, staging_app)
        if "asar" in steps and asar_backup.exists() and not asar.exists():
            os.rename(asar_backup, asar)
        return asar.is_file() and not asar_backup.exists() and not app_dir.exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# status


def status(target: Path | None, state_dir: Path) -> dict:
    state_dir = state_dir.expanduser().resolve()
    ledger = load_ledger(state_dir)
    if ledger is None:
        stray = bool(target and (target.expanduser().resolve() / APP_REL).exists())
        return {
            "ok": not stray,
            "command": "status",
            "status": "not_installed",
            "conflicts": ["resources/app exists without a ledger"] if stray else [],
            "stateDir": str(state_dir),
        }
    recorded_target = Path(ledger["target"])
    if target is not None and os.path.normcase(os.path.abspath(str(target.expanduser().resolve()))) != os.path.normcase(
        os.path.abspath(str(recorded_target))
    ):
        raise NativeInstallError("ledger belongs to a different target; refusing to report on this one")

    root = recorded_target
    asar = root / ASAR_REL
    asar_backup = root / ASAR_BACKUP_REL
    app_dir = root / APP_REL
    engine = root / ENGINE_REL
    conflicts: list[str] = []
    result = {
        "command": "status",
        "status": ledger.get("status"),
        "desktopVersion": ledger.get("desktopVersion"),
        "target": str(root),
        "ledger": str(ledger_path(state_dir)),
        "conflicts": conflicts,
    }

    engine_sha = sha256_file(engine) if engine.is_file() else ""
    result["engine"] = {
        "sha256": engine_sha,
        "original": engine_sha == ENGINE_SHA256,
        "writtenByThisTool": False,
    }
    if engine_sha and engine_sha != ENGINE_SHA256:
        conflicts.append("engine modified outside this tool; preserved as-is")

    exe = root / EXE_REL
    exe_sha = sha256_file(exe) if exe.is_file() else ""
    result["executable"] = {"sha256": exe_sha, "recorded": ledger["hashes"]["exe"]}
    if exe_sha and exe_sha != ledger["hashes"]["exe"]:
        conflicts.append("ZCode.exe changed after installation (updater or manual change)")

    current = ledger.get("status")
    if current == "prepared":
        staging_app = Path(ledger["paths"]["stagingApp"])
        if asar_backup.exists():
            conflicts.append("resources/app.asar.original exists before apply")
        if staging_app.is_dir():
            tree = tree_facts(staging_app)
            result["tree"] = tree
            if tree["digest"] != ledger["tree"]["digest"]:
                conflicts.append("staged tree digest changed")
                result["status"] = "modified"
        else:
            conflicts.append("staged tree is missing")
            result["status"] = "modified"
        if not asar.is_file():
            conflicts.append("app.asar is missing after prepare")
            result["status"] = "modified"
        elif sha256_file(asar) != ledger["hashes"]["asar"]:
            conflicts.append("app.asar changed after prepare")
            result["status"] = "modified"
    elif current in ("applied",):
        if not app_dir.is_dir():
            conflicts.append("resources/app is missing")
            result["status"] = "modified"
        else:
            tree = tree_facts(app_dir)
            result["tree"] = tree
            if tree["digest"] != ledger["tree"]["digest"]:
                conflicts.append("expanded tree digest changed after installation")
                result["status"] = "modified"
            if sha256_file(app_dir / MAIN_REL) != ledger["hashes"]["bootstrap"]:
                conflicts.append("installed bootstrap is missing or changed")
            if asar_backup.is_file():
                backup_sha = sha256_file(asar_backup)
                result["asarOriginal"] = {"sha256": backup_sha, "recorded": ledger["hashes"]["asar"]}
                if backup_sha != ledger["hashes"]["asar"]:
                    conflicts.append("resources/app.asar.original changed after installation")
            else:
                conflicts.append("resources/app.asar.original is missing")
        if not asar_backup.is_file() and not asar.exists():
            conflicts.append("neither app.asar nor app.asar.original exists")
        if asar.exists():
            conflicts.append("resources/app.asar exists again (auto-update or manual change)")
    elif current == "rolled_back":
        if app_dir.exists() or asar_backup.exists() or not asar.exists():
            conflicts.append("files changed after rollback")
            result["status"] = "modified"
    elif str(current).startswith("partial") or current == "partial":
        result["phase"] = ledger.get("phase")
        if ledger.get("failure"):
            result["failure"] = ledger["failure"]
        result["steps"] = ledger.get("steps", [])
        if asar_backup.exists() and ledger.get("phase") == "apply-failed":
            conflicts.append("apply did not complete; target may need rollback")
        if asar.exists() and ledger.get("phase") == "apply-failed":
            already_reverted = (
                not app_dir.exists()
                and not asar_backup.exists()
                and sha256_file(asar) == ledger["hashes"]["asar"]
            )
            if already_reverted:
                result["note"] = "apply was reverted; rollback will record the finished state"
            else:
                conflicts.append("app.asar exists although apply did not complete")
    else:
        conflicts.append("unknown ledger status")
        result["status"] = "modified"

    if result["status"] in ("applied", "prepared") and conflicts:
        result["status"] = "modified"
    result["ok"] = result["status"] == "applied" and not conflicts
    return result


# ---------------------------------------------------------------------------
# rollback


def _release_state_artifacts(state_dir: Path) -> list[str]:
    """Drop this install's private derived state once the tree is gone.

    The ledger itself is kept for audit; the host shim state, the control file
    with its token and the runtime ownership/discovery files are not.
    """
    removed: list[str] = []
    host_state = state_dir / "host"
    if host_state.exists():
        shutil.rmtree(host_state, ignore_errors=True)
        if not host_state.exists():
            removed.append(str(host_state))
    for name in ("control.json", "host-discovery.json", "native-ownership.json"):
        path = state_dir / name
        if path.exists():
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


def rollback(target: Path | None, state_dir: Path, dry_run: bool = False, probe=None) -> dict:
    state_dir = state_dir.expanduser().resolve()
    ledger = load_ledger(state_dir)
    if ledger is None:
        raise NativeInstallError("no installation recorded; nothing to roll back")
    recorded_target = Path(ledger["target"])
    if target is not None:
        _require_same_target(ledger, target.expanduser().resolve())
    root = recorded_target
    if not root.is_dir():
        raise NativeInstallError(f"recorded target no longer exists: {root}")

    app_dir = root / APP_REL
    asar = root / ASAR_REL
    asar_backup = root / ASAR_BACKUP_REL
    engine = root / ENGINE_REL
    staging = Path(ledger["paths"]["staging"])
    _require_within(staging, root, "staging directory")
    if staging.name != STAGING_DIRNAME:
        raise NativeInstallError("ledger staging path is not the owned directory name")

    current = ledger.get("status")
    report = {
        "ok": True,
        "command": "rollback",
        "dryRun": dry_run,
        "target": str(root),
        "recordedStatus": current,
        "engineWritten": False,
    }

    if current == "prepared":
        # Discard a preparation that was never applied.
        if app_dir.exists():
            raise NativeInstallError(f"{APP_REL} exists although the ledger is only prepared; refusing")
        if asar_backup.exists():
            raise NativeInstallError(f"{ASAR_BACKUP_REL} exists although the ledger is only prepared; refusing")
        if dry_run:
            report.update({
                "status": "prepared",
                "wouldDiscard": True,
                "staging": str(staging),
                "stagingExists": staging.exists(),
            })
            report["engine"] = _engine_fact(engine)
            return report
        if staging.exists():
            _require_within(staging, root, "staging directory")
            shutil.rmtree(staging)
        report["releasedState"] = _release_state_artifacts(state_dir)
        report.update({"status": "rolled_back", "discarded": True})
        ledger.update({"status": "rolled_back", "phase": "discarded", "rolledBackAt": _now()})
        _save_ledger(state_dir, ledger)
        report["engine"] = _engine_fact(engine)
        return report

    if current not in ("applied", "partial"):
        raise NativeInstallError(f"installation is {current}; nothing to roll back")

    # An asar on its own is the recorded original again: that is an
    # already-reverted (compensated or interrupted) state, handled below.
    # Only an asar next to our expanded tree or our backup is ambiguous.
    if asar.exists() and (app_dir.exists() or asar_backup.exists()):
        raise NativeInstallError(
            "resources/app.asar exists again next to this installation's files "
            "(auto-update); refusing to roll back over the updater"
        )

    # Files of a running Desktop cannot be moved or deleted on Windows.
    if app_dir.exists():
        running_info = processes_from_target(root, probe)
        report["running"] = running_info
        if running_info["running"] and not dry_run:
            pids = ", ".join(str(item["pid"]) for item in running_info["running"])
            raise NativeInstallError(
                f"ZCode is running from the target (pid {pids}); close it normally before rollback"
            )

    # Verify the expanded tree byte-for-byte before touching it.
    if app_dir.exists():
        _require_within(app_dir, root, "expanded application tree")
        tree = tree_facts(app_dir)
        report["tree"] = tree
        if tree["digest"] != ledger["tree"]["digest"]:
            raise NativeInstallError(
                "expanded tree changed after installation; refusing to overwrite it. "
                "Review the differences and restore manually."
            )
        main = app_dir / MAIN_REL
        if not main.is_file() or BOOTSTRAP_MARKER not in main.read_text(encoding="utf-8", errors="replace"):
            raise NativeInstallError("expanded tree does not carry this tool's bootstrap; refusing")
    if asar_backup.exists():
        _require_within(asar_backup, root, "app.asar.original")
        backup_sha = sha256_file(asar_backup)
        report["asarOriginal"] = {"sha256": backup_sha}
        if backup_sha != ledger["hashes"]["asar"]:
            raise NativeInstallError(
                "resources/app.asar.original does not match the recorded original; refusing"
            )
    if not app_dir.exists() and not asar_backup.exists():
        if not asar.is_file() or sha256_file(asar) != ledger["hashes"]["asar"]:
            raise NativeInstallError(
                "neither resources/app nor resources/app.asar.original exists and app.asar "
                "is not the recorded original; manual repair is required"
            )
        report.update({"status": "rolled_back", "alreadyReverted": True})
        if not dry_run:
            if staging.exists():
                _require_within(staging, root, "staging directory")
                shutil.rmtree(staging, ignore_errors=True)
                report["stagingCleanup"] = "ok" if not staging.exists() else "failed"
            report["releasedState"] = _release_state_artifacts(state_dir)
            ledger.update({"status": "rolled_back", "phase": "already-reverted", "rolledBackAt": _now()})
            _save_ledger(state_dir, ledger)
        report["engine"] = _engine_fact(engine)
        return report

    if dry_run:
        report.update({"status": "applied", "wouldRestore": True})
        return report

    ledger.update({"status": "partial", "phase": "rollback-in-progress"})
    _save_ledger(state_dir, ledger)
    trash = staging / ("rollback-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()))
    moved = False
    try:
        if app_dir.exists():
            trash.mkdir(parents=True, exist_ok=False)
            os.rename(app_dir, trash / "app")
            moved = True
        if asar_backup.exists():
            os.rename(asar_backup, asar)
            if sha256_file(asar) != ledger["hashes"]["asar"]:
                raise NativeInstallError("restored app.asar verification failed")
        if sha256_file(asar) != ledger["hashes"]["asar"]:
            raise NativeInstallError("app.asar is not the recorded original after rollback")
        if app_dir.exists():
            raise NativeInstallError("resources/app still exists after rollback")
    except Exception as error:
        ledger.update({"status": "partial", "phase": "rollback-failed", "failure": f"{type(error).__name__}: {error}"})
        _save_ledger(state_dir, ledger)
        raise NativeInstallError(
            f"rollback failed ({error}); installation recorded as partial"
        ) from error

    # The moved tree was verified against the recorded digest before the move;
    # deletion is limited to this tool's own staging directory.
    cleanup = "nothing-to-remove"
    if moved:
        _require_within(trash, staging, "rollback trash directory")
        shutil.rmtree(trash, ignore_errors=True)
        cleanup = "ok" if not trash.exists() else "failed"

    # A prepared-but-never-applied staging tree can survive a partial apply.
    # Remove it only when it still matches the digest recorded at prepare.
    leftover = staging / "app"
    if leftover.is_dir():
        try:
            if tree_facts(leftover)["digest"] == ledger["tree"]["digest"]:
                _require_within(staging, root, "staging directory")
                shutil.rmtree(staging, ignore_errors=True)
                cleanup = "ok" if not staging.exists() else "staging-cleanup-failed"
            else:
                cleanup = "staging-retained-digest-mismatch"
        except OSError:
            cleanup = "staging-cleanup-failed"
    elif staging.exists():
        try:
            staging.rmdir()
        except OSError:
            pass

    report["releasedState"] = _release_state_artifacts(state_dir)
    report["stagingCleanup"] = cleanup
    ledger.update({
        "status": "rolled_back",
        "phase": "rolled-back",
        "failure": None,
        "stagingCleanup": cleanup,
        "rolledBackAt": _now(),
    })
    _save_ledger(state_dir, ledger)
    report.update({"status": "rolled_back", "ledger": str(ledger_path(state_dir))})
    report["engine"] = _engine_fact(engine)
    return report


def _engine_fact(engine: Path) -> dict:
    sha = sha256_file(engine) if engine.is_file() else ""
    return {"sha256": sha, "original": sha == ENGINE_SHA256, "writtenByThisTool": False}


# ---------------------------------------------------------------------------
# CLI


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    preflight_parser = commands.add_parser("preflight", help="read-only check of the installed tree")
    preflight_parser.add_argument("--target", type=Path, required=True)
    preflight_parser.add_argument("--dry-run", action="store_true", help="no-op; preflight never writes")

    prepare_parser = commands.add_parser("prepare", help="build the staged replacement tree")
    prepare_parser.add_argument("--target", type=Path, required=True)
    prepare_parser.add_argument("--workspace", action="append", default=[])
    prepare_parser.add_argument("--dry-run", action="store_true")

    apply_parser = commands.add_parser("apply", help="atomically switch the prepared installation in")
    apply_parser.add_argument("--target", type=Path, required=True)
    apply_parser.add_argument("--dry-run", action="store_true")

    status_parser = commands.add_parser("status", help="compare the ledger with the install")
    status_parser.add_argument("--target", type=Path)
    status_parser.add_argument("--dry-run", action="store_true", help="no-op; status never writes")

    rollback_parser = commands.add_parser("rollback", help="restore the recorded original files")
    rollback_parser.add_argument("--target", type=Path)
    rollback_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight(args.target, args.state_dir)
        elif args.command == "prepare":
            result = prepare(args.target, args.state_dir, args.workspace, args.dry_run)
        elif args.command == "apply":
            result = apply(args.target, args.state_dir, args.dry_run)
        elif args.command == "status":
            result = status(args.target, args.state_dir)
        elif args.command == "rollback":
            result = rollback(args.target, args.state_dir, args.dry_run)
        else:  # pragma: no cover - argparse enforces the set
            raise NativeInstallError(f"unknown command {args.command}")
    except (NativeInstallError, shim.ShimError, copy_tool.CopyError, OSError) as error:
        _print({"ok": False, "command": args.command, "error": str(error)})
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
