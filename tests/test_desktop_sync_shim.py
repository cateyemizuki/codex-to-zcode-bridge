import hashlib
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("desktop_sync_shim", ROOT / "scripts/desktop_sync_shim.py")
shim = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shim
SPEC.loader.exec_module(shim)
COPY_SPEC = importlib.util.spec_from_file_location("desktop_sync_copy", ROOT / "scripts/desktop_sync_copy.py")
copy_tool = importlib.util.module_from_spec(COPY_SPEC)
sys.modules[COPY_SPEC.name] = copy_tool
COPY_SPEC.loader.exec_module(copy_tool)


def make_asar(files: dict[str, bytes]) -> bytes:
    tree = {"files": {}}
    for name, data in files.items():
        parts = name.split("/")
        node = tree
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"size": len(data), "offset": "000000000000"}
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
    body = prefix + header + b"\0" * (base - 16 - header_size) + b"".join(files.values())
    return body

PACKAGE = json.dumps({"name": "@zcode/desktop", "version": "3.18.2-lab"}, indent=2) + "\n"
TARGET = (
    "let Xi=fpe({agentService:Et,taskIndexRepo:T}),_i=xx({}),I=1;\n"
    "function f(){return{mutateSessionFacts:ie,other:1};}\n"
)


class DesktopSyncShimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        (self.app / "out/host").mkdir(parents=True)
        (self.app / "package.json").write_text(PACKAGE, encoding="utf-8")
        (self.app / "out/host/index.js").write_text(TARGET, encoding="utf-8")
        self.state = self.root / "state"
        self.manifest = self.root / "manifest.json"
        shim.write_manifest(self.app, self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_records_version_and_target_hash(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["desktopVersion"], "3.18.2-lab")
        self.assertIn("out/host/index.js", manifest["targets"])
        self.assertEqual(len(manifest["targets"]["out/host/index.js"]), 64)

    def test_packaged_application_is_refused(self):
        (self.app / "resources").mkdir()
        (self.app / "resources/app.asar").write_bytes(b"")
        with self.assertRaisesRegex(shim.ShimError, "app.asar"):
            shim.install(self.app, self.state, shim.load_manifest(self.manifest), dry_run=True)

    def test_unsupported_version_is_rejected(self):
        package = json.loads(PACKAGE)
        package["version"] = "3.19.0"
        (self.app / "package.json").write_text(json.dumps(package), encoding="utf-8")
        with self.assertRaisesRegex(shim.ShimError, "unsupported Desktop version"):
            shim.verify(self.app)

    def test_manifest_cannot_override_builtin_3182_hash(self):
        manifest = {"desktopVersion": "3.18.2", "targets": {shim.TARGET: "0" * 64}}
        with self.assertRaisesRegex(shim.ShimError, "conflict with the built-in"):
            shim.expected_hashes("3.18.2", manifest)

    def test_manifest_version_mismatch_is_rejected(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["desktopVersion"] = "3.17.2"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(shim.ShimError, "does not match application version"):
            shim.verify(self.app, shim.load_manifest(self.manifest))

    def test_hash_mismatch_is_rejected(self):
        target = self.app / "out/host/index.js"
        target.write_text(TARGET + "// drift\n", encoding="utf-8")
        with self.assertRaisesRegex(shim.ShimError, "SHA-256 mismatch"):
            shim.verify(self.app, shim.load_manifest(self.manifest))

    def test_dry_run_does_not_write(self):
        before = (self.app / "out/host/index.js").read_bytes()
        report = shim.install(self.app, self.state, shim.load_manifest(self.manifest), dry_run=True)
        self.assertTrue(report["dryRun"])
        self.assertEqual((self.app / "out/host/index.js").read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_install_and_restore_are_byte_exact(self):
        before = (self.app / "out/host/index.js").read_bytes()
        report = shim.install(self.app, self.state, shim.load_manifest(self.manifest))
        patched = (self.app / "out/host/index.js").read_bytes()
        self.assertEqual(report["originalSha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(report["patchedSha256"], hashlib.sha256(patched).hexdigest())
        self.assertIn(shim.SHIM_MARKER.encode(), patched)
        self.assertIn(b"syncSessionFacts", patched)
        self.assertEqual(shim.status(self.state)["status"], "patched")
        restored = shim.restore(self.state)
        self.assertTrue(restored["restored"])
        self.assertEqual((self.app / "out/host/index.js").read_bytes(), before)
        self.assertEqual(restored["originalSha256"], hashlib.sha256(before).hexdigest())

    def test_second_install_is_refused(self):
        shim.install(self.app, self.state, shim.load_manifest(self.manifest))
        with self.assertRaisesRegex(shim.ShimError, "already present|state already exists"):
            shim.install(self.app, self.state, shim.load_manifest(self.manifest))

    def test_restore_refuses_foreign_change_without_force(self):
        shim.install(self.app, self.state, shim.load_manifest(self.manifest))
        target = self.app / "out/host/index.js"
        target.write_bytes(target.read_bytes() + b"// foreign\n")
        with self.assertRaisesRegex(shim.ShimError, "target changed"):
            shim.restore(self.state)
        shim.restore(self.state, force=True)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                         json.loads(self.manifest.read_text(encoding="utf-8"))["targets"]["out/host/index.js"])

    def test_shim_has_no_generic_execution_surface(self):
        source = (ROOT / "runtime/desktop_sync/shim.mjs").read_text(encoding="utf-8")
        for token in ("eval(", "child_process", "exec(", "execSync", "spawn(", "require("):
            self.assertNotIn(token, source)
        self.assertIn("op === 'probe'", source)
        self.assertIn("op === 'sync'", source)
        self.assertIn("resolveAllowed", source)
        self.assertNotIn("::1", source)

    def test_expose_hook_is_a_fixed_read_only_project_resolver(self):
        expose = shim.REPLACEMENT_EXPOSE
        # A fixed factory argument placed after the caller spread: the private
        # control plane cannot override or drop it, and no global channel exists.
        self.assertIn("...O,resolveProjectId:async S=>{", expose)
        self.assertNotIn("globalThis.resolve", expose)
        self.assertIn("await T.getProjectRepository()", expose)
        self.assertIn("readTree({includeTasks:!1}).projects", expose)
        for token in ("eval(", "new Function", "child_process", "app.asar"):
            self.assertNotIn(token, expose)
        # Only these fixed decisions cross the boundary.
        for status in ("unique", "none", "ambiguous", "not_found", "inactive", "workspace_mismatch", "invalid"):
            self.assertIn(f'status:"{status}"', expose)
        # Read-only: no project create/rename/move and no raw SQL.
        for token in ("saveProject", "initializeProjectRoot", "reserveCreatedTask", "moveTask", "INSERT ", "UPDATE "):
            self.assertNotIn(token, expose)

    def test_project_resolver_stays_out_of_the_fpe_api_scope(self):
        # Inside fpe the local T is the unsubscribeIndex helper; the resolver must
        # stay at the module-level expose anchor where T is the task index repo.
        self.assertNotIn("resolveProjectId", shim.REPLACEMENT_API)
        self.assertIn("resolveProjectId", shim.REPLACEMENT_EXPOSE)

    def test_plan_inlines_the_fixed_project_resolver_once(self):
        plan = shim.make_plan(self.app, shim.load_manifest(self.manifest))
        text = plan.patched.decode("utf-8")
        self.assertEqual(text.count("resolveProjectId:async S=>{"), 1)
        self.assertIn("...O,resolveProjectId:async S=>{", text)
        self.assertIn("await T.getProjectRepository()", text)
        self.assertIn("ZCODE_DESKTOP_NATIVE_TRANSPORT_V1", text)

    def test_injected_resolver_matches_only_one_active_local_project(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to execute the injected resolver text")
        expose = shim.REPLACEMENT_EXPOSE
        start = expose.index("resolveProjectId:") + len("resolveProjectId:")
        resolver = expose[start:expose.index(",makeEnvelope:ti")]
        projects = [
            {"id": "prj_one", "hostId": "local", "deleted": False, "roots": [{"rootPath": "/projects/ws/", "isPrimary": True}]},
            {"id": "prj_other", "hostId": "local", "deleted": False, "roots": [{"rootPath": "/projects/other"}]},
            {"id": "prj_remote", "hostId": "remote-1", "deleted": False, "roots": [{"rootPath": "/projects/ws"}]},
            {"id": "prj_deleted", "hostId": "local", "deleted": True, "roots": [{"rootPath": "/projects/ws"}]},
            {"id": "prj_twin_a", "hostId": "local", "deleted": False, "roots": [{"rootPath": "/projects/twin"}]},
            {"id": "prj_twin_b", "hostId": "local", "deleted": False, "roots": [{"rootPath": "/projects/twin"}]},
        ]
        cases = [
            {"workspacePath": "\\projects\\ws", "projectId": None},
            {"workspacePath": "/projects/nothing", "projectId": None},
            {"workspacePath": "/projects/twin", "projectId": None},
            {"workspacePath": "/projects/ws", "projectId": "prj_one"},
            {"workspacePath": "/projects/ws", "projectId": "prj_missing"},
            {"workspacePath": "/projects/ws", "projectId": "prj_deleted"},
            {"workspacePath": "/projects/ws", "projectId": "prj_other"},
        ]
        script = (
            "const projects=" + json.dumps(projects) + ";"
            "const T={async getProjectRepository(){return{readTree(input){"
            "if(input.includeTasks!==false)throw new Error('tasks must not be read');"
            "return{projects};}};}};"
            "const resolveProjectId=" + resolver + ";"
            "const cases=" + json.dumps(cases) + ";"
            "const out=[];for(const item of cases)out.push(await resolveProjectId(item));"
            "console.log(JSON.stringify(out));"
        )
        completed = subprocess.run([node, "--input-type=module", "-e", script],
                                   capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = json.loads(completed.stdout)
        self.assertEqual(out[0], {"status": "unique", "projectId": "prj_one"})
        self.assertEqual(out[1], {"status": "none", "projectId": None})
        self.assertEqual(out[2], {"status": "ambiguous", "projectId": None, "count": 2})
        self.assertEqual(out[3], {"status": "unique", "projectId": "prj_one"})
        self.assertEqual(out[4], {"status": "not_found", "projectId": None})
        self.assertEqual(out[5], {"status": "inactive", "projectId": None})
        self.assertEqual(out[6], {"status": "workspace_mismatch", "projectId": None})

    def test_sync_cli_sends_canonical_workspace(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        captured = {}

        def fake_call(config, message):
            captured.update(message)
            return {"ok": True}

        with patch.object(shim, "control_call", side_effect=fake_call):
            code = shim.main([
                "sync", "--config", str(self.state / "control.json"),
                "--workspace", str(workspace) + "/", "--session-id", "sess_unit",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(captured["workspace"], str(workspace.resolve()))

    def test_control_call_requires_loopback_v4(self):
        config = self.root / "control.json"
        config.write_text(json.dumps({"host": "::1", "port": 1, "token": "x" * 32}), encoding="utf-8")
        with self.assertRaisesRegex(shim.ShimError, "127.0.0.1"):
            shim.control_call(config, {"op": "probe"})

    def test_configure_writes_private_token_and_allowlist(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.state.mkdir()
        (self.state / "host-discovery.json").write_text("{\"port\": 1}\n", encoding="utf-8")
        report = shim.configure(self.state, [str(workspace)])
        config = json.loads(Path(report["config"]).read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(config["token"]), 16)
        self.assertEqual(config["allowedWorkspaces"], [str(workspace.resolve())])
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertTrue(config["discoveryPath"].endswith("host-discovery.json"))
        self.assertTrue(Path(config["discoveryPath"]).is_absolute())
        self.assertFalse((self.state / "host-discovery.json").exists())

    def test_configure_rejects_missing_workspace(self):
        with self.assertRaisesRegex(shim.ShimError, "workspace does not exist"):
            shim.configure(self.state, [str(self.root / "missing")])


class EngineAndCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine_text = (
            'async reconcileSessions(t){let n=Sat.parse(t);if(!this.host.reconcileSessions)'
            'throw new Error("sessionsIndex/reconcile is unavailable");'
            "return wat.parse(await this.host.reconcileSessions(n))}"
        )
        self.engine = self.root / "zcode.cjs"
        self.engine.write_text(self.engine_text + "\n", encoding="utf-8")
        self.engine_sha = hashlib.sha256(self.engine.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_engine_patch_requires_exact_hash(self):
        with self.assertRaisesRegex(shim.ShimError, "SHA-256 mismatch"):
            shim.engine_plan(self.engine, "0" * 64)
        plan = shim.engine_plan(self.engine, self.engine_sha)
        self.assertIn(b"hydratePublisher(s,void 0,!0)", plan.patched)
        self.assertNotIn(b"hydratePublisher(s,void 0,!0)", plan.original)

    def test_copy_prepare_and_restore_roundtrip(self):
        source = self.root / "source"
        app = source / "resources/app"
        app.mkdir(parents=True)
        (source / "ZCode.exe").write_bytes(b"fake executable")
        (source / "resources/glm").mkdir(parents=True)
        (source / "resources/glm/zcode.cjs").write_text(self.engine_text + "\n", encoding="utf-8")
        (source / "resources/config/provider").mkdir(parents=True)
        (source / "resources/config/provider/zcode-builtin.json").write_text("{}", encoding="utf-8")
        host_text = (
            "let Xi=fpe({agentService:Et,taskIndexRepo:T}),_i=1;\n"
            "function f(){return{mutateSessionFacts:ie,other:1};}\n"
        )
        asar_files = {
            "package.json": json.dumps({"name": "@zcode/desktop", "version": "3.18.2-lab"}).encode(),
            "out/host/index.js": host_text.encode(),
            "out/main/index.js": b"export {};\n",
        }
        (source / "resources/app.asar").write_bytes(make_asar(asar_files))
        shutil.rmtree(app)
        engine_sha = hashlib.sha256((source / "resources/glm/zcode.cjs").read_bytes()).hexdigest()
        host_sha = hashlib.sha256(host_text.encode()).hexdigest()
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "desktopVersion": "3.18.2-lab",
            "targets": {
                "resources/app/out/host/index.js": host_sha,
                "resources/glm/zcode.cjs": engine_sha,
            },
        }), encoding="utf-8")
        destination = self.root / "copy"
        state = self.root / "state"
        dry = copy_tool.prepare(source, destination, state, copy_tool.shim.load_manifest(manifest), dry_run=True)
        self.assertTrue(dry["dryRun"])
        self.assertFalse(destination.exists())
        result = copy_tool.prepare(source, destination, state, copy_tool.shim.load_manifest(manifest))
        self.assertEqual(result["desktopVersion"], "3.18.2-lab")
        self.assertFalse((destination / "resources/app.asar").exists())
        self.assertTrue((destination / "resources/app.asar.original").is_file())
        self.assertIn(b"ZCODE_DESKTOP_SYNC_SHIM_V1",
                      (destination / "resources/app/out/host/index.js").read_bytes())
        self.assertIn(b"hydratePublisher(s,void 0,!0)",
                      (destination / "resources/glm/zcode.cjs").read_bytes())
        main_entry = destination / copy_tool.MAIN_REL
        original_entry = destination / copy_tool.MAIN_ORIGINAL_REL
        self.assertIn(copy_tool.BOOTSTRAP_MARKER.encode(), main_entry.read_bytes())
        self.assertEqual(original_entry.read_bytes(), asar_files["out/main/index.js"])
        self.assertEqual(copy_tool.verify_bootstrap(destination)["originalSha256"],
                         hashlib.sha256(asar_files["out/main/index.js"]).hexdigest())
        self.assertTrue(copy_tool.verify_patched_copy(destination)["engineReconcilePatch"])
        report = copy_tool.restore(state)
        self.assertTrue(report["restored"])
        self.assertFalse((destination / "resources/app").exists())
        self.assertTrue((destination / "resources/app.asar").is_file())
        self.assertEqual(hashlib.sha256((destination / "resources/app.asar").read_bytes()).hexdigest(),
                         result["asarSha256"])
        self.assertEqual(hashlib.sha256((destination / "resources/glm/zcode.cjs").read_bytes()).hexdigest(),
                         engine_sha)
        self.assertEqual(hashlib.sha256((destination / "ZCode.exe").read_bytes()).hexdigest(),
                         hashlib.sha256(b"fake executable").hexdigest())

    def test_engine_manifest_cannot_override_builtin_3182_hash(self):
        manifest = {"desktopVersion": "3.18.2", "targets": {
            "resources/glm/zcode.cjs": "19df55be" + "0" * 56,
        }}
        with self.assertRaisesRegex(copy_tool.CopyError, "conflicts with the built-in"):
            copy_tool.expected_engine_hash("3.18.2", manifest)

    def test_copy_manifest_mismatch_is_rejected(self):
        source = self.root / "source"
        (source / "resources").mkdir(parents=True)
        (source / "ZCode.exe").write_bytes(b"exe")
        (source / "resources/glm").mkdir()
        (source / "resources/glm/zcode.cjs").write_text(self.engine_text, encoding="utf-8")
        (source / "resources/app.asar").write_bytes(make_asar({
            "package.json": b'{"version":"3.18.2-lab"}',
            "out/host/index.js": b"x",
        }))
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "desktopVersion": "3.18.2",
            "targets": {
                "resources/app/out/host/index.js": "0" * 64,
                "resources/glm/zcode.cjs": "0" * 64,
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(copy_tool.CopyError, "does not match the application version"):
            copy_tool.prepare(source, self.root / "copy", self.root / "state",
                              copy_tool.shim.load_manifest(manifest), dry_run=True)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.destination = self.root / "copy"
        self.main = self.destination / copy_tool.MAIN_REL
        self.main.parent.mkdir(parents=True)
        self.main_bytes = b'import "@zcode/desktop";\nexport const marker = "original";\n'
        self.main.write_bytes(self.main_bytes)
        self.sha = hashlib.sha256(self.main_bytes).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_install_wraps_and_preserves_original(self):
        report = copy_tool.install_bootstrap(self.destination, self.sha)
        wrapper = self.main.read_bytes()
        self.assertIn(copy_tool.BOOTSTRAP_MARKER.encode(), wrapper)
        self.assertIn(b"index.sync-original.js", wrapper)
        self.assertNotIn(b"child_process", wrapper)
        original = (self.destination / copy_tool.MAIN_ORIGINAL_REL).read_bytes()
        self.assertEqual(original, self.main_bytes)
        self.assertEqual(report["originalSha256"], self.sha)
        state = copy_tool.verify_bootstrap(self.destination)
        self.assertEqual(state["bootstrapSha256"], hashlib.sha256(wrapper).hexdigest())
        self.assertEqual(state["originalSha256"], self.sha)

    def test_install_refuses_hash_mismatch(self):
        with self.assertRaisesRegex(copy_tool.CopyError, "does not match app.asar"):
            copy_tool.install_bootstrap(self.destination, "0" * 64)
        self.assertEqual(self.main.read_bytes(), self.main_bytes)
        self.assertFalse((self.destination / copy_tool.MAIN_ORIGINAL_REL).exists())

    def test_install_refuses_second_wrap(self):
        copy_tool.install_bootstrap(self.destination, self.sha)
        with self.assertRaisesRegex(copy_tool.CopyError, "already present|already exists"):
            copy_tool.install_bootstrap(self.destination, self.sha)

    def test_launch_requires_bootstrap(self):
        (self.destination / "ZCode.exe").write_bytes(b"exe")
        with self.assertRaisesRegex(copy_tool.CopyError, "no sync startup entry"):
            copy_tool.launch(self.destination, self.root / "ud", self.root / "home", None, None)

    def test_launch_requires_host_and_engine_patches(self):
        (self.destination / "ZCode.exe").write_bytes(b"exe")
        copy_tool.install_bootstrap(self.destination, self.sha)
        with self.assertRaisesRegex(copy_tool.CopyError, "host bundle is not readable|no sync shim"):
            copy_tool.launch(self.destination, self.root / "ud", self.root / "home", None, None)

    def test_verify_checks_bootstrap_original_against_asar(self):
        copy_tool.install_bootstrap(self.destination, self.sha)
        (self.destination / "resources").mkdir(exist_ok=True)
        backup = self.destination / copy_tool.ASAR_BACKUP_REL
        backup.write_bytes(make_asar({"out/main/index.js": self.main_bytes}))
        state = copy_tool.verify_bootstrap(self.destination)
        self.assertEqual(state["asarMainSha256"], self.sha)
        backup.write_bytes(make_asar({"out/main/index.js": b"export const marker = 'drift';\n"}))
        with self.assertRaisesRegex(copy_tool.CopyError, "does not match app.asar.original"):
            copy_tool.verify_bootstrap(self.destination)

    def test_bootstrap_source_is_narrow(self):
        source = (ROOT / "runtime/desktop_sync/bootstrap.mjs").read_text(encoding="utf-8")
        for token in ("eval(", "child_process", "exec(", "execSync", "spawn(", "require("):
            self.assertNotIn(token, source)
        self.assertEqual(source.count(copy_tool.BOOTSTRAP_MARKER), 1)
        self.assertIn("app.setPath", source)
        self.assertIn("ZCODE_DESKTOP_USER_DATA_DIR", source)


if __name__ == "__main__":
    unittest.main()
