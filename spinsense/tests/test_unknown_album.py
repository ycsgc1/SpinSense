"""The "no album" marker has one definition, and its value is a file format.

When no album can be found for a play, the engine files it under the string in
`albums.UNKNOWN_ALBUM`. Stats leave such plays out of the album charts,
reconciliation treats them as open questions, and scrobbling sends no album at
all — each by comparing against that string. It used to be written out in five
places, where one edit to one of them would have quietly made "Unknown Album"
a record in somebody's top ten.
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spinsense import albums  # noqa: E402

PACKAGES = ("core", "gui", "spinsense")
HOME = os.path.join("spinsense", "albums.py")


def source_files():
    for package in PACKAGES:
        for folder, dirs, names in os.walk(os.path.join(ROOT, package)):
            dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
            for name in names:
                if name.endswith(".py"):
                    yield os.path.join(folder, name)


def docstrings(tree) -> set:
    """ids of the string nodes that are docstrings: prose may name the marker."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                found.add(id(body[0].value))
    return found


def literal_uses(path: str) -> list[int]:
    """Line numbers of string literals in `path` that spell the marker out."""
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    prose = docstrings(tree)
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and albums.UNKNOWN_ALBUM in node.value and id(node) not in prose]


class UnknownAlbumTest(unittest.TestCase):
    def test_the_value_is_the_one_already_in_every_database(self):
        # Not a tautology: this string is stored in rows that exist. Changing
        # the constant would not rename them, it would stop recognising them.
        self.assertEqual(albums.UNKNOWN_ALBUM, "Unknown Album")

    def test_the_scan_sees_the_definition(self):
        # So that an empty result below means "none", not "looked at nothing".
        self.assertEqual(len(literal_uses(os.path.join(ROOT, HOME))), 1)

    def test_no_other_module_spells_it_out(self):
        for path in source_files():
            if path.endswith(HOME):
                continue
            with self.subTest(module=os.path.relpath(path, ROOT)):
                self.assertEqual(
                    literal_uses(path), [],
                    "use spinsense.albums.UNKNOWN_ALBUM instead of the literal")


if __name__ == "__main__":
    unittest.main()
