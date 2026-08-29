"""The cache-busting stamp on /static URLs.

Shipping a beta proved VERSION alone is not enough: it only moves at a release,
so five builds of the rolling :beta tag all served
`/static/settings.js?v=1.8.0.0-beta` with different JavaScript underneath, and a
browser happily kept the first one against the fifth build's markup. These tests
pin the property that failed — the stamp must change when an asset does.
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import backend_main  # noqa: E402


class StaticDigestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.write("app.js", "console.log('one');")
        self.write("styles.css", "body { color: red }")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, content):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def digest(self):
        return backend_main._static_digest(self.dir)

    def test_changing_a_file_changes_the_stamp(self):
        # The whole point: new JavaScript must mean a new URL.
        before = self.digest()
        self.write("app.js", "console.log('two');")
        self.assertNotEqual(before, self.digest())

    def test_identical_content_keeps_the_stamp(self):
        # A rebuild that changes nothing must not throw away good caches —
        # which is why this hashes content and not mtimes, since a git checkout
        # restamps every mtime on every build.
        before = self.digest()
        self.write("app.js", "console.log('one');")
        os.utime(os.path.join(self.dir, "app.js"), (0, 0))
        self.assertEqual(before, self.digest())

    def test_adding_or_removing_a_file_changes_the_stamp(self):
        before = self.digest()
        self.write("new.js", "//")
        after_add = self.digest()
        self.assertNotEqual(before, after_add)
        os.remove(os.path.join(self.dir, "new.js"))
        self.assertEqual(before, self.digest())

    def test_renaming_a_file_changes_the_stamp(self):
        # Same bytes, different path: the digest folds in names as well.
        before = self.digest()
        os.rename(os.path.join(self.dir, "app.js"),
                  os.path.join(self.dir, "app2.js"))
        self.assertNotEqual(before, self.digest())

    def test_nested_files_count(self):
        before = self.digest()
        self.write(os.path.join("sub", "deep.js"), "//")
        self.assertNotEqual(before, self.digest())

    def test_stamp_is_stable_across_calls(self):
        self.assertEqual(self.digest(), self.digest())

    def test_stamp_is_short_and_url_safe(self):
        stamp = self.digest()
        self.assertEqual(len(stamp), 8)
        self.assertTrue(stamp.isalnum())


class AssetVersionTest(unittest.TestCase):
    def test_combines_version_with_a_digest(self):
        # Shape matters: the release version stays legible for support, with
        # the digest appended rather than replacing it.
        version = backend_main._asset_version()
        self.assertIn(".", version)
        head, _, digest = version.rpartition(".")
        self.assertTrue(head)
        self.assertEqual(len(digest), 8)

    def test_falls_back_to_a_bare_version_when_assets_are_unreadable(self):
        # _asset_version() runs at import time, so a missing or unreadable
        # static directory must degrade to the plain version rather than take
        # the whole app down before it can serve its first request.
        with open(os.path.join(GUI_DIR, "..", "VERSION")) as f:
            expected = f.read().strip()

        def boom(_dir):
            raise OSError("no static directory")

        original = backend_main._static_digest
        backend_main._static_digest = boom
        try:
            self.assertEqual(backend_main._asset_version(), expected)
        finally:
            backend_main._static_digest = original

    def test_the_live_stamp_is_wired_into_templates(self):
        self.assertEqual(
            backend_main.templates.env.globals["asset_v"], backend_main.ASSET_VERSION
        )


if __name__ == "__main__":
    unittest.main()
