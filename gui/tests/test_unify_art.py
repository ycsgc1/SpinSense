"""Making a run's artwork uniform.

The reported bug: setting an album across a whole session rewrote every row's
album text but left each row's original cover, so the run still looked like
several different records. Two causes — artwork was skipped entirely when no
URL was chosen, and each row downloaded independently rather than sharing one
image — so both are pinned here.
"""
import asyncio
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import ipc_manager  # noqa: E402
import play_history  # noqa: E402


def _png_bytes(colour: tuple) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 200), colour).save(buf, "PNG")
    return buf.getvalue()


class UnifyArtTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp()
        self.art_dir = os.path.join(self.data_dir, "art")
        os.makedirs(self.art_dir, exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

        self._orig_art_dir = ipc_manager.ART_DIR
        self._orig_db = play_history.DB_PATH
        self._orig_fetch = ipc_manager._fetch_art
        ipc_manager.ART_DIR = self.art_dir
        play_history.DB_PATH = self.db_path

        self.fetches = []
        self.fetch_result = _png_bytes((10, 20, 30))

        async def fake_fetch(url):
            self.fetches.append(url)
            return self.fetch_result

        ipc_manager._fetch_art = fake_fetch

    def tearDown(self):
        ipc_manager.ART_DIR = self._orig_art_dir
        play_history.DB_PATH = self._orig_db
        ipc_manager._fetch_art = self._orig_fetch
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def play(self, title="T"):
        return play_history.record_play(title, "A", "Al", None, db_path=self.db_path)

    def art_of(self, pid):
        path = os.path.join(self.art_dir, f"{pid}.jpg")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def run_unify(self, ids, source, url=None):
        return asyncio.run(ipc_manager.unify_art(ids, source, url))

    def test_one_url_becomes_identical_art_on_every_play(self):
        ids = [self.play("a"), self.play("b"), self.play("c")]
        changed = self.run_unify(ids, ids[0], "http://a/cover.jpg")
        self.assertEqual(changed, ids)
        arts = {self.art_of(pid) for pid in ids}
        self.assertEqual(len(arts), 1)          # byte-identical across the run
        self.assertNotIn(None, arts)

    def test_the_image_is_fetched_once_not_once_per_play(self):
        # A ten-play run used to pull the same 1000x1000 image ten times.
        ids = [self.play(str(i)) for i in range(5)]
        self.run_unify(ids, ids[0], "http://a/cover.jpg")
        self.assertEqual(self.fetches, ["http://a/cover.jpg"])

    def test_every_row_gets_its_art_path_recorded(self):
        ids = [self.play("a"), self.play("b")]
        self.run_unify(ids, ids[0], "http://a/cover.jpg")
        for pid in ids:
            row = play_history.get_play(pid, db_path=self.db_path)
            self.assertEqual(row["art_path"], f"art/{pid}.jpg")

    def test_without_a_url_the_run_adopts_the_edited_play_art(self):
        # Typing an album by hand used to skip artwork altogether.
        ids = [self.play("a"), self.play("b"), self.play("c")]
        source = ids[1]
        with open(os.path.join(self.art_dir, f"{source}.jpg"), "wb") as f:
            f.write(b"SOURCE-ART")
        changed = self.run_unify(ids, source)
        self.assertEqual(changed, ids)
        for pid in ids:
            self.assertEqual(self.art_of(pid), b"SOURCE-ART")
        self.assertEqual(self.fetches, [])      # no network call needed

    def test_a_failed_fetch_falls_back_to_the_edited_play_art(self):
        ids = [self.play("a"), self.play("b")]
        with open(os.path.join(self.art_dir, f"{ids[0]}.jpg"), "wb") as f:
            f.write(b"SOURCE-ART")
        self.fetch_result = None                # download failed
        self.run_unify(ids, ids[0], "http://a/cover.jpg")
        self.assertEqual(self.art_of(ids[1]), b"SOURCE-ART")

    def test_no_art_anywhere_is_a_clean_no_op(self):
        # Nothing to copy and nothing to fetch: leave every row alone rather
        # than writing empty files over them.
        ids = [self.play("a"), self.play("b")]
        self.fetch_result = None
        self.assertEqual(self.run_unify(ids, ids[0], "http://a/cover.jpg"), [])
        self.assertIsNone(self.art_of(ids[0]))
        self.assertIsNone(self.art_of(ids[1]))

    def test_undecodable_image_falls_back_rather_than_raising(self):
        ids = [self.play("a"), self.play("b")]
        with open(os.path.join(self.art_dir, f"{ids[0]}.jpg"), "wb") as f:
            f.write(b"SOURCE-ART")
        self.fetch_result = b"this is not an image"
        self.run_unify(ids, ids[0], "http://a/cover.jpg")
        self.assertEqual(self.art_of(ids[1]), b"SOURCE-ART")

    def test_stored_art_is_a_thumbnail_not_the_original(self):
        ids = [self.play("a")]
        self.fetch_result = _png_bytes((200, 100, 50))
        self.run_unify(ids, ids[0], "http://a/cover.jpg")
        from PIL import Image

        with Image.open(io.BytesIO(self.art_of(ids[0]))) as img:
            self.assertLessEqual(max(img.size), 64)
            self.assertEqual(img.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
