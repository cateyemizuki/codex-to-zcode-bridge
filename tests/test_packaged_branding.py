"""Check branding from a clean build, without installed-cache leftovers."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build as package_builder


class PackagedBrandingTests(unittest.TestCase):
    def test_clean_build_packages_loadable_square_icons_and_attribution(self):
        with tempfile.TemporaryDirectory(prefix='bridge-branding-') as temporary:
            stage = Path(temporary).resolve()
            self.assertTrue(stage.is_relative_to(Path(tempfile.gettempdir()).resolve()))
            package_builder.build(stage)
            root = stage / 'plugins/codex'
            manifest = json.loads((root / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
            for field in ('logo', 'composerIcon'):
                relative = manifest['interface'].get(field)
                self.assertIsInstance(relative, str, field)
                self.assertTrue(relative.startswith('./assets/'), field)
                image = (root / relative).resolve()
                self.assertTrue(image.is_relative_to(root.resolve()))
                data = image.read_bytes()
                self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
                self.assertEqual(data[12:16], b'IHDR')
                width, height = struct.unpack('>II', data[16:24])
                self.assertGreater(width, 0)
                self.assertEqual(width, height)
                self.assertEqual(data, (ROOT / 'runtime/assets' / image.name).read_bytes())
            for host in ('codex', 'zcode'):
                self.assertEqual((stage / 'plugins' / host / 'assets/NOTICE').read_bytes(),
                                 (ROOT / 'runtime/assets/NOTICE').read_bytes())


if __name__ == '__main__':
    unittest.main()
