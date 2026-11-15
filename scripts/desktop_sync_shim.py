#!/usr/bin/env python3
"""Optional, reversible JS-layer sync shim for ZCode Desktop 3.18.2.

This tool never changes a default installation. It operates only on an explicit
application copy that the caller names, after a version and SHA-256 check, and it
records a byte-exact backup before writing anything.

The shim is appended to ``out/host/index.js`` in the copy. It exposes a narrow
loopback control endpoint with two default operations for explicitly allowed
workspaces, plus a separately disabled-by-default native-session transport:

* ``probe`` - readiness and recently observed workspace change events.
* ``sync``  - ask the host's own engine client to re-read persisted CLI session
  facts and apply them through the application's existing TaskIndexRepo path,
  then emit the same in-host ``workspace_task_list_changed`` event the normal
  sessions-index mirror emits.

The optional native transport only calls fixed public host methods for sessions
it created, with explicit caller permissions and persistent runtime ownership.

No eval, no shell, no arbitrary protocol injection and no generic command
runner is added. Connection details live in a private state directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM_SOURCE = ROOT / "runtime/desktop_sync/shim.mjs"
NATIVE_SOURCE = ROOT / "runtime/desktop_sync/native_transport.mjs"
NATIVE_MARKER = "ZCODE_DESKTOP_NATIVE_TRANSPORT_V1"
SHIM_MARKER = "ZCODE_DESKTOP_SYNC_SHIM_V1"
TARGET = "out/host/index.js"

# Known-good 3.18.2 host bundle. Any other version must be supplied through an
# explicit manifest so a user cannot silently patch an unverified build.
SUPPORTED = {
    "3.18.2": {
        TARGET: "e6346359c9b659b7f33595565cde04b1badaca02babeee6d46f4bdbc0639f105",
    },
}

ENGINE_TARGET = "resources/glm/zcode.cjs"
SUPPORTED_ENGINE = {
    "3.18.2": "08726b5ef69e1e9072828baa4656713cb3aac93bb096938681278c5600312b2c",
}
ENGINE_ANCHOR = (
    "async reconcileSessions(t){let n=Sat.parse(t);if(!this.host.reconcileSessions)"
    'throw new Error("sessionsIndex/reconcile is unavailable");'
    "return wat.parse(await this.host.reconcileSessions(n))}"
)
ENGINE_REPLACEMENT = (
    "async reconcileSessions(t){let n=Sat.parse(t);if(!this.host.reconcileSessions)"
    'throw new Error("sessionsIndex/reconcile is unavailable");'
    "let r=wat.parse(await this.host.reconcileSessions(n));"
    "for(let s of n.sessionIds){"
    "if(![...this.flushStates.values()].some(v=>v.sessionId===s))continue;"
    "try{await this.hydratePublisher(s,void 0,!0)}catch{}}"
    "return r}"
)

ANCHOR_EXPOSE = "let Xi=fpe({agentService:Et,taskIndexRepo:T}),"
REPLACEMENT_EXPOSE = (
    "let Xi=fpe({agentService:Et,taskIndexRepo:T}),"
    "__zcodeSyncBind=(globalThis.__zcodeDesktopSync=Xi),"
    "__zcodeNativeBind=(globalThis.__zcodeDesktopNativeCreate=O=>createZcodeDesktopNativeTransport({"
    "...O,makeEnvelope:ti,syncer:()=>Xi,agent:Object.freeze(Object.fromEntries(["
    '"createSession","closeSession","getWorkspaceRuntimeIdentity","getSessionLifecycle","readSession",'
    '"readSessionMessages","readSessionEvents","listSessionSubagents","listSessions","setModel",'
    '"setThoughtLevel","setMode","sendPrompt","sendConversationCommandV4",'
    '"conversationWorkflowRunsV4","conversationWorkflowRunEventsV4","reconcileSessionsIndexV4"'
    "].map(M=>[M,typeof Et[M]===\"function\"?Et[M].bind(Et):void 0]))) })),"
)
ANCHOR_API = "mutateSessionFacts:ie,"
REPLACEMENT_API = (
    "mutateSessionFacts:ie,"
    "syncSessionFacts:O=>(async()=>{"
    "let g={workspacePath:O.workspacePath,...O.workspaceIdentity?{workspaceIdentity:O.workspaceIdentity}:{},taskId:O.sessionId};"
    "let b=await n.getTaskMeta(g);"
    "let r=await t.reconcileSessionsIndexV4({...O,sessionIds:[O.sessionId]});"
    "let f=(r&&r.sessions||[])[0];"
    "if(!f||!f.exists)return{changed:!1,meta:b,facts:(r&&r.sessions)||[],missing:!0};"
    "if(f.deleted){await j.removed(g);return{changed:!!b,meta:await n.getTaskMeta(g),facts:r.sessions,deleted:!0};}"
    "let s={sessionId:f.sessionId,title:f.title,titleSource:f.titleSource,createdAt:f.createdAt,"
    "lastActivityAt:f.updatedAt,...f.lastOutcome?{lastOutcome:f.lastOutcome}:{},"
    "...f.parentSessionId?{parentSessionId:f.parentSessionId}:{}};"
    "let m=await j.ingest(g,s);"
    "let ch=!b||b.title!==m.title||b.updatedAt!==m.updatedAt||b.status!==m.status;"
    'if(ch)K({...g},m,b?"task_title_changed":"task_created");'
    "return{changed:ch,meta:m,facts:r.sessions};"
    "})(),"
)


class ShimError(RuntimeError):
    """A check or state transition refused to continue."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def default_state_dir() -> Path:
    override = os.environ.get("ZCODE_DESKTOP_SYNC_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local/share")
    return Path(base) / "ZCodeDesktopSyncShim"


