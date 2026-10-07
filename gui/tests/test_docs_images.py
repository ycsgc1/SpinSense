"""Every picture the documentation points at is there, and none is left over.

The screenshots are referenced by filename from two documents, in two syntaxes
— Markdown images and `<img>` tags sized for the smaller shots — and nothing
renders either document before it is published. A renamed file shows up as a
broken image on the repository's front page; a retired one just sits in the
tree, describing a screen that no longer exists. That second kind is how the
README went on showing an MQTT toggle after MQTT had been removed.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
IMAGES_DIR = os.path.join(ROOT, "docs", "images")

# The documents that show screenshots, as paths from the repository root.
DOCUMENTS = ("README.md", os.path.join("docs", "CONFIGURATION.md"))

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")

_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_HTML_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"')


def referenced(document: str) -> list[str]:
    """Absolute paths of the local images `document` shows."""
    path = os.path.join(ROOT, document)
    with open(path) as f:
        text = f.read()
    found = _MARKDOWN_RE.findall(text) + _HTML_RE.findall(text)
    return [os.path.normpath(os.path.join(os.path.dirname(path), target))
            for target in found if not target.startswith(("http://", "https://"))]


def all_referenced() -> set[str]:
    return {path for document in DOCUMENTS for path in referenced(document)}


class DocsImagesTest(unittest.TestCase):
    def test_the_documents_do_show_pictures(self):
        # Guards the two patterns: an empty result would pass everything below.
        for document in DOCUMENTS:
            with self.subTest(document=document):
                self.assertGreater(len(referenced(document)), 3)

    def test_every_picture_referenced_exists(self):
        for document in DOCUMENTS:
            for path in referenced(document):
                with self.subTest(document=document,
                                  image=os.path.relpath(path, ROOT)):
                    self.assertTrue(os.path.isfile(path))

    def test_every_picture_referenced_is_spelled_as_it_is_on_disk(self):
        # A checkout on macOS or Windows finds `stats_page.png` when the file
        # is `Stats_Page.png`. GitHub, serving the README, does not.
        on_disk = set(os.listdir(IMAGES_DIR))
        for path in all_referenced():
            if os.path.dirname(path) != IMAGES_DIR:
                continue
            with self.subTest(image=os.path.basename(path)):
                self.assertIn(os.path.basename(path), on_disk)

    def test_no_picture_in_the_folder_has_stopped_being_used(self):
        used = {os.path.basename(p) for p in all_referenced()
                if os.path.dirname(p) == IMAGES_DIR}
        for name in sorted(os.listdir(IMAGES_DIR)):
            if not name.lower().endswith(IMAGE_TYPES):
                continue        # the folder's own README, and anything not a picture
            with self.subTest(image=name):
                self.assertIn(name, used,
                              "not shown by the README or the configuration "
                              "reference — use it or remove it")

    def test_the_folders_index_lists_what_is_in_it(self):
        with open(os.path.join(IMAGES_DIR, "README.md")) as f:
            index = f.read()
        for name in sorted(os.listdir(IMAGES_DIR)):
            if name.lower().endswith(IMAGE_TYPES):
                with self.subTest(image=name):
                    self.assertIn(f"`{name}`", index)


if __name__ == "__main__":
    unittest.main()
