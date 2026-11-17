"""Le journal des nouveautés (`hub/CHANGELOG.md`) : sa lecture, la route `GET /api/changelog` et l'accord du fichier avec `__version__`."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.rig import MiniHub, call
from hub import __version__, accounts, changelog


class TestParse(unittest.TestCase):
    def test_reads_versions_dates_and_items_newest_first(self):
        text = "## 0.31.0 — 2026-11-17\n- Nouveau.\n- Autre.\n\n## 0.30.0 — 2026-11-10\n- Ancien.\n"
        self.assertEqual(changelog.parse(text), [
            {"version": "0.31.0", "date": "2026-11-17", "items": ["Nouveau.", "Autre."]},
            {"version": "0.30.0", "date": "2026-11-10", "items": ["Ancien."]},
        ])

    def test_lines_outside_the_format_are_ignored(self):
        text = "# Nouveautés\n- avant tout titre\n## pas une version\n- orphelin\n## 1.2.3 — 2026-01-02\ntexte libre\n-   Gardé.  \n-\n* étoile\n## 1.2 — 2026-01-01\n- perdu\n"
        self.assertEqual(changelog.parse(text), [{"version": "1.2.3", "date": "2026-01-02", "items": ["Gardé."]}])

    def test_a_missing_file_gives_no_version(self):
        self.assertEqual(changelog.load(Path(tempfile.gettempdir()) / "absent-changelog.md"), [])

    def test_an_unreadable_file_gives_no_version(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "binaire.md").write_bytes(b"\xff\xfe\x00## 0.1.0")
        (root / "dossier.md").mkdir()
        self.assertEqual(changelog.load(root / "binaire.md"), [])
        self.assertEqual(changelog.load(root / "dossier.md"), [])

    def test_a_file_saved_with_a_byte_order_mark_keeps_its_first_version(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "CHANGELOG.md").write_text("## 0.2.0 — 2026-11-17\r\n- Nouveau.\r\n## 0.1.0 — 2026-11-01\r\n- Ancien.\r\n", encoding="utf-8-sig")
        self.assertEqual([v["version"] for v in changelog.load(root / "CHANGELOG.md")], ["0.2.0", "0.1.0"])

    def test_an_en_dash_or_a_hyphen_also_separates_the_date(self):
        text = "## 0.3.0 – 2026-11-17\n- a\n## 0.2.0 - 2026-11-10\n- b\n"
        self.assertEqual([v["version"] for v in changelog.parse(text)], ["0.3.0", "0.2.0"])

    def test_an_impossible_date_or_a_version_without_items_is_left_out(self):
        text = "## 0.3.0 — 2026-13-45\n- perdu\n## 0.2.0 — 2026-11-10\n## 0.1.0 — 2026-11-01\n- gardé\n"
        self.assertEqual(changelog.parse(text), [{"version": "0.1.0", "date": "2026-11-01", "items": ["gardé"]}])


class TestRoute(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.hub = MiniHub(self.root)

    def get(self, headers=None):
        return call(self.hub, "GET", "/api/changelog", headers=headers)[1]

    def test_the_route_returns_the_versions(self):
        reply = self.get()
        self.assertEqual(reply.status, 200)
        versions = reply.json()["versions"]
        self.assertTrue(versions)
        for version in versions:
            self.assertEqual(set(version), {"version", "date", "items"})
            self.assertTrue(all(isinstance(text, str) and text for text in version["items"]))

    def test_without_the_file_the_route_returns_no_version(self):
        with mock.patch.object(changelog, "FILE", self.root / "absent.md"):
            reply = self.get()
        self.assertEqual((reply.status, reply.json()), (200, {"versions": []}))

    def test_the_route_asks_for_an_account(self):
        row = accounts.create(self.hub.db, "root", "motdepasse12")
        self.assertEqual(self.get().status, 401)
        self.assertEqual(self.get(self.hub.cookie(row)).status, 200)


class TestFile(unittest.TestCase):
    def test_the_newest_entry_is_the_current_version(self):
        versions = changelog.load(changelog.FILE)
        self.assertTrue(versions, "hub/CHANGELOG.md est absent ou illisible")
        self.assertEqual(versions[0]["version"], __version__, "ajoute l'entrée de la nouvelle version en tête de hub/CHANGELOG.md")
        self.assertTrue(versions[0]["items"], "l'entrée de la version courante n'a aucun point")


if __name__ == "__main__":
    unittest.main()
