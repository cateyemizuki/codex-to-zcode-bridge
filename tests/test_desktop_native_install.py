"""Tests for the explicit original-install entry point.

Fixtures are real-format ASAR archives built in temp directories (the same
header/pickle layout Electron uses), never mocks of the installer's work.

The only substitutions are:

* the OS process probe (``PROCESS_PROBE`` / the ``probe`` argument), because
  the host platform cannot be made to run a fake ZCode; and
* the built-in reviewed hash constants, which are re-pinned to the generated
  fixture bytes so ``preflight``/``prepare``/``apply``/``rollback`` execute
  their real code paths against those exact bytes.

No installer function is replaced, and no return value is fabricated: every
assertion below inspects files produced by a real prepare/apply/rollback run.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shim = _load("desktop_sync_shim", ROOT / "scripts" / "desktop_sync_shim.py")
copy_tool = _load("desktop_sync_copy", ROOT / "scripts" / "desktop_sync_copy.py")
tool = _load("desktop_native_install", ROOT / "scripts" / "desktop_native_install.py")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    handle = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            handle.update(block)
    return handle.hexdigest()


def make_asar(files, unpacked_names=()):
    """Build a real-format ASAR: 16-byte pickle prefix, JSON header, data area."""
    tree = {"files": {}}
    for name, data in files.items():
        parts = name.split("/")
        node = tree
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        leaf = {"size": len(data), "offset": "000000000000"}
        if name in unpacked_names:
            leaf["unpacked"] = True
        node["files"][parts[-1]] = leaf
    provisional = json.dumps(tree, separators=(",", ":")).encode()
    header_size = len(provisional)
    base = 16 + header_size + ((4 - header_size % 4) % 4)
    offset = 0
    for name, data in files.items():
        node = tree
        for part in name.split("/"):
            node = node["files"][part]
        node["offset"] = f"{offset:012d}"
        offset += len(data)
    header = json.dumps(tree, separators=(",", ":")).encode()
    assert len(header) == header_size
    prefix = struct.pack("<IIII", 4, header_size + 9, header_size + 5, header_size)
    return prefix + header + b"\0" * (base - 16 - header_size) + b"".join(files.values())


ANCHOR_EXPOSE = shim.ANCHOR_EXPOSE.encode()
ANCHOR_API = shim.ANCHOR_API.encode()

HOST = (
    "let Xi=fpe({agentService:Et,taskIndexRepo:T}),_i=xx({}),I=1;\n"
    "function f(){return{mutateSessionFacts:ie,other:1};}\n"
).encode()

# The fixture "original main entry". It records the environment it sees so the
# bootstrap test can prove where ZCODE_DESKTOP_SYNC_CONFIG came from and that
# no user/session/home path was set by the bootstrap.
MAIN_PROBE = b"""import { writeFileSync } from 'node:fs';

