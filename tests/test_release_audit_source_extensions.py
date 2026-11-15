"""Audit executable text sources and archives using synthetic private data only."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/audit_release.py"
SPEC = importlib.util.spec_from_file_location("release_audit_source_extensions", SCRIPT)
release_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_audit)


class ReleaseAuditSourceExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="release-audit-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Assemble these to keep the checked-in test source itself portable.
        # These are intentionally fake values, never local account information.
        self.fake_key = "sk-" + "A" * 24
        self.fake_path = "Z" + ":" + "\\" + "synthetic-account" + "\\" + "private-input"
        self.extensions = ("mjs", "js", "sql", "MJS", "JS", "SQL")
        self.sensitive = ("/* " + self.fake_key + " " + self.fake_path + " */\n").encode()

    def assert_redacted(self, errors):
        rendered = "\n".join(errors)
        self.assertNotIn(self.fake_key, rendered)
        self.assertNotIn(self.fake_path, rendered)

    def test_source_tree_rejects_credentials_and_private_paths_in_each_new_extension(self):
        for index, extension in enumerate(self.extensions):
            (self.root / ("fixture-" + str(index) + "." + extension)).write_bytes(self.sensitive)
        errors, count = release_audit.audit(self.root, archives=False)
        self.assertEqual(count, len(self.extensions))
        for index, extension in enumerate(self.extensions):
            prefix = "fixture-" + str(index) + "." + extension
            self.assertIn(prefix + ": API key", errors)
            self.assertIn(prefix + ": absolute Windows path", errors)
        self.assert_redacted(errors)

    def test_zip_rejects_the_same_new_extensions_without_echoing_body_content(self):
        dist = self.root / "dist"
        dist.mkdir()
        with zipfile.ZipFile(dist / "fixture.zip", "w") as archive:
            for extension in self.extensions:
                archive.writestr("runtime/fixture." + extension, self.sensitive)
        errors, count = release_audit.audit(self.root)
        self.assertEqual(count, len(self.extensions))
        for extension in self.extensions:
            prefix = "fixture.zip/runtime/fixture." + extension
            self.assertIn(prefix + ": API key", errors)
            self.assertIn(prefix + ": absolute Windows path", errors)
        self.assert_redacted(errors)

    def test_portable_javascript_and_sql_pass_in_both_source_tree_and_zip(self):
        portable = {
            "runtime/transport.mjs": "export const resolveWorkspace = value => String(value);\n",
            "runtime/helpers.js": "const path = require('node:path'); module.exports = path.resolve;\n",
            "tests/schema.sql": "CREATE TABLE fixture (id TEXT PRIMARY KEY, value TEXT NOT NULL);\n",
        }
        for name, content in portable.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        dist = self.root / "dist"
        dist.mkdir()
        with zipfile.ZipFile(dist / "portable.zip", "w") as archive:
            for name, content in portable.items():
                archive.writestr(name, content)
        errors, count = release_audit.audit(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(count, 2 * len(portable))

    def test_public_cli_reports_rejection_without_printing_the_synthetic_secret(self):
        (self.root / "fixture.mjs").write_bytes(self.sensitive)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["ok"])
        self.assertIn("fixture.mjs: API key", report["errors"])
        self.assert_redacted(report["errors"])
        self.assertNotIn(self.fake_key, completed.stdout + completed.stderr)
        self.assertNotIn(self.fake_path, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
