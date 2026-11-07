"""Les pop-up de l'interface : un seul gabarit, quatre déclinaisons (docs/popups.md).

Le test statique tourne partout ; celui du navigateur est sauté sans Playwright ou ffmpeg.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests import fixtures
from tests.test_ui import UiCase

JS = Path(__file__).resolve().parent.parent / "hub" / "web" / "js"
KINDS = ("choice", "confirm", "form", "wide")


def sources():
    return {p.name: p.read_text(encoding="utf-8") for p in JS.glob("*.js") if p.name != "lang-en.js"}


class TestPopupsShareOneTemplate(unittest.TestCase):
    def test_only_the_shared_builder_makes_a_dialog(self):
        """Un pop-up qui construit lui-même son `div role="dialog"` échappe aux quatre déclinaisons : il passe par H.box."""
        for name, text in sources().items():
            builds = len(re.findall(r"role: 'dialog'", text)) + len(re.findall(r"class: '(?:menu|veil)[ ']", text))
            self.assertEqual(builds, 3 if name == "core.js" else 0, f"{name} construit un pop-up à la main : utiliser H.menu, H.confirm, H.form, H.prompt ou H.box")   # core.js : le voile, le gabarit et son role

    def test_every_box_names_one_of_the_four_kinds(self):
        found = 0
        for name, text in sources().items():
            for kind in re.findall(r"H\.box\('([^']*)'", text):
                found += 1
                self.assertIn(kind.split()[0], KINDS, f"{name} : H.box('{kind}') n'est pas une des déclinaisons {KINDS}")
        self.assertGreaterEqual(found, 5)

    def test_an_overlay_only_takes_a_box_from_the_shared_builder(self):
        """`H.overlay.open(x)` : x vient de H.box (la fenêtre est une variable `box`)."""
        for name, text in sources().items():
            for line in text.splitlines():
                if "H.overlay.open(" in line and name != "core.js":
                    self.assertIn("H.overlay.open(box", line, f"{name} : {line.strip()}")


class TestPopupsInTheBrowser(UiCase):
    def kind(self):
        """Les déclinaisons de la fenêtre du dessus : exactement une."""
        self.page.wait_for_selector(".veil > .menu")
        names = self.page.evaluate("Array.prototype.slice.call(document.querySelector('.veil > .menu').classList)")
        found = [k for k in names if k in KINDS]
        self.assertEqual(len(found), 1, names)
        return found[0]

    def test_a_choice_a_confirmation_a_form_and_a_wide_window(self):
        self.open("#/l/films")
        self.view(".tools .btn").first.click()
        self.assertEqual(self.kind(), "choice")
        self.press("Escape", wait=300)

        self.open("#/admin")
        self.view("[data-key=junk-del]").click()
        self.assertEqual(self.kind(), "confirm")
        self.assertIn("Annuler", self.focused())                      # le focus part sur la sortie sûre : Entrée ne détruit rien
        self.assertEqual(self.page.locator(".menu.confirm .btn.danger").count(), 1)
        self.press("Escape", wait=300)

        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".trk").first.wait_for()
        self.view(".actions .btn", has_text="Playlist").click()
        self.page.locator(".veil .opt", has_text="Nouvelle playlist").click()
        self.assertEqual(self.kind(), "form")
        self.press("Escape", wait=300)

        self.open("#/l/musique")
        card = self.view(".grid .card", has_text="Discovery")
        card.hover()
        card.locator(".qbtn").first.click()
        self.page.wait_for_selector("#abar:not([hidden])")
        self.page.locator("#abar [aria-label=\"File d’attente\"]").click()
        self.assertEqual(self.kind(), "wide")


if __name__ == "__main__":
    unittest.main()