const out = process.env.ZCODE_BOOTSTRAP_PROBE_OUT;
const names = [
  'ZCODE_DESKTOP_SYNC_CONFIG',
  'ZCODE_DESKTOP_USER_DATA_DIR',
  'ZCODE_DESKTOP_SESSION_DATA_DIR',
  'HOME',
  'USERPROFILE',
];
const seen = {};
for (const name of names) {
  seen[name] = Object.prototype.hasOwnProperty.call(process.env, name) ? process.env[name] : null;
}
writeFileSync(out, JSON.stringify(seen));
"""

ENGINE = b"// fixture engine bundle\n"
EXE = b"MZ-fixture-executable"
UNPACKED = "node_modules/demo/native.node"


def package_json(version="3.18.2"):
    return json.dumps(
        {
            "name": "@zcode/desktop",
            "version": version,
            "type": "module",
            "main": "out/main/index.js",
        },
        indent=2,
    ).encode()


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zcode-native-install-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.target = self.tmp / "installed-zcode"
        self.state = self.tmp / "state"
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self._saved = {
            name: getattr(tool, name)
            for name in (
                "ASAR_BYTES",
                "ASAR_SHA256",
                "HOST_SHA256",
                "MAIN_SHA256",
                "ENGINE_SHA256",
                "EXE_SHA256",
                "PROCESS_PROBE",
            )
        }
        self._saved_supported = (dict(shim.SUPPORTED), dict(shim.SUPPORTED_ENGINE))
        self.addCleanup(self._restore)
        tool.PROCESS_PROBE = lambda: []
        self.files = {}
        self.build()
        self.pin()

    def _restore(self):
        for name, value in self._saved.items():
            setattr(tool, name, value)
        shim.SUPPORTED = self._saved_supported[0]
        shim.SUPPORTED_ENGINE = self._saved_supported[1]

    # -- fixture -----------------------------------------------------------
    def build(self, version="3.18.2", host=HOST, main=MAIN_PROBE, engine=ENGINE, exe=EXE):
        resources = self.target / "resources"
        (resources / "glm").mkdir(parents=True, exist_ok=True)
        files = {
            "package.json": package_json(version),
            "out/host/index.js": host,
            "out/main/index.js": main,
            "out/main/chunk.js": b"export const chunk = 1;\n",
            UNPACKED: b"\x7fELFfixture",
        }
        asar = make_asar(files, {UNPACKED})
        (resources / "app.asar").write_bytes(asar)
        unpacked = resources / "app.asar.unpacked" / Path(UNPACKED).parent
        unpacked.mkdir(parents=True, exist_ok=True)
        (unpacked / Path(UNPACKED).name).write_bytes(files[UNPACKED])
        (resources / "glm" / "zcode.cjs").write_bytes(engine)
        (self.target / "ZCode.exe").write_bytes(exe)
        (self.target / ".zcode-install-manifest").write_bytes(
            b"ZCode.exe\r\nresources\\app.asar\r\n"
        )
        self.files = files
        return files

    def pin(self, host_for_hash=None, main_for_hash=None, engine_for_hash=None, exe_for_hash=None):
        asar = (self.target / "resources" / "app.asar").read_bytes()
        tool.ASAR_BYTES = len(asar)
        tool.ASAR_SHA256 = sha(asar)
        tool.HOST_SHA256 = sha(host_for_hash if host_for_hash is not None else self.files["out/host/index.js"])
        tool.MAIN_SHA256 = sha(main_for_hash if main_for_hash is not None else self.files["out/main/index.js"])
        tool.ENGINE_SHA256 = sha(engine_for_hash if engine_for_hash is not None else (self.target / "resources" / "glm" / "zcode.cjs").read_bytes())
        tool.EXE_SHA256 = sha(exe_for_hash if exe_for_hash is not None else (self.target / "ZCode.exe").read_bytes())
        shim.SUPPORTED = {"3.18.2": {shim.TARGET: tool.HOST_SHA256}}
        shim.SUPPORTED_ENGINE = {"3.18.2": tool.ENGINE_SHA256}

    # -- paths -------------------------------------------------------------
    @property
    def asar(self):
        return self.target / "resources" / "app.asar"

    @property
    def asar_backup(self):
        return self.target / "resources" / "app.asar.original"

    @property
    def app_dir(self):
        return self.target / "resources" / "app"

    @property
    def staging(self):
        return self.target / tool.STAGING_DIRNAME

    @property
    def engine(self):
        return self.target / "resources" / "glm" / "zcode.cjs"

    @property
    def exe(self):
        return self.target / "ZCode.exe"

    @property
    def ledger_path(self):
        return self.state / tool.LEDGER_FILENAME

    def ledger(self):
        return json.loads(self.ledger_path.read_text(encoding="utf-8"))

    def control(self):
        return json.loads((self.state / "control.json").read_text(encoding="utf-8"))

    def prepare(self):
        return tool.prepare(self.target, self.state, [str(self.workspace)])

    def apply(self):
        return tool.apply(self.target, self.state, probe=lambda: [])

    def rollback(self):
        return tool.rollback(self.target, self.state, probe=lambda: [])

    def snapshot(self, root):
        snapshot = {}
        root = Path(root)
        for base, _dirs, files in os.walk(root):
            for name in files:
                path = Path(base) / name
                snapshot[path.relative_to(root).as_posix()] = sha(path.read_bytes())
        return snapshot


class RoundTripTests(FixtureCase):
    def test_preflight_then_full_roundtrip_restores_bytes(self):
        engine_before = self.engine.read_bytes()
        exe_before = self.exe.read_bytes()
        asar_before = self.asar.read_bytes()

        pre = tool.preflight(self.target, self.state)
        self.assertTrue(pre["ok"])
        self.assertEqual(pre["desktopVersion"], "3.18.2")
        self.assertEqual(pre["host"]["anchors"], {"expose": 1, "api": 1})
        self.assertFalse(pre["running"]["running"])

        target_before = self.snapshot(self.target)
        dry = tool.prepare(self.target, self.state, [str(self.workspace)], dry_run=True)
        self.assertTrue(dry["dryRun"])
        self.assertFalse(self.staging.exists(), "prepare --dry-run created staging")
        self.assertFalse(self.state.exists(), "prepare --dry-run created state")
        self.assertEqual(self.snapshot(self.target), target_before, "prepare --dry-run changed target")

        prepared = self.prepare()
        self.assertEqual(prepared["treeFiles"], len(self.snapshot(self.staging / "app")))
        self.assertEqual(self.asar.read_bytes(), asar_before, "prepare changed app.asar")
        self.assertEqual(self.engine.read_bytes(), engine_before)
        staged_main = self.staging / "app" / "out/main/index.js"
        staged_original = self.staging / "app" / "out/main/index.native-original.js"
        self.assertEqual(staged_main.read_bytes(), tool._bootstrap_bytes())
        self.assertEqual(staged_original.read_bytes(), self.files["out/main/index.js"])
        pointer = json.loads((self.staging / "app" / tool.POINTER_REL).read_text(encoding="utf-8"))
        self.assertTrue(Path(pointer["controlPath"]).is_absolute())
        self.assertEqual(Path(pointer["controlPath"]), (self.state / "control.json").resolve())
        self.assertEqual(self.ledger()["status"], "prepared")
        self.assertEqual(tool.status(self.target, self.state)["status"], "prepared")

        applied = self.apply()
        self.assertEqual(applied["status"], "applied")
        self.assertFalse(self.asar.exists())
        self.assertEqual(self.asar_backup.read_bytes(), asar_before)
        self.assertEqual(sha_file(self.asar_backup), sha(asar_before))
        self.assertTrue((self.app_dir / "out/host/index.js").is_file())
        self.assertEqual(self.engine.read_bytes(), engine_before, "apply changed engine")
        self.assertEqual(self.exe.read_bytes(), exe_before, "apply changed ZCode.exe")
        status = tool.status(self.target, self.state)
        self.assertEqual(status["status"], "applied")
        self.assertTrue(status["ok"])
        self.assertEqual(status["conflicts"], [])
        self.assertTrue(status["engine"]["original"])

        rolled = self.rollback()
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual(self.asar.read_bytes(), asar_before, "rollback did not restore app.asar bytes")
        self.assertEqual(sha_file(self.asar), sha(asar_before))
        self.assertEqual(self.exe.read_bytes(), exe_before)
        self.assertEqual(self.engine.read_bytes(), engine_before, "rollback changed engine")
        self.assertFalse(self.app_dir.exists())
        self.assertFalse(self.asar_backup.exists())
        self.assertFalse(self.staging.exists())
        self.assertEqual(self.ledger()["status"], "rolled_back")
        self.assertEqual(tool.status(self.target, self.state)["status"], "rolled_back")

    def test_prepared_rollback_discards_without_touching_target(self):
        asar_before = self.asar.read_bytes()
        self.prepare()
        self.assertTrue(self.staging.exists())
        rolled = self.rollback()
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertTrue(rolled.get("discarded"))
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.state.exists() and (self.state / "control.json").exists())
        self.assertEqual(self.asar.read_bytes(), asar_before)
        self.assertFalse(self.asar_backup.exists())
        self.assertEqual(self.ledger()["status"], "rolled_back")


class BootstrapTests(FixtureCase):
    def test_bootstrap_reads_pointer_and_does_not_set_profile_paths(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        self.prepare()
        staged_main = self.staging / "app" / "out/main/index.js"
        self.assertTrue(staged_main.is_file())

        source = staged_main.read_text(encoding="utf-8")
        self.assertNotIn("'electron'", source)
        self.assertNotIn('"electron"', source)
        self.assertNotIn("setPath(", source)
        self.assertNotIn("ZCodeDesktopSyncShim", source)
        self.assertNotIn("ZCODE_DESKTOP_USER_DATA_DIR", source)
        self.assertNotIn("ZCODE_DESKTOP_SESSION_DATA_DIR", source)

        base_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "ZCODE_DESKTOP_SYNC_CONFIG",
                "ZCODE_DESKTOP_USER_DATA_DIR",
                "ZCODE_DESKTOP_SESSION_DATA_DIR",
            )
        }
        probe_out = self.tmp / "env-from-pointer.json"
        env = dict(base_env, ZCODE_BOOTSTRAP_PROBE_OUT=str(probe_out))
        completed = self._run_node(node, staged_main, env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seen = json.loads(probe_out.read_text(encoding="utf-8"))
        pointer = json.loads((self.staging / "app" / tool.POINTER_REL).read_text(encoding="utf-8"))
        self.assertTrue(Path(pointer["controlPath"]).is_absolute())
        self.assertEqual(seen["ZCODE_DESKTOP_SYNC_CONFIG"], pointer["controlPath"])
        self.assertIsNone(seen["ZCODE_DESKTOP_USER_DATA_DIR"])
        self.assertIsNone(seen["ZCODE_DESKTOP_SESSION_DATA_DIR"])
        self.assertEqual(seen["HOME"], os.environ.get("HOME"))
        self.assertEqual(seen["USERPROFILE"], os.environ.get("USERPROFILE"))

        # The installed pointer is authoritative: an inherited value from
        # another install must not win and must not survive.
        probe_out_2 = self.tmp / "env-preset.json"
        env2 = dict(
            base_env,
            ZCODE_BOOTSTRAP_PROBE_OUT=str(probe_out_2),
            ZCODE_DESKTOP_SYNC_CONFIG="/foreign-install/control.json",
        )
        completed = self._run_node(node, staged_main, env2)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seen2 = json.loads(probe_out_2.read_text(encoding="utf-8"))
        self.assertEqual(seen2["ZCODE_DESKTOP_SYNC_CONFIG"], pointer["controlPath"])

    def test_bootstrap_without_pointer_still_loads_original(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        self.prepare()
        staged = self.staging / "app"
        (staged / tool.POINTER_REL).unlink()
        probe_out = self.tmp / "env-without-pointer.json"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("ZCODE_DESKTOP_SYNC_CONFIG",)
        }
        env["ZCODE_BOOTSTRAP_PROBE_OUT"] = str(probe_out)
        env["ZCODE_DESKTOP_SYNC_CONFIG"] = "/foreign-install/control.json"
        completed = self._run_node(node, staged / "out/main/index.js", env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        seen = json.loads(probe_out.read_text(encoding="utf-8"))
        self.assertIsNone(seen["ZCODE_DESKTOP_SYNC_CONFIG"])

    def _run_node(self, node, entry, env):
        import subprocess

        entry = Path(entry)
        return subprocess.run(
            [node, str(entry)],
            cwd=str(entry.parents[2]),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


class RejectionTests(FixtureCase):
    def test_unsupported_version_is_refused(self):
        self.build(version="3.18.1")
        self.pin()
        with self.assertRaisesRegex(tool.NativeInstallError, "unsupported Desktop version"):
            tool.preflight(self.target, self.state)

    def test_asar_hash_mismatch_is_refused(self):
        data = bytearray(self.asar.read_bytes())
        data[-1] ^= 0xFF
        self.asar.write_bytes(bytes(data))
        with self.assertRaisesRegex(tool.NativeInstallError, "app.asar SHA-256 mismatch"):
            tool.preflight(self.target, self.state)

    def test_executable_hash_mismatch_is_refused(self):
        self.exe.write_bytes(b"MZ-other-build")
        with self.assertRaisesRegex(tool.NativeInstallError, "ZCode.exe SHA-256 mismatch"):
            tool.preflight(self.target, self.state)

    def test_engine_hash_mismatch_is_refused(self):
        self.engine.write_bytes(ENGINE + b"// local edit\n")
        with self.assertRaisesRegex(tool.NativeInstallError, "zcode.cjs SHA-256 mismatch"):
            tool.preflight(self.target, self.state)

    def test_host_hash_mismatch_is_refused(self):
        drifted = HOST + b"// drifted\n"
        self.build(host=drifted)
        self.pin(host_for_hash=HOST)
        with self.assertRaisesRegex(tool.NativeInstallError, "out/host/index.js SHA-256 mismatch"):
            tool.preflight(self.target, self.state)

    def test_missing_expose_anchor_is_refused(self):
        self.build(host=b"function f(){return{mutateSessionFacts:ie,other:1};}\n")
        self.pin()
        with self.assertRaisesRegex(tool.NativeInstallError, "expose anchor appears 0 times"):
            tool.preflight(self.target, self.state)

    def test_duplicate_api_anchor_is_refused(self):
        host = ANCHOR_EXPOSE + b"\n" + ANCHOR_API + ANCHOR_API + b"\n"
        self.build(host=host)
        self.pin()
        with self.assertRaisesRegex(tool.NativeInstallError, "API anchor appears 2 times"):
            tool.preflight(self.target, self.state)

    def test_apply_refuses_target_process_from_target_executable(self):
        self.prepare()
        with self.assertRaisesRegex(tool.NativeInstallError, "running from the target"):
            tool.apply(
                self.target,
                self.state,
                probe=lambda: [{"pid": os.getpid(), "path": str(self.exe)}],
            )
        self.assertTrue(self.asar.is_file(), "refused apply changed app.asar")

    def test_same_named_process_elsewhere_does_not_block_apply(self):
        self.prepare()
        other = self.tmp / "another-install" / "ZCode.exe"
        applied = tool.apply(
            self.target,
            self.state,
            probe=lambda: [{"pid": os.getpid(), "path": str(other)}],
        )
        self.assertEqual(applied["status"], "applied")

    def test_target_changes_after_prepare_are_refused(self):
        self.prepare()
        engine_bytes = self.engine.read_bytes()
        self.engine.write_bytes(engine_bytes + b"// drift\n")
        with self.assertRaisesRegex(tool.NativeInstallError, "engine is no longer the reviewed original"):
            self.apply()
        self.engine.write_bytes(engine_bytes)

        asar_bytes = self.asar.read_bytes()
        self.asar.write_bytes(asar_bytes + b"\0")
        with self.assertRaisesRegex(tool.NativeInstallError, "app.asar changed after prepare"):
            self.apply()
        self.asar.write_bytes(asar_bytes)

        self.assertEqual(self.apply()["status"], "applied")

    def test_expanded_tree_change_is_detected_and_refused_by_rollback(self):
        self.prepare()
        self.apply()
        extra = self.app_dir / "out" / "extra.js"
        extra.write_text("export const extra = 1;\n", encoding="utf-8")
        status = tool.status(self.target, self.state)
        self.assertEqual(status["status"], "modified")
        self.assertFalse(status["ok"])
        self.assertTrue(any("expanded tree digest changed" in item for item in status["conflicts"]))
        with self.assertRaisesRegex(tool.NativeInstallError, "changed after installation"):
            self.rollback()
        self.assertTrue(extra.is_file(), "refused rollback removed the foreign file")
        extra.unlink()
        self.assertEqual(self.rollback()["status"], "rolled_back")

    def test_reintroduced_asar_is_reported_and_blocks_rollback(self):
        asar_before = self.asar.read_bytes()
        self.prepare()
        self.apply()
        self.asar.write_bytes(asar_before)
        status = tool.status(self.target, self.state)
        self.assertEqual(status["status"], "modified")
        self.assertFalse(status["ok"])
        self.assertTrue(any("auto-update" in item for item in status["conflicts"]))
        with self.assertRaisesRegex(tool.NativeInstallError, "app.asar exists"):
            self.rollback()
        self.assertTrue(self.asar_backup.is_file())

    def test_unknown_executable_path_is_not_treated_as_stopped(self):
        tool.PROCESS_PROBE = lambda: [{"pid": os.getpid(), "path": ""}]
        pre = tool.preflight(self.target, self.state)
        self.assertEqual(pre["running"]["unknownPaths"], [os.getpid()])
        tool.PROCESS_PROBE = lambda: []
        self.prepare()
        tool.PROCESS_PROBE = lambda: [{"pid": os.getpid(), "path": ""}]
        with self.assertRaisesRegex(tool.NativeInstallError, "cannot determine the executable path"):
            tool.apply(self.target, self.state)
        self.assertTrue(self.asar.is_file(), "refused apply changed app.asar")
        tool.PROCESS_PROBE = lambda: []
        self.apply()
        tool.PROCESS_PROBE = lambda: [{"pid": os.getpid(), "path": None}]
        with self.assertRaisesRegex(tool.NativeInstallError, "cannot determine the executable path"):
            tool.rollback(self.target, self.state)
        self.assertTrue(self.app_dir.is_dir(), "refused rollback moved the expanded tree")

    def test_foreign_state_files_are_refused_without_overwrite(self):
        self.state.mkdir(parents=True)
        foreign = {
            "host": "127.0.0.1",
            "port": 0,
            "token": "foreign-token-0123456789abcdef",
            "discoveryPath": str(self.state / "host-discovery.json"),
            "allowedWorkspaces": [str(self.workspace)],
            "nativeTransport": {"enabled": True, "ownershipPath": str(self.tmp / "elsewhere-ownership.json")},
        }
        control_path = self.state / "control.json"
        control_path.write_text(json.dumps(foreign), encoding="utf-8")
        ownership_path = self.state / "native-ownership.json"
        ownership_path.write_text(json.dumps({"ownedSids": ["foreign-sid-1"], "generation": 7}), encoding="utf-8")
        control_bytes = control_path.read_bytes()
        ownership_bytes = ownership_path.read_bytes()
        with self.assertRaisesRegex(tool.NativeInstallError, "state directory already contains files"):
            self.prepare()
        self.assertEqual(control_path.read_bytes(), control_bytes)
        self.assertEqual(ownership_path.read_bytes(), ownership_bytes)
        self.assertFalse(self.staging.exists())


class PartialStateTests(FixtureCase):
    """Simulate an interrupted apply by performing its real renames by hand.

    Only the filesystem effects of apply's two renames and the ledger status are
    written directly; status/rollback then run for real against that state.
    """

    def _mark_partial(self, steps):
        ledger = self.ledger()
        ledger["status"] = "partial"
        ledger["phase"] = "apply-in-progress"
        ledger["steps"] = steps
        self.ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    def test_asar_renamed_but_app_missing_is_partial_and_recoverable(self):
        asar_before = self.asar.read_bytes()
        self.prepare()
        os.rename(self.asar, self.asar_backup)
        self._mark_partial(["asar"])
        status = tool.status(self.target, self.state)
        self.assertEqual(status["status"], "partial")
        self.assertFalse(status["ok"])
        rolled = self.rollback()
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual(self.asar.read_bytes(), asar_before)
        self.assertFalse(self.asar_backup.exists())
        self.assertFalse(self.app_dir.exists())
        self.assertFalse(self.staging.exists())

    def test_both_renames_done_is_partial_and_recoverable(self):
        asar_before = self.asar.read_bytes()
        self.prepare()
        os.rename(self.asar, self.asar_backup)
        os.rename(self.staging / "app", self.app_dir)
        self._mark_partial(["asar", "app"])
        status = tool.status(self.target, self.state)
        self.assertEqual(status["status"], "partial")
        self.assertIn("phase", status)
        rolled = self.rollback()
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual(self.asar.read_bytes(), asar_before)
        self.assertFalse(self.app_dir.exists())
        self.assertFalse(self.asar_backup.exists())

    def test_compensated_apply_is_not_mistaken_for_an_updater_asar(self):
        asar_before = self.asar.read_bytes()
        self.prepare()
        self.apply()
        # Simulate a successful _compensate_apply: app back in staging, asar restored.
        os.rename(self.app_dir, self.staging / "app")
        os.rename(self.asar_backup, self.asar)
        ledger = self.ledger()
        ledger.update(
            {
                "status": "partial",
                "phase": "apply-failed",
                "steps": ["asar", "app"],
                "compensated": True,
                "failure": "NativeInstallError: injected verification failure",
            }
        )
        self.ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

        status = tool.status(self.target, self.state)
        self.assertEqual(status["status"], "partial")
        self.assertEqual(status["conflicts"], [])
        self.assertIn("note", status)

        rolled = self.rollback()
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual(self.asar.read_bytes(), asar_before)
        self.assertFalse(self.app_dir.exists())
        self.assertFalse(self.asar_backup.exists())
        self.assertFalse(self.staging.exists())
        self.assertEqual(self.ledger()["status"], "rolled_back")


class SecurityTests(FixtureCase):
    def test_cli_output_and_ledger_have_no_token(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = tool.main(
                [
                    "--state-dir",
                    str(self.state),
                    "prepare",
                    "--target",
                    str(self.target),
                    "--workspace",
                    str(self.workspace),
                ]
            )
        self.assertEqual(code, 0, captured.getvalue())
        token = self.control()["token"]
        self.assertGreaterEqual(len(token), 16)
        self.assertNotIn(token, captured.getvalue())
        self.assertNotIn(token, self.ledger_path.read_text(encoding="utf-8"))
        pointer = (self.staging / "app" / tool.POINTER_REL).read_text(encoding="utf-8")
        self.assertNotIn(token, pointer)
        self.assertNotIn(token, (self.staging / "app" / "out/main/index.js").read_text(encoding="utf-8"))

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = tool.main(["--state-dir", str(self.state), "status", "--target", str(self.target)])
        self.assertEqual(code, 0)
        self.assertNotIn(token, captured.getvalue())

    def test_public_sources_and_docs_have_no_absolute_paths(self):
        pattern = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
        for relative in (
            "scripts/desktop_native_install.py",
            "runtime/desktop_sync/native_bootstrap.mjs",
            "docs/DESKTOP_NATIVE_INSTALL.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), f"{relative} contains an absolute Windows path")
            self.assertNotIn(str(Path.home()), text, f"{relative} contains the local home path")

    def test_install_doc_states_required_boundaries(self):
        text = (ROOT / "docs/DESKTOP_NATIVE_INSTALL.md").read_text(encoding="utf-8")
        for needle in (
            "appData/ZCode",
            "session",
            "逐字节",
            "hydrate",
            "未实测",
            "<installed-zcode>",
            "<workspace>",
            "自动更新",
            "rollback",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("ZCodeDesktopSyncShim", (ROOT / "runtime/desktop_sync/native_bootstrap.mjs").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
