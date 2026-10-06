"""`write_atomically`: a file another process is reading is never seen half-written.

config.json has one writer that matters (the backend) and a reader in another
process (the engine, polling it). Written where it stands, the file is empty
for a moment on every save, and stays that way if the save is cut short — which
is how a power cut during "Save" used to cost every setting.

The property under test is the reader's: whatever moment it looks, it gets a
complete file. The rest pins what happens when replacing isn't possible.
"""
import errno
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spinsense import files  # noqa: E402
from spinsense.files import write_atomically  # noqa: E402


def os_error(code):
    return OSError(code, os.strerror(code))


class WriteAtomicallyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def read(self):
        with open(self.path) as f:
            return f.read()

    def put(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    # --- the ordinary case ---

    def test_it_creates_a_file_that_is_not_there(self):
        write_atomically(self.path, "new")
        self.assertEqual(self.read(), "new")

    def test_it_replaces_what_was_there(self):
        self.put("old and rather longer than its replacement")
        write_atomically(self.path, "new")
        self.assertEqual(self.read(), "new")

    def test_nothing_is_left_beside_the_file(self):
        write_atomically(self.path, "one")
        write_atomically(self.path, "two")
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_the_file_is_a_new_one_not_the_old_one_refilled(self):
        # What makes it atomic: a reader holding the old file open keeps
        # reading the old contents, whole.
        self.put("old")
        with open(self.path) as held_open:
            write_atomically(self.path, "new")
            self.assertEqual(held_open.read(), "old")
        self.assertEqual(self.read(), "new")

    def test_a_reader_never_sees_part_of_a_file(self):
        # The engine's side of it. Every read must parse and be one of the two
        # documents being written — never empty, never a prefix.
        small = json.dumps({"n": 1})
        large = json.dumps({"n": 2, "pad": "x" * 200_000})
        self.put(small)
        done = threading.Event()
        seen_wrong = []

        def reader():
            while not done.is_set():
                with open(self.path) as f:
                    text = f.read()
                if text not in (small, large):
                    seen_wrong.append(len(text))
                    return

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(300):
                write_atomically(self.path, large if i % 2 else small)
        finally:
            done.set()
            thread.join()
        self.assertEqual(seen_wrong, [], "a reader saw a partly written file")

    def test_writers_at_once_each_leave_a_whole_file(self):
        # The backend saves from request threads; two saves may overlap.
        texts = [json.dumps({"writer": n, "pad": str(n) * 50_000}) for n in range(8)]
        threads = [threading.Thread(target=write_atomically, args=(self.path, t))
                   for t in texts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(self.read(), texts)
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    # --- permissions ---

    def test_an_existing_files_permissions_are_kept(self):
        self.put("old")
        os.chmod(self.path, 0o600)
        write_atomically(self.path, "new")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_new_file_gets_the_mode_a_plain_write_would_give(self):
        plain = os.path.join(self.dir, "plain")
        with open(plain, "w") as f:
            f.write("x")
        write_atomically(self.path, "x")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode),
                         stat.S_IMODE(os.stat(plain).st_mode))

    # --- when it goes wrong ---

    def test_a_write_that_fails_leaves_the_original_alone(self):
        # A full disk, say. Rewriting in place would have emptied the file
        # first and then had nothing to fill it with.
        self.put("the settings")
        with mock.patch.object(files.os, "replace",
                               side_effect=os_error(errno.ENOSPC)):
            with self.assertRaises(OSError):
                write_atomically(self.path, "new")
        self.assertEqual(self.read(), "the settings")
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_a_file_that_cannot_be_replaced_is_rewritten_instead(self):
        # config.json bind-mounted into the container by itself is a mount
        # point, and rename(2) refuses those with EBUSY. Saving must still work.
        self.put("old")
        with mock.patch.object(files.os, "replace",
                               side_effect=os_error(errno.EBUSY)):
            write_atomically(self.path, "new")
        self.assertEqual(self.read(), "new")
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_a_directory_we_cannot_write_in_falls_back_to_the_file_itself(self):
        self.put("old")
        real_open = os.open

        def no_new_files(path, flags, *a, **kw):
            if flags & os.O_CREAT and str(path).endswith(".tmp"):
                raise os_error(errno.EACCES)
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(files.os, "open", side_effect=no_new_files):
            write_atomically(self.path, "new")
        self.assertEqual(self.read(), "new")

    def test_no_room_for_a_temporary_file_is_an_error_not_a_rewrite(self):
        # The same fallback on a full disk would destroy what it meant to save.
        self.put("the settings")
        real_open = os.open

        def disk_full(path, flags, *a, **kw):
            if flags & os.O_CREAT and str(path).endswith(".tmp"):
                raise os_error(errno.ENOSPC)
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(files.os, "open", side_effect=disk_full):
            with self.assertRaises(OSError):
                write_atomically(self.path, "new")
        self.assertEqual(self.read(), "the settings")

    def test_a_filesystem_without_fsync_still_saves(self):
        with mock.patch.object(files.os, "fsync",
                               side_effect=os_error(errno.EINVAL)):
            write_atomically(self.path, "new")
        self.assertEqual(self.read(), "new")


if __name__ == "__main__":
    unittest.main()
