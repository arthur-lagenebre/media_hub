"""Les réglages d'un compte : ce qu'il choisit d'afficher, gardé côté serveur pour le suivre d'un appareil à l'autre."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hub import prefs
from hub.db import Database


class TestPrefs(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")

    def test_an_account_without_a_choice_gets_the_defaults(self):
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "albums"})

    def test_a_choice_is_kept_for_that_account_only(self):
        self.assertEqual(prefs.save(self.db, "u1", {"music_view": "artists"}), {"music_view": "artists"})
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "artists"})
        self.assertEqual(prefs.load(self.db, "u2"), {"music_view": "albums"})

    def test_a_new_choice_replaces_the_old_one(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        self.assertEqual(prefs.save(self.db, "u1", {"music_view": "albums"}), {"music_view": "albums"})

    def test_an_unknown_setting_or_value_is_refused_and_changes_nothing(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        for changes, message in (({"couleur": "rouge"}, "réglage inconnu : couleur"), ({"music_view": "mosaique"}, "valeur inconnue pour music_view : mosaique"),
                                 ({"music_view": ["artists"]}, "valeur inconnue pour music_view"), ({}, "aucun réglage à changer"), ("artists", "aucun réglage à changer"),
                                 ({"music_view": "artists", "couleur": "rouge"}, "réglage inconnu : couleur")):
            with self.assertRaisesRegex(ValueError, message):
                prefs.save(self.db, "u1", changes)
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "artists"})

    def test_a_stored_value_that_is_no_longer_allowed_falls_back_to_the_default(self):
        self.db.kv_set(prefs.KEY + "u1", {"music_view": "ancienne", "autre": 1})
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "albums"})

    def test_a_damaged_value_is_ignored(self):
        self.db.kv_set(prefs.KEY + "u1", "pas un dictionnaire")
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "albums"})


if __name__ == "__main__":
    unittest.main()
