"""Execute the real ESM transport with fake native public methods; never launch Desktop."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_shim_cli", ROOT / "scripts/desktop_sync_shim.py")
shim = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shim
SPEC.loader.exec_module(shim)


class NativeTransportBehaviorTests(unittest.TestCase):
    def test_native_private_opt_in_preserves_authentication_and_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            report = shim.configure(root / "private", [str(workspace)])
            config_path = Path(report["config"])
            before = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(before["nativeTransport"]["enabled"])
            shim.configure_native(config_path, True)
            enabled = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(enabled["nativeTransport"]["enabled"])
            self.assertEqual(enabled["token"], before["token"])
            self.assertEqual(enabled["allowedWorkspaces"], before["allowedWorkspaces"])
            self.assertEqual(enabled["nativeTransport"]["ownershipPath"], before["nativeTransport"]["ownershipPath"])
            shim.configure_native(config_path, False)
            self.assertFalse(json.loads(config_path.read_text(encoding="utf-8"))["nativeTransport"]["enabled"])

    def test_native_transport_module(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the native transport behavioral tests")
        completed = subprocess.run(
            [node, "--test", str(ROOT / "tests/native_transport.test.mjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
