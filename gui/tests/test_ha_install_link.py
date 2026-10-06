"""The way from SpinSense to its Home Assistant integration.

The wizard's Home Assistant step used to say "install the HACS integration"
and stop, leaving the user to find a repository whose name they had not been
given. It now carries a My Home Assistant link, which opens HACS on the user's
own instance with the repository filled in.

A link like that fails silently. A wrong owner or repository still opens Home
Assistant and still opens HACS — on a page that says the repository does not
exist — so nothing here would notice. These pin the address, in the three
places it is written: the wizard, Settings, and the README.
"""
import html
import json
import os
import re
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI_DIR)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from fastapi.testclient import TestClient  # noqa: E402

import backend_main  # noqa: E402
import config_manager  # noqa: E402

# The repository the README tells people to add by hand. The link must open
# this one and no other.
REPOSITORY = "https://github.com/ycsgc1/homeassistant-spinsense"

_ANCHOR_RE = re.compile(r"<a\b[^>]*>")
_HREF_RE = re.compile(r'\bhref="([^"]*)"')


def anchors(page: str) -> list[tuple[str, str]]:
    """(href, whole tag) for every link on the page, entities decoded."""
    found = []
    for tag in _ANCHOR_RE.findall(page):
        href = _HREF_RE.search(tag)
        if href:
            found.append((html.unescape(href.group(1)), tag))
    return found


class InstallLinkTest(unittest.TestCase):
    def setUp(self):
        fd, self.cfg_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.cfg_path, "w") as f:
            json.dump({"System": {"Setup_Wizard_State": "completed"}}, f)
        self._orig_cfg = config_manager.CONFIG_PATH
        config_manager.CONFIG_PATH = self.cfg_path
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        config_manager.CONFIG_PATH = self._orig_cfg
        try:
            os.remove(self.cfg_path)
        except OSError:
            pass

    def page(self, path: str) -> str:
        r = self.client.get(path, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        return r.text

    def install_links(self, path: str) -> list[tuple[str, str]]:
        return [(href, tag) for href, tag in anchors(self.page(path))
                if "my.home-assistant.io" in href]

    # --- the address ---

    def test_it_opens_the_hacs_repository_page(self):
        url = urlsplit(backend_main.HA_INSTALL_URL)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "my.home-assistant.io")
        self.assertEqual(url.path, "/redirect/hacs_repository/")

    def test_it_names_the_repository_the_readme_names(self):
        query = parse_qs(urlsplit(backend_main.HA_INSTALL_URL).query)
        self.assertEqual(
            f"https://github.com/{query['owner'][0]}/{query['repository'][0]}",
            REPOSITORY)
        # HACS files a repository by kind, and offers this one as nothing
        # unless it is told the kind.
        self.assertEqual(query["category"], ["integration"])

    def test_the_readme_gives_the_same_link_and_the_same_repository(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        self.assertIn(backend_main.HA_INSTALL_URL, readme)
        self.assertIn(REPOSITORY, readme)

    # --- where it is offered ---

    def test_the_wizard_offers_it_on_the_home_assistant_step(self):
        page = self.page("/setup")
        step = page[page.index('data-step="3"', page.index("wizard-step")):]
        step = step[:step.index("</section>")]
        self.assertIn("Connect to Home Assistant", step)
        links = [href for href, _ in anchors(step) if "my.home-assistant.io" in href]
        self.assertEqual(links, [backend_main.HA_INSTALL_URL])

    def test_settings_offers_it_too(self):
        self.assertEqual([href for href, _ in self.install_links("/settings")],
                         [backend_main.HA_INSTALL_URL])

    def test_hacs_itself_is_linked_for_anyone_without_it(self):
        for path in ("/setup", "/settings"):
            with self.subTest(page=path):
                self.assertIn(backend_main.HACS_URL,
                              [href for href, _ in anchors(self.page(path))])

    # --- leaving the page ---

    def test_a_link_that_leaves_spinsense_opens_a_new_tab(self):
        # The wizard keeps its progress in the page. Following a link in the
        # same tab would throw away a calibration to go and install something.
        for path in ("/setup", "/settings"):
            for href, tag in anchors(self.page(path)):
                if not href.startswith("http"):
                    continue
                with self.subTest(page=path, href=href):
                    self.assertIn('target="_blank"', tag)
                    self.assertIn("noopener", tag)


if __name__ == "__main__":
    unittest.main()