def load_manifest(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ShimError(f"manifest is not readable JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise ShimError("manifest must contain desktopVersion and targets")
    if not isinstance(data.get("desktopVersion"), str):
        raise ShimError("manifest.desktopVersion must be a string")
    targets = data["targets"]
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in targets.items()):
        raise ShimError("manifest.targets must map paths to SHA-256 strings")
    return data


def expected_hashes(version: str, manifest: dict | None) -> dict[str, str]:
    if manifest is not None:
        if manifest["desktopVersion"] != version:
            raise ShimError(
                f"manifest version {manifest['desktopVersion']} does not match application version {version}"
            )
        if version in SUPPORTED and dict(manifest["targets"]) != SUPPORTED[version]:
            raise ShimError(
                f"manifest hashes conflict with the built-in hashes for Desktop {version}; refusing to enable"
            )
        return dict(manifest["targets"])
    if version not in SUPPORTED:
        raise ShimError(
            f"unsupported Desktop version {version}; only {', '.join(sorted(SUPPORTED))} is built in. "
            "Supply a reviewed manifest with --manifest for another build."
        )
    return dict(SUPPORTED[version])


@dataclass(frozen=True)
class Plan:
    version: str
    target: Path
    original: bytes
    patched: bytes
    original_sha256: str
    patched_sha256: str
    expected_sha256: str


def read_version(app_root: Path) -> str:
    package = app_root / "package.json"
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ShimError(f"application package.json is not readable: {error}") from error
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ShimError("application package.json has no version string")
    return version


def verify(app_root: Path, manifest: dict | None = None) -> tuple[str, dict[str, str]]:
    app_root = app_root.resolve()
    version = read_version(app_root)
    expected = expected_hashes(version, manifest)
    if TARGET not in expected:
        raise ShimError(f"manifest does not list {TARGET}")
    target = (app_root / TARGET).resolve()
    try:
        relative = target.relative_to(app_root)
    except ValueError as error:
        raise ShimError("target escapes the application copy") from error
    if relative.as_posix() != TARGET or not target.is_file():
        raise ShimError(f"missing target file: {TARGET}")
    actual = sha256_file(target)
    if actual != expected[TARGET]:
        raise ShimError(
            f"SHA-256 mismatch for {TARGET}: expected {expected[TARGET]}, found {actual}; refusing to enable"
        )
    return version, expected


def verify_engine(path: Path, expected_sha256: str) -> str:
    try:
        actual = sha256_file(path)
    except OSError as error:
        raise ShimError(f"engine file is not readable: {error}") from error
    if actual != expected_sha256:
        raise ShimError(
            f"SHA-256 mismatch for {path.name}: expected {expected_sha256}, found {actual}; refusing to enable"
        )
    return actual


def engine_plan(path: Path, expected_sha256: str) -> Plan:
    target = path.resolve()
    original = target.read_bytes()
    if sha256_bytes(original) != expected_sha256:
        raise ShimError(
            f"SHA-256 mismatch for {target.name}: expected {expected_sha256}, "
            f"found {sha256_bytes(original)}; refusing to enable"
        )
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ShimError("engine bundle is not valid UTF-8") from error
    if text.count(ENGINE_ANCHOR) != 1:
        raise ShimError("engine reconcile anchor not found exactly once; refusing to patch")
    patched = text.replace(ENGINE_ANCHOR, ENGINE_REPLACEMENT, 1).encode("utf-8")
    if patched.count(b"hydratePublisher(s,void 0,!0)") != 1:
        raise ShimError("engine patch verification failed")
    return Plan(
        version="",
        target=target,
        original=original,
        patched=patched,
        original_sha256=sha256_bytes(original),
        patched_sha256=sha256_bytes(patched),
        expected_sha256=expected_sha256,
    )


def make_plan(app_root: Path, manifest: dict | None = None) -> Plan:
    app_root = app_root.resolve()
    if (app_root / "resources" / "app.asar").is_file():
        raise ShimError(
            "app-copy still contains resources/app.asar; extract the application first. "
            "This tool refuses to patch an installed or packaged third-party application"
        )
    target = app_root / TARGET
    if target.is_file():
        try:
            current = target.read_bytes()
        except OSError as error:
            raise ShimError(f"target is not readable: {error}") from error
        if SHIM_MARKER.encode("utf-8") in current:
            raise ShimError("shim marker already present; restore before reinstalling")
    version, expected = verify(app_root, manifest)
    original = target.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ShimError("host bundle is not valid UTF-8") from error
    if SHIM_MARKER in text:
        raise ShimError("shim marker already present; restore before reinstalling")
    if text.count(ANCHOR_EXPOSE) != 1:
        raise ShimError("expose anchor not found exactly once; refusing to patch")
    if text.count(ANCHOR_API) != 1:
        raise ShimError("API anchor not found exactly once; refusing to patch")
    try:
        snippet = SHIM_SOURCE.read_text(encoding="utf-8")
        native = NATIVE_SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        raise ShimError(f"shim source is missing: {error}") from error
    if SHIM_MARKER not in snippet:
        raise ShimError("shim source is missing its version marker")
    if snippet.count(SHIM_MARKER) != 1:
        raise ShimError("shim source must contain exactly one version marker")
    if native.count(NATIVE_MARKER) != 1 or native.count("export function createZcodeDesktopNativeTransport(") != 1:
        raise ShimError("native transport source marker or factory is not unique")
    native = native.replace("export function createZcodeDesktopNativeTransport(", "function createZcodeDesktopNativeTransport(", 1)
    patched_text = text.replace(ANCHOR_EXPOSE, REPLACEMENT_EXPOSE, 1)
    patched_text = patched_text.replace(ANCHOR_API, REPLACEMENT_API, 1)
    combined = patched_text + "\n" + native + "\n" + snippet
    if combined.count(SHIM_MARKER) != 1:
        raise ShimError("patched output does not contain exactly one shim marker")
    patched = combined.encode("utf-8")
    return Plan(
        version=version,
        target=target,
        original=original,
        patched=patched,
        original_sha256=sha256_bytes(original),
        patched_sha256=sha256_bytes(patched),
        expected_sha256=expected[TARGET],
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def state_path(state_dir: Path) -> Path:
    return state_dir / "shim-state.json"


def backup_path(state_dir: Path) -> Path:
    return state_dir / "backup" / TARGET


def install(
    app_root: Path,
    state_dir: Path,
    manifest: dict | None = None,
    dry_run: bool = False,
) -> dict:
    plan = make_plan(app_root, manifest)
    report = {
        "ok": True,
        "dryRun": dry_run,
        "desktopVersion": plan.version,
        "target": str(plan.target),
        "expectedSha256": plan.expected_sha256,
        "originalSha256": plan.original_sha256,
        "patchedSha256": plan.patched_sha256,
        "backup": str(backup_path(state_dir)),
    }
    if dry_run:
        return report
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_path(state_dir).exists():
        raise ShimError("state already exists; run restore or choose another state directory")
    backup = backup_path(state_dir)
    if backup.exists():
        raise ShimError("backup already exists but state is missing; refusing to overwrite")
    _atomic_write(backup, plan.original)
    if sha256_file(backup) != plan.original_sha256:
        raise ShimError("backup verification failed")
    _atomic_write(plan.target, plan.patched)
    if sha256_file(plan.target) != plan.patched_sha256:
        raise ShimError("patched file verification failed; restore from the backup")
    state = {
        "kind": "zcode-desktop-sync-shim",
        "formatVersion": 1,
        "desktopVersion": plan.version,
        "appRoot": str(Path(app_root).resolve()),
        "target": TARGET,
        "originalSha256": plan.original_sha256,
        "patchedSha256": plan.patched_sha256,
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "marker": SHIM_MARKER,
    }
    _atomic_write(state_path(state_dir), (json.dumps(state, indent=2) + "\n").encode("utf-8"))
    report["state"] = str(state_path(state_dir))
    return report


def load_state(state_dir: Path) -> dict:
    try:
        state = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ShimError(f"shim state is not readable: {error}") from error
    required = ("desktopVersion", "appRoot", "target", "originalSha256", "patchedSha256")
    if not all(isinstance(state.get(key), str) for key in required):
        raise ShimError("shim state is incomplete")
    return state


def restore(state_dir: Path, force: bool = False) -> dict:
    state = load_state(state_dir)
    app_root = Path(state["appRoot"]).resolve()
    target = (app_root / state["target"]).resolve()
    try:
        relative = target.relative_to(app_root)
    except ValueError as error:
        raise ShimError("state target escapes the recorded application copy") from error
    if relative.as_posix() != state["target"]:
        raise ShimError("state target escapes the recorded application copy")
    backup = backup_path(state_dir)
    if not backup.is_file():
        raise ShimError("backup file is missing")
    original = backup.read_bytes()
    if sha256_bytes(original) != state["originalSha256"]:
        raise ShimError("backup hash does not match the recorded original")
    current = target.read_bytes() if target.is_file() else b""
    current_sha = sha256_bytes(current)
    if current_sha != state["patchedSha256"] and not force:
        raise ShimError(
            "target changed after installation; re-run with --force after reviewing the file"
        )
    _atomic_write(target, original)
    if sha256_file(target) != state["originalSha256"]:
        raise ShimError("restored file hash does not match the recorded original")
    record = {
        "ok": True,
        "restored": True,
        "target": str(target),
        "originalSha256": state["originalSha256"],
        "replacedSha256": current_sha,
        "restoredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write(state_dir / "last-restore.json", (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    state_path(state_dir).unlink(missing_ok=True)
    backup.unlink(missing_ok=True)
    return record


def status(state_dir: Path) -> dict:
    state = load_state(state_dir)
    target = Path(state["appRoot"]).resolve() / state["target"]
    if not target.is_file():
        return {"ok": False, "error": "target_missing", **state}
    current = sha256_file(target)
    if current == state["patchedSha256"]:
        status_name = "patched"
    elif current == state["originalSha256"]:
        status_name = "original"
    else:
        status_name = "modified"
    return {"ok": status_name == "patched", "status": status_name, "currentSha256": current, **state}


def write_manifest(app_root: Path, destination: Path) -> dict:
    app_root = app_root.resolve()
    version = read_version(app_root)
    target = app_root / TARGET
    if not target.is_file():
        raise ShimError(f"missing target file: {TARGET}")
    manifest = {
        "desktopVersion": version,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "targets": {TARGET: sha256_file(target)},
    }
    _atomic_write(destination, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return manifest


def canonical_workspace(value: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ShimError(f"workspace does not exist: {path}")
    return str(path)


def configure(state_dir: Path, workspaces: list[str], port: int = 0) -> dict:
    state_dir = state_dir.expanduser().resolve()
    resolved = []
    for workspace in workspaces:
        path = canonical_workspace(workspace)
        if path not in resolved:
            resolved.append(path)
    if not resolved:
        raise ShimError("at least one allowed workspace is required")
    if port < 0 or port > 65535:
        raise ShimError("port must be between 0 and 65535")
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "host": "127.0.0.1",
        "port": port,
        "token": secrets.token_urlsafe(32),
        "discoveryPath": str(state_dir / "host-discovery.json"),
        "allowedWorkspaces": resolved,
        "nativeTransport": {"enabled": False, "ownershipPath": str(state_dir / "native-ownership.json")},
    }
    destination = state_dir / "control.json"
    _atomic_write(destination, (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    (state_dir / "host-discovery.json").unlink(missing_ok=True)
    return {
        "ok": True,
        "config": str(destination),
        "allowedWorkspaces": resolved,
        "port": port,
        "warning": "Set ZCODE_DESKTOP_SYNC_CONFIG to this file in the patched host process.",
    }


def _read_control_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ShimError(f"control config is not readable: {error}") from error
    if not isinstance(config.get("token"), str) or len(config["token"]) < 16:
        raise ShimError("control config token is missing or too short")
    return config


def configure_native(config_path: Path, enabled: bool) -> dict:
    """Change only private opt-in state; never start or restart Desktop."""
    config_path = config_path.expanduser().resolve()
    config = _read_control_config(config_path)
    previous = config.get("nativeTransport", {})
    ownership = previous.get("ownershipPath") if isinstance(previous, dict) else None
    config["nativeTransport"] = {"enabled": enabled, "ownershipPath": ownership or str(config_path.parent / "native-ownership.json")}
    _atomic_write(config_path, (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    return {"ok": True, "nativeEnabled": enabled, "config": str(config_path), "processStarted": False}


def control_call(config_path: Path, message: dict, timeout: float = 30.0) -> dict:
    config = _read_control_config(config_path)
    port = config.get("port")
    discovery = config.get("discoveryPath")
    if (not isinstance(port, int) or port <= 0) and isinstance(discovery, str) and discovery:
        try:
            port = int(json.loads(Path(discovery).read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ShimError(f"host discovery is not readable: {error}") from error
    if not isinstance(port, int) or not (0 < port < 65536):
        raise ShimError("no control port is configured or discovered")
    host = config.get("host", "127.0.0.1")
    if host != "127.0.0.1":
        raise ShimError("control host must be 127.0.0.1")
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        stream = connection.makefile("rwb", buffering=0)

        def exchange(payload: dict) -> dict:
            stream.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = stream.readline()
            if not line:
                raise ShimError("control connection closed before a response")
            try:
                return json.loads(line)
            except ValueError as error:
                raise ShimError("control response was not JSON") from error

        auth = exchange({"token": config["token"]})
        if not auth.get("ok"):
            raise ShimError(f"control authentication failed: {auth.get('error')}")
        return exchange(message)


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    manifest_parser = commands.add_parser("manifest", help="write a SHA-256 manifest for an application copy")
    manifest_parser.add_argument("--app-copy", type=Path, required=True)
    manifest_parser.add_argument("--out", type=Path, required=True)

    verify_parser = commands.add_parser("verify", help="check version and hashes without writing")
    verify_parser.add_argument("--app-copy", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path)

    install_parser = commands.add_parser("install", help="patch an explicit application copy (experimental)")
    install_parser.add_argument("--app-copy", type=Path, required=True)
    install_parser.add_argument("--manifest", type=Path)
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument(
        "--experimental",
        action="store_true",
        help="acknowledge that this modifies the named copy of a third-party application",
    )

    restore_parser = commands.add_parser("restore", help="byte-exactly restore the backed-up host bundle")
    restore_parser.add_argument("--force", action="store_true")

    commands.add_parser("status", help="compare the current file with the recorded state")

    configure_parser = commands.add_parser("configure", help="write a private control config")
    configure_parser.add_argument("--workspace", action="append", required=True)
    configure_parser.add_argument("--port", type=int, default=0)

    native_config_parser = commands.add_parser("native-config", help="enable/disable the private native transport opt-in")
    native_config_parser.add_argument("--config", type=Path, required=True)
    native_config_parser.add_argument("--mode", choices=("enabled", "disabled"), required=True)

    native_call_parser = commands.add_parser("native-call", help="call one fixed native operation using a private JSON request file")
    native_call_parser.add_argument("--config", type=Path, required=True)
    native_call_parser.add_argument("--request-file", type=Path, required=True)
    native_call_parser.add_argument("--timeout", type=float, default=60.0)

    probe_parser = commands.add_parser("probe", help="probe the running patched host")
    probe_parser.add_argument("--config", type=Path, required=True)

    sync_parser = commands.add_parser("sync", help="ask the running host to reconcile specific sessions")
    sync_parser.add_argument("--config", type=Path, required=True)
    sync_parser.add_argument("--workspace", required=True)
    sync_parser.add_argument("--session-id", action="append", default=[])

    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            _print({"ok": True, **write_manifest(args.app_copy, args.out)})
        elif args.command == "verify":
            version, expected = verify(args.app_copy, load_manifest(args.manifest))
            _print({"ok": True, "desktopVersion": version, "expected": expected})
        elif args.command == "install":
            if not args.dry_run and not args.experimental:
                raise ShimError("install modifies an application copy; re-run with --dry-run first, then --experimental")
            _print(install(args.app_copy, args.state_dir, load_manifest(args.manifest), args.dry_run))
        elif args.command == "restore":
            _print(restore(args.state_dir, args.force))
        elif args.command == "status":
            _print(status(args.state_dir))
        elif args.command == "configure":
            _print(configure(args.state_dir, args.workspace, args.port))
        elif args.command == "native-config":
            _print(configure_native(args.config, args.mode == "enabled"))
        elif args.command == "native-call":
            try:
                message = json.loads(args.request_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ShimError(f"native request is not readable JSON: {error}") from error
            allowed = {"native_create", "native_send", "native_snapshot", "native_messages", "native_events",
                       "native_resolve", "native_workflows", "native_workflow_events", "native_cancel",
                       "native_pending", "native_model_catalog", "native_children", "native_topology", "native_lifecycle",
                       "native_child_messages", "native_child_snapshot"}
            if not isinstance(message, dict) or message.get("op") not in allowed:
                raise ShimError("native request operation is not supported")
            if not isinstance(message.get("workspace"), str) or not message["workspace"].strip():
                raise ShimError("native request requires an explicit workspace")
            message["workspace"] = canonical_workspace(message["workspace"])
            _print(control_call(args.config, message, timeout=args.timeout))
        elif args.command == "probe":
            _print(control_call(args.config, {"op": "probe"}))
        elif args.command == "sync":
            workspace = canonical_workspace(args.workspace)
            _print(control_call(args.config, {
                "op": "sync",
                "workspace": workspace,
                "sessionIds": args.session_id,
            }))
        else:  # pragma: no cover - argparse enforces the set
            raise ShimError(f"unknown command {args.command}")
    except ShimError as error:
        _print({"ok": False, "error": str(error)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
