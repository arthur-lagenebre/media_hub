"""Les réglages d'un compte : ce qu'il choisit d'afficher, gardé côté serveur pour le suivre d'un appareil à l'autre."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hub import prefs
from hub.db import Database

ALL = {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_shelf_rows": "2", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show", "home_recommend": "show", "hide_spoilers": "show", "audio_langs": [], "sub_langs": [], "sub_mode": "forced"}      # les réglages d'un compte qui n'a rien choisi


class PrefsCase(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.db = Database(root / "hub.db")


class TestPrefs(PrefsCase):
    def test_an_account_without_a_choice_gets_the_defaults(self):
        self.assertEqual(prefs.load(self.db, "u1"), ALL)

    def test_a_choice_is_kept_for_that_account_only(self):
        self.assertEqual(prefs.save(self.db, "u1", {"music_view": "artists"}), {**ALL, "music_view": "artists"})
        self.assertEqual(prefs.load(self.db, "u1"), {**ALL, "music_view": "artists"})
        self.assertEqual(prefs.load(self.db, "u2"), ALL)

    def test_each_setting_is_kept_on_its_own(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        self.assertEqual(prefs.save(self.db, "u1", {"movie_view": "films"}), {**ALL, "music_view": "artists", "movie_view": "films"})
        self.assertEqual(prefs.save(self.db, "u1", ALL), ALL)

    def test_the_two_shelves_of_the_home_are_shown_unless_the_account_hides_them_one_by_one(self):
        self.assertEqual({k: v for k, v in prefs.load(self.db, "u1").items() if k.startswith("home_")}, {"home_latest": "show", "home_unseen": "show", "home_shelf_rows": "2", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show", "home_recommend": "show"})
        self.assertEqual(prefs.save(self.db, "u1", {"home_unseen": "hide"})["home_latest"], "show")                 # chacun a son réglage
        loaded = prefs.load(self.db, "u1")
        self.assertEqual((loaded["home_latest"], loaded["home_unseen"]), ("show", "hide"))
        self.assertEqual(prefs.load(self.db, "u2")["home_unseen"], "show")
        with self.assertRaises(ValueError):
            prefs.save(self.db, "u1", {"home_latest": "peut-être"})

    def test_a_new_choice_replaces_the_old_one(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        self.assertEqual(prefs.save(self.db, "u1", ALL), ALL)

    def test_an_unknown_setting_or_value_is_refused_and_changes_nothing(self):
        prefs.save(self.db, "u1", {"music_view": "artists"})
        for changes, message in (({"couleur": "rouge"}, "réglage inconnu : couleur"), ({"music_view": "mosaique"}, "valeur inconnue pour music_view : mosaique"), ({"movie_view": "albums"}, "valeur inconnue pour movie_view : albums"),
                                 ({"music_view": ["artists"]}, "valeur inconnue pour music_view"), ({}, "aucun réglage à changer"), ("artists", "aucun réglage à changer"),
                                 ({"music_view": "artists", "couleur": "rouge"}, "réglage inconnu : couleur")):
            with self.assertRaisesRegex(ValueError, message):
                prefs.save(self.db, "u1", changes)
        self.assertEqual(prefs.load(self.db, "u1"), {**ALL, "music_view": "artists"})

    def test_a_stored_value_that_is_no_longer_allowed_falls_back_to_the_default(self):
        self.db.kv_set(prefs.KEY + "u1", {"music_view": "ancienne", "autre": 1})
        self.assertEqual(prefs.load(self.db, "u1"), ALL)

    def test_the_order_of_the_unseen_shelf_is_random_unless_the_account_picks_rating_or_added(self):
        self.assertEqual(prefs.load(self.db, "u1")["home_unseen_sort"], "random")
        for choice in ("rating", "added", "random"):
            self.assertEqual(prefs.save(self.db, "u1", {"home_unseen_sort": choice})["home_unseen_sort"], choice)
        prefs.save(self.db, "u1", {"home_unseen_sort": "rating"})
        self.assertEqual(prefs.load(self.db, "u2")["home_unseen_sort"], "random")             # par compte
        with self.assertRaisesRegex(ValueError, "valeur inconnue pour home_unseen_sort : titre"):
            prefs.save(self.db, "u1", {"home_unseen_sort": "titre"})
        self.assertEqual(prefs.load(self.db, "u1")["home_unseen_sort"], "rating")

    def test_a_damaged_value_is_ignored(self):
        self.db.kv_set(prefs.KEY + "u1", "pas un dictionnaire")
        self.assertEqual(prefs.load(self.db, "u1"), ALL)

    def test_unseen_episodes_are_shown_unless_the_account_hides_them(self):
        self.assertEqual(prefs.load(self.db, "u1")["hide_spoilers"], "show")
        self.assertEqual(prefs.save(self.db, "u1", {"hide_spoilers": "hide"})["hide_spoilers"], "hide")
        self.assertEqual(prefs.load(self.db, "u1")["hide_spoilers"], "hide")
        self.assertEqual(prefs.load(self.db, "u2")["hide_spoilers"], "show")                  # par compte
        with self.assertRaisesRegex(ValueError, "valeur inconnue pour hide_spoilers : flou"):
            prefs.save(self.db, "u1", {"hide_spoilers": "flou"})
        self.assertEqual(prefs.load(self.db, "u1")["hide_spoilers"], "hide")

    def test_the_recommendations_are_shown_unless_the_account_hides_them(self):
        self.assertEqual(prefs.load(self.db, "u1")["home_recommend"], "show")
        self.assertEqual(prefs.save(self.db, "u1", {"home_recommend": "hide"})["home_recommend"], "hide")
        self.assertEqual(prefs.load(self.db, "u2")["home_recommend"], "show")                 # par compte
        with self.assertRaisesRegex(ValueError, "valeur inconnue pour home_recommend : peut-être"):
            prefs.save(self.db, "u1", {"home_recommend": "peut-être"})
        self.assertEqual(prefs.load(self.db, "u1")["home_recommend"], "hide")


class TestShelfRows(PrefsCase):
    """« Rangées par rayon » : le réglage commun aux rayons de l'accueil, qui reprend sans écrire en base l'ancien choix en nombre de cartes."""

    def rows_with(self, **kept):
        self.db.kv_set(prefs.KEY + "u1", kept)
        return prefs.load(self.db, "u1")["home_shelf_rows"]

    def test_two_rows_by_default(self):
        self.assertEqual(prefs.load(self.db, "u1")["home_shelf_rows"], "2")

    def test_one_two_or_three_rows_can_be_chosen_for_that_account_only(self):
        for rows in ("1", "2", "3"):
            self.assertEqual(prefs.save(self.db, "u1", {"home_shelf_rows": rows})["home_shelf_rows"], rows)
            self.assertEqual(prefs.load(self.db, "u1")["home_shelf_rows"], rows)
        self.assertEqual(prefs.load(self.db, "u2")["home_shelf_rows"], "2")

    def test_any_other_number_of_rows_is_refused_and_changes_nothing(self):
        prefs.save(self.db, "u1", {"home_shelf_rows": "3"})
        for value in ("0", "4", "", 2, None, ["2"]):
            with self.assertRaisesRegex(ValueError, "valeur inconnue pour home_shelf_rows"):
                prefs.save(self.db, "u1", {"home_shelf_rows": value})
        self.assertEqual(prefs.load(self.db, "u1")["home_shelf_rows"], "3")

    def test_the_old_number_of_cards_gives_the_rows(self):
        for cards, rows in (("6", "1"), ("12", "2"), ("18", "3"), ("24", "3")):
            with self.subTest(cards=cards):
                self.assertEqual(self.rows_with(home_unseen_count=cards), rows)

    def test_taking_over_the_old_choice_writes_nothing_and_keeps_it(self):
        self.db.kv_set(prefs.KEY + "u1", {"home_unseen_count": "18"})
        self.assertEqual(prefs.load(self.db, "u1")["home_unseen_count"], "18")
        self.assertEqual(self.db.kv_get(prefs.KEY + "u1"), {"home_unseen_count": "18"})

    def test_saved_rows_win_over_the_old_number_of_cards(self):
        self.assertEqual(self.rows_with(home_unseen_count="24", home_shelf_rows="1"), "1")
        prefs.save(self.db, "u1", {"home_unseen_count": "6"})
        self.assertEqual(prefs.load(self.db, "u1")["home_shelf_rows"], "1")

    def test_damaged_rows_fall_back_on_the_old_number_of_cards(self):
        self.assertEqual(self.rows_with(home_unseen_count="6", home_shelf_rows="9"), "1")

    def test_damaged_values_everywhere_give_the_default(self):
        for damaged in ("9", ["12"], {"a": 1}, None):
            with self.subTest(damaged=damaged):
                self.assertEqual(self.rows_with(home_unseen_count=damaged, home_shelf_rows=damaged), "2")


class TestLanguagePrefs(PrefsCase):
    def test_without_a_choice_the_file_decides_the_audio_and_only_forced_subtitles_are_shown(self):
        loaded = prefs.load(self.db, "u1")
        self.assertEqual((loaded["audio_langs"], loaded["sub_langs"], loaded["sub_mode"]), ([], [], "forced"))

    def test_ordered_lists_of_languages_are_kept_for_that_account_only(self):
        saved = prefs.save(self.db, "u1", {"audio_langs": ["ja", "fr"], "sub_langs": ["fr", "en"], "sub_mode": "foreign"})
        self.assertEqual((saved["audio_langs"], saved["sub_langs"], saved["sub_mode"]), (["ja", "fr"], ["fr", "en"], "foreign"))
        self.assertEqual(prefs.load(self.db, "u1"), saved)
        self.assertEqual(prefs.load(self.db, "u2"), ALL)
        self.assertEqual(prefs.save(self.db, "u1", {"audio_langs": []})["audio_langs"], [])             # vidée : le fichier redécide
        self.assertEqual(prefs.load(self.db, "u1")["sub_langs"], ["fr", "en"])

    def test_every_subtitle_mode_is_accepted(self):
        for mode in ("never", "forced", "foreign", "always"):
            self.assertEqual(prefs.save(self.db, "u1", {"sub_mode": mode})["sub_mode"], mode)

    def test_a_bad_list_is_refused_and_changes_nothing(self):
        prefs.save(self.db, "u1", {"audio_langs": ["en"], "sub_langs": ["fr"]})
        bad = ("fr", None, ["fre"], ["FR"], ["xx"], ["und"], [""], ["fr", "fr"], [1], [["fr"]], ["fr", "en", "es", "de", "it", "ja", "pt", "nl", "sv"])
        for value in bad:
            for name in ("audio_langs", "sub_langs"):
                with self.assertRaisesRegex(ValueError, f"valeur inconnue pour {name}"):
                    prefs.save(self.db, "u1", {"sub_mode": "always", name: value})
        with self.assertRaisesRegex(ValueError, "valeur inconnue pour sub_mode : parfois"):
            prefs.save(self.db, "u1", {"audio_langs": ["fr"], "sub_mode": "parfois"})
        self.assertEqual(prefs.load(self.db, "u1"), {**ALL, "audio_langs": ["en"], "sub_langs": ["fr"]})

    def test_eight_languages_are_allowed(self):
        eight = ["fr", "en", "es", "de", "it", "ja", "pt", "nl"]
        self.assertEqual(prefs.save(self.db, "u1", {"sub_langs": eight})["sub_langs"], eight)

    def test_the_settings_kept_before_the_languages_still_load(self):
        self.db.kv_set(prefs.KEY + "u1", {"music_view": "artists", "home_unseen": "hide"})
        self.assertEqual(prefs.load(self.db, "u1"), {**ALL, "music_view": "artists", "home_unseen": "hide"})

    def test_a_damaged_stored_list_falls_back_to_the_default(self):
        self.db.kv_set(prefs.KEY + "u1", {"audio_langs": "fr", "sub_langs": ["fr", "zz"], "sub_mode": "toujours"})
        loaded = prefs.load(self.db, "u1")
        self.assertEqual((loaded["audio_langs"], loaded["sub_langs"], loaded["sub_mode"]), ([], [], "forced"))

    def test_the_default_lists_are_not_shared_between_calls(self):
        prefs.load(self.db, "u1")["audio_langs"].append("fr")
        self.assertEqual(prefs.load(self.db, "u1")["audio_langs"], [])


if __name__ == "__main__":
    unittest.main()
