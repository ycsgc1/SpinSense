"""Every constant a page script uses is one it declares.

Removing MQTT took `const MDNS_ENABLED = …` out of setup.js along with the MQTT
handles around it, and left the two lines that read it. Nothing failed: the
page still loaded, and the first read sits inside a try/catch that only logs.
The second is in the function that builds the config to save, so **Save and
finish** and **Skip** both threw a ReferenceError before sending anything — and
because every page redirects to /setup until the wizard has been saved, a fresh
install could not get past it.

CI runs ESLint over these scripts (`lint-js` in .github/workflows/tests.yml),
which catches this properly — any undefined name, in scope. This is the same
check for wherever the Python suites run and Node does not: lexical, not an
execution. The scripts keep their page-level handles in ALL_CAPS constants, and
each one a script mentions has to be declared in that same script. It is
deliberately narrow — it knows nothing about scope and does not look at
camelCase names — but it is exact about the mistake that shipped.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(os.path.dirname(HERE), "static")

# ALL_CAPS names the browser provides rather than the script.
BROWSER_GLOBALS = {"JSON"}

_CONSTANT = r"[A-Z][A-Z0-9_]{2,}"
# A use: the bare name, not a property access (`dbUtil.FLOOR_DB`).
_USED_RE = re.compile(r"(?<![.\w$])(" + _CONSTANT + r")\b")
_DECLARED_RE = re.compile(r"\b(?:const|let|var)\s+(" + _CONSTANT + r")\b")
# A key in an object literal (`{ FLOOR_DB: -120 }`) names nothing in scope.
_OBJECT_KEY_RE = re.compile(r"[{,]\s*(" + _CONSTANT + r")\s*:")

# A `/` after one of these starts a regex literal; after anything else it divides.
_REGEX_MAY_FOLLOW = set("(,=:[!&|?{;+-*%<>~^")


def code_only(src: str, start: int = 0, in_template: bool = False):
    """`src` with comments, strings and regex literals blanked out.

    What is left is only the text an identifier can meaningfully appear in.
    Template literals lose their text but keep their `${…}` expressions, since
    those are code. Returns `(code, index after the last character consumed)`;
    with `in_template` it stops at the `}` that closes the expression.
    """
    out, i, n, depth, prev = [], start, len(src), 0, ""
    while i < n:
        ch, two = src[i], src[i:i + 2]
        if two == "//":
            end = src.find("\n", i)
            i = n if end == -1 else end
        elif two == "/*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch in "\"'":
            i += 1
            while i < n and src[i] != ch:
                i += 2 if src[i] == "\\" else 1
            i += 1
            out.append('""')
            prev = '"'
        elif ch == "`":
            i += 1
            while i < n and src[i] != "`":
                if src[i] == "\\":
                    i += 2
                elif src[i:i + 2] == "${":
                    inner, i = code_only(src, i + 2, in_template=True)
                    out.append(f" {inner} ")
                else:
                    i += 1
            i += 1
            out.append('""')
            prev = '"'
        elif ch == "/" and (prev in _REGEX_MAY_FOLLOW or prev == ""
                            or "".join(out).rstrip().endswith("return")):
            i += 1
            in_class = False
            while i < n and (in_class or src[i] != "/"):
                if src[i] == "\\":
                    i += 1
                elif src[i] == "[":
                    in_class = True
                elif src[i] == "]":
                    in_class = False
                i += 1
            i += 1
            out.append('""')
            prev = '"'
        else:
            if in_template and ch == "{":
                depth += 1
            elif in_template and ch == "}":
                if depth == 0:
                    return "".join(out), i + 1
                depth -= 1
            out.append(ch)
            if not ch.isspace():
                prev = ch
            i += 1
    return "".join(out), i


def undeclared_constants(src: str) -> set[str]:
    code, _ = code_only(src)
    used = set(_USED_RE.findall(code))
    declared = set(_DECLARED_RE.findall(code))
    keys = set(_OBJECT_KEY_RE.findall(code))
    return used - declared - keys - BROWSER_GLOBALS


def scripts() -> list[str]:
    return sorted(n for n in os.listdir(STATIC_DIR) if n.endswith(".js"))


class ScriptConstantsTest(unittest.TestCase):
    def test_there_are_scripts_to_check(self):
        # Guards the guard: a moved directory would otherwise pass vacuously.
        self.assertIn("setup.js", scripts())

    def test_every_constant_used_is_declared(self):
        for name in scripts():
            with self.subTest(script=name):
                with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
                    self.assertEqual(undeclared_constants(f.read()), set())

    def test_the_wizard_still_reads_the_discovery_toggle(self):
        # The line that went missing, named, so the failure says what broke.
        with open(os.path.join(STATIC_DIR, "setup.js"), encoding="utf-8") as f:
            declared = re.search(
                r'const MDNS_ENABLED = document\.getElementById\("wizard-mdns-enabled"\)',
                f.read())
        self.assertTrue(
            declared,
            "setup.js no longer declares MDNS_ENABLED; buildPayload() reads it, "
            "so the wizard cannot be saved or skipped without it")


class CheckerTest(unittest.TestCase):
    """The check is only worth having if it sees what it claims to."""

    def test_it_catches_a_constant_whose_declaration_was_removed(self):
        src = 'const A_BTN = 1;\nif (GONE_BTN) GONE_BTN.checked = true;\n'
        self.assertEqual(undeclared_constants(src), {"GONE_BTN"})

    def test_it_sees_into_template_expressions(self):
        self.assertEqual(undeclared_constants("x = `a ${MISSING_ROW} b`;"),
                         {"MISSING_ROW"})

    def test_names_in_strings_comments_and_regexes_are_not_uses(self):
        src = (
            '// MQTT_HOST was removed\n'
            '/* and MQTT_PORT too */\n'
            'const t = "SELECT"; const u = \'POST\';\n'
            's.replace(/[&<>"\']/g, "x"); const m = /(ABC)/.exec(s);\n'
            'const v = `HTTP ${t}`;\n'
        )
        self.assertEqual(undeclared_constants(src), set())

    def test_properties_and_object_keys_are_not_uses(self):
        src = 'const d = { FLOOR_DB: -120, OTHER_KEY: 1 }; d.FLOOR_DB; JSON.parse(d);'
        self.assertEqual(undeclared_constants(src), set())

    def test_a_division_is_not_mistaken_for_a_regex(self):
        # Otherwise everything up to the next slash would vanish from view.
        src = "const pct = (a - LOW_DB) / (b - LOW_DB); c = d / 2;"
        self.assertEqual(undeclared_constants(src), {"LOW_DB"})


if __name__ == "__main__":
    unittest.main()
