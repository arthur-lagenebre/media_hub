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
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_a_choice_is_kept_for_that_account_only(self):
        self.assertEqual(prefs.save(self.db, "u1", {"music_view": "artists"}), {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})
        self.assertEqual(prefs.load(self.db, "u2"), {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_each_setting_is_kept_on_its_own(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        self.assertEqual(prefs.save(self.db, "u1", {"movie_view": "films"}), {"music_view": "artists", "movie_view": "films", "home_latest": "show", "home_unseen": "show"})
        self.assertEqual(prefs.save(self.db, "u1", {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"}), {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_the_two_shelves_of_the_home_are_shown_unless_the_account_hides_them_one_by_one(self):
        self.assertEqual({k: v for k, v in prefs.load(self.db, "u1").items() if k.startswith("home_")}, {"home_latest": "show", "home_unseen": "show"})
        self.assertEqual(prefs.save(self.db, "u1", {"home_unseen": "hide"})["home_latest"], "show")                 # chacun a son réglage
        loaded = prefs.load(self.db, "u1")
        self.assertEqual((loaded["home_latest"], loaded["home_unseen"]), ("show", "hide"))
        self.assertEqual(prefs.load(self.db, "u2")["home_unseen"], "show")
        with self.assertRaises(ValueError):
            prefs.save(self.db, "u1", {"home_latest": "peut-être"})

    def test_a_new_choice_replaces_the_old_one(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        self.assertEqual(prefs.save(self.db, "u1", {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"}), {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_an_unknown_setting_or_value_is_refused_and_changes_nothing(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        for changes, message in (({"couleur": "rouge"}, "réglage inconnu : couleur"), ({"music_view": "mosaique"}, "valeur inconnue pour music_view : mosaique"), ({"movie_view": "albums"}, "valeur inconnue pour movie_view : albums"),
                                 ({"music_view": ["artists"]}, "valeur inconnue pour music_view"), ({}, "aucun réglage à changer"), ("artists", "aucun réglage à changer"),
                                 ({"music_view": "artists", "couleur": "rouge"}, "réglage inconnu : couleur")):
            with self.assertRaisesRegex(ValueError, message):
                prefs.save(self.db, "u1", changes)
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "artists", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_a_stored_value_that_is_no_longer_allowed_falls_back_to_the_default(self):
        self.db.kv_set(prefs.KEY + "u1", {"music_view": "ancienne", "autre": 1})
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})

    def test_a_damaged_value_is_ignored(self):
        self.db.kv_set(prefs.KEY + "u1", "pas un dictionnaire")
        self.assertEqual(prefs.load(self.db, "u1"), {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show"})


if __name__ == "__main__":
    unittest.main()
