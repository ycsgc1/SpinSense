"""Every field on the Settings form is a setting that exists.

The form has no list of its own fields: settings.js walks whatever carries a
`name`, reads the saved value at that dotted path, and writes the edited one
back to the same path. So the template *is* the mapping, and nothing checks it.
A name that matches no setting loads blank and saves a key the schema drops —
a control that looks as if it works and changes nothing.

Home Assistant discovery is pinned by name because it was missing: the toggle
lived only in the setup wizard, so switching it meant running the wizard again.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import config_manager  # noqa: E402

_FIELD_RE = re.compile(r'<(input|select|textarea)\b[^>]*\bname="([^"]+)"[^>]*>')


def form_fields() -> dict:
    """{dotted name: the tag that carries it} for templates/settings.html."""
    with open(os.path.join(GUI_DIR, "templates", "settings.html")) as f:
        html = f.read()
    return {m.group(2): m.group(0) for m in _FIELD_RE.finditer(html)}


def lookup(config: dict, dotted: str):
    """The value at `dotted`, or KeyError if any part of the path is missing."""
    value = config
    for part in dotted.split("."):
        if not isinstance(value, dict):
            raise KeyError(dotted)
        value = value[part]
    return value


class SettingsFormTest(unittest.TestCase):
    def setUp(self):
        self.fields = form_fields()
        self.defaults = config_manager.get_default_config()

    def test_the_form_has_fields(self):
        # Guards the regex as much as the template: an empty result would make
        # every other check here pass without looking at anything.
        self.assertGreater(len(self.fields), 10)

    def test_every_field_names_a_real_setting(self):
        for name in self.fields:
            with self.subTest(field=name):
                try:
                    lookup(self.defaults, name)
                except KeyError:
                    self.fail(f'settings.html has name="{name}", which is not '
                              "a path in the config schema")

    def test_a_checkbox_is_a_boolean_setting_and_nothing_else_is(self):
        # settings.js sends `.checked` for a checkbox and `.value` otherwise,
        # so the wrong control type saves the wrong type — which the schema
        # then coerces or refuses, depending on the field.
        for name, tag in self.fields.items():
            with self.subTest(field=name):
                is_checkbox = 'type="checkbox"' in tag
                is_bool = isinstance(lookup(self.defaults, name), bool)
                self.assertEqual(is_checkbox, is_bool)

    def test_home_assistant_discovery_is_on_the_settings_page(self):
        self.assertIn("Discovery.mDNS.Enabled", self.fields)

    def test_no_credential_the_server_keeps_is_a_form_field(self):
        # The Last.fm connection is written by the auth flow and restored over
        # whatever a form posts; a field for it would be a control that cannot
        # work. (The AudD token is a form field, and is meant to be.)
        for name in ("LastFM.Session_Key", "LastFM.API_Secret", "LastFM.API_Key",
                     "LastFM.Username", "LastFM.Scrobble_Since"):
            with self.subTest(field=name):
                self.assertNotIn(name, self.fields)


if __name__ == "__main__":
    unittest.main()
