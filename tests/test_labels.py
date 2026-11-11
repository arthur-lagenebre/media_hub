"""Renommer l'affichage d'une bibliothèque : le libellé change partout où le serveur l'écrit, l'identifiant et les données jamais."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, labels
from hub.server.jobs import Jobs


def libs(root, *specs):
    """Les bibliothèques d'essai : (identifiant, libellé d'origine, genre), chacune dans son dossier."""
    out = []
    for lib_id, label, kind in specs:
        (root / lib_id).mkdir(exist_ok=True)
        out.append(config.Library(lib_id, label, kind, root / lib_id))
    return out


SPECS = (("films", "Films", "movies"), ("series", "Séries", "shows"), ("musique", "Musique", "music"))


class LabelCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.libraries = libs(self.root, *SPECS)
        self.hub = MiniHub(self.root, self.libraries)
        self.db = self.hub.db
        self.admin = self.hub.cookie(accounts.create(self.db, "root", "motdepasse12"))         # le premier compte est l'administrateur
        self.user = accounts.create(self.db, "bob", "motdepasse12")
        self.user_cookie = self.hub.cookie(self.user)

    def get(self, target, headers=None):
        return call(self.hub, "GET", target, headers=headers or self.admin)[1]

    def rename(self, lib_id, body, headers=None):
        reply = call(self.hub, "POST", f"/api/library/{lib_id}/label", body, headers=headers or self.admin)[1]
        return reply.status, reply.json()

    def label_of(self, lib_id, headers=None):
        return {lib["id"]: lib["label"] for lib in self.get("/api/state", headers).json()["libraries"]}[lib_id]


class TestRename(LabelCase):
    def test_a_library_is_renamed_and_the_reply_carries_the_new_and_the_original_label(self):
        self.assertEqual(self.rename("series", {"label": "Feuilletons"}), (200, {"id": "series", "label": "Feuilletons", "default_label": "Séries", "kind": "shows"}))
        self.assertEqual(self.label_of("series"), "Feuilletons")

    def test_the_label_is_cleaned_of_its_surrounding_spaces_and_may_keep_accents_and_inner_spaces(self):
        self.assertEqual(self.rename("series", {"label": "  Séries télé  "})[1]["label"], "Séries télé")
        self.assertEqual(self.rename("series", {"label": "日本のアニメ ★"})[1]["label"], "日本のアニメ ★")

    def test_an_empty_or_missing_label_restores_the_original_name(self):
        for body in ({"label": ""}, {"label": "   "}, {"label": None}, {}):
            self.rename("series", {"label": "Feuilletons"})
            self.assertEqual(self.rename("series", body), (200, {"id": "series", "label": "Séries", "default_label": "Séries", "kind": "shows"}), body)
            self.assertEqual(self.label_of("series"), "Séries")
            self.assertNotIn("series", labels.stored(self.db))

    def test_typing_the_original_name_again_keeps_no_override(self):
        self.rename("series", {"label": "Feuilletons"})
        self.assertEqual(self.rename("series", {"label": "Séries"})[1]["label"], "Séries")
        self.assertEqual(labels.stored(self.db), {})

    def test_a_library_may_change_only_the_case_of_its_own_name(self):
        self.assertEqual(self.rename("films", {"label": "FILMS"})[1]["label"], "FILMS")

    def test_forty_characters_pass_and_forty_one_do_not(self):
        self.assertEqual(self.rename("films", {"label": "é" * 40})[0], 200)
        self.assertEqual(self.rename("films", {"label": "é" * 41}), (400, {"error": "le libellé fait 40 caractères au plus"}))
        self.assertEqual(self.label_of("films"), "é" * 40)

    def test_control_and_formatting_characters_are_refused_and_change_nothing(self):
        for bad in ("Fi\nlms", "Fi\tlms", "Fi\x00lms", "Fi​lms", "Fi‮lms", "Fi lms", "Fi\x7flms", "Fi﻿ms"):
            self.assertEqual(self.rename("films", {"label": bad}), (400, {"error": "le libellé ne doit contenir ni caractère de contrôle ni caractère de mise en forme"}), repr(bad))
        self.assertEqual(self.label_of("films"), "Films")

    def test_a_label_that_is_not_text_is_refused(self):
        for bad in (5, ["Films"], {"a": 1}, True):
            self.assertEqual(self.rename("films", {"label": bad}), (400, {"error": "le libellé doit être un texte"}), repr(bad))

    def test_another_library_s_name_is_refused_whatever_the_case_or_the_accents(self):
        self.assertEqual(self.rename("films", {"label": "Séries"}), (400, {"error": "« Séries » est déjà le nom d'une autre bibliothèque"}))
        for same in ("séries", "SERIES", "Series", "sÉrIeS", "  series "):
            self.assertEqual(self.rename("films", {"label": same})[0], 400, same)
        self.assertEqual(self.label_of("films"), "Films")

    def test_the_name_another_library_was_given_is_refused_too(self):
        self.rename("series", {"label": "Feuilletons"})
        self.assertEqual(self.rename("films", {"label": "feuilletons"}), (400, {"error": "« Feuilletons » est déjà le nom d'une autre bibliothèque"}))

    def test_the_original_name_of_a_renamed_library_stays_reserved_so_that_restoring_it_always_works(self):
        self.rename("series", {"label": "Feuilletons"})
        self.assertEqual(self.rename("films", {"label": "Séries"})[0], 400)
        self.assertEqual(self.rename("series", {"label": ""})[1]["label"], "Séries")

    def test_a_name_freed_by_a_rename_of_the_same_library_can_be_reused_by_it(self):
        self.rename("series", {"label": "Feuilletons"})
        self.assertEqual(self.rename("series", {"label": "FEUILLETONS"})[0], 200)

    def test_a_body_that_is_not_a_json_object_is_a_400_and_restores_nothing(self):
        self.rename("films", {"label": "Cinéma"})
        for raw in (b"{nope", b'["Films"]', b"42", b"", b"label=Films"):
            reply = call(self.hub, "POST", "/api/library/films/label", raw, headers=self.admin)[1]
            self.assertEqual((reply.status, reply.json()), (400, {"error": "le corps doit être un objet JSON"}), raw)
        self.assertEqual(self.label_of("films"), "Cinéma")

    def test_the_rights_and_the_library_are_judged_before_the_body(self):
        reply = call(self.hub, "POST", "/api/library/films/label", b"{nope", headers=self.user_cookie)[1]
        self.assertEqual(reply.status, 403)
        reply = call(self.hub, "POST", "/api/library/inconnue/label", b"{nope", headers=self.admin)[1]
        self.assertEqual(reply.status, 404)

    def test_an_unknown_library_is_a_404(self):
        self.assertEqual(self.rename("inconnue", {"label": "X"}), (404, {"error": "bibliothèque inconnue"}))

    def test_a_plain_account_is_a_403_and_nothing_changes(self):
        self.assertEqual(self.rename("films", {"label": "Cinéma"}, headers=self.user_cookie), (403, {"error": "réservé à l'administrateur"}))
        self.assertEqual(self.rename("films", {"label": ""}, headers=self.user_cookie)[0], 403)
        self.assertEqual(self.label_of("films"), "Films")

    def test_a_request_without_a_session_is_a_401(self):
        reply = call(self.hub, "POST", "/api/library/films/label", {"label": "Cinéma"}, headers={})[1]
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.label_of("films"), "Films")


class TestShownEverywhere(LabelCase):
    def setUp(self):
        super().setUp()
        self.db.replace_library("films", [item("f1", title="Inception", lib="films")], [entry("e1", item_id="f1", lib="films")])
        self.db.replace_library("series", [item("s1", title="Dark", lib="series", kind="show")], [entry("e2", item_id="s1", lib="series")])
        self.rename("films", {"label": "Cinéma"})
        self.rename("series", {"label": "Feuilletons"})

    def test_state_gives_the_label_and_the_original_name_of_each_library(self):
        shown = {lib["id"]: (lib["label"], lib["default_label"]) for lib in self.get("/api/state").json()["libraries"]}
        self.assertEqual(shown, {"films": ("Cinéma", "Films"), "series": ("Feuilletons", "Séries"), "musique": ("Musique", "Musique")})

    def test_a_plain_account_sees_the_new_names_too(self):
        self.assertEqual(self.label_of("films", self.user_cookie), "Cinéma")

    def test_home_gives_the_label(self):
        self.assertEqual({row["id"]: row["label"] for row in self.get("/api/home").json()["libraries"]}, {"films": "Cinéma", "series": "Feuilletons", "musique": "Musique"})

    def test_search_gives_the_label(self):
        self.assertEqual([(r["id"], r["label"]) for r in self.get("/api/search?q=incep").json()["results"]], [("films", "Cinéma")])

    def test_a_library_page_and_an_item_page_give_the_label(self):
        self.assertEqual(self.get("/api/library/films").json()["library"]["label"], "Cinéma")
        self.assertEqual(self.get("/api/item/f1").json()["library"]["label"], "Cinéma")

    def test_the_rights_of_an_account_are_listed_by_identifier_and_the_label_is_the_one_of_state(self):
        rights = self.get("/api/users").json()["users"]
        bob = next(u for u in rights if u["name"] == "bob")
        self.assertEqual(sorted(bob["rights"]), ["films", "musique", "series"])                  # l'interface appaire ces identifiants aux libellés de /api/state
        self.assertEqual(self.label_of("films"), "Cinéma")

    def test_a_library_an_account_cannot_see_stays_hidden_whatever_its_name(self):
        call(self.hub, "POST", f"/api/user/{self.user['id']}/rights", {"rights": {"films": {"view": False}}}, headers=self.admin)
        self.assertEqual(sorted(lib["id"] for lib in self.get("/api/state", self.user_cookie).json()["libraries"]), ["musique", "series"])

    def test_the_tasks_and_their_journal_use_the_label(self):
        jobs = Jobs(self.hub.cfg, self.db)
        job = jobs.scan(self.libraries[1])
        for _ in range(100):
            if job.status in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(job.label, "Mise à jour · Feuilletons")
        self.assertTrue(job.log and all(line.startswith("[Feuilletons]") for line in job.log), list(job.log))
        self.assertEqual(jobs.enrich(self.libraries[1]).label, "Enrichissement TMDB · Feuilletons")
        self.assertEqual(jobs.enrich_artists(self.libraries[2]).label, "Fiches artistes · Musique")

    def test_the_inbox_warning_names_the_library_by_its_label(self):
        self.hub.inbox.root = self.libraries[0].path / "envois"
        self.assertIn("« Cinéma »", self.hub.inbox.check())


class TestNothingElseChanges(LabelCase):
    def snapshot(self):
        """Tout ce qu'un renommage ne doit pas toucher : lignes `items` et `entries`, scan, préférences, droits, identifiants configurés."""
        rows = lambda sql: [tuple(row) for row in self.db.query(sql)]          # noqa: E731
        return (rows("SELECT * FROM items ORDER BY id"), rows("SELECT * FROM entries ORDER BY id"), self.db.kv_get("scanned:films"), self.db.kv_get("prefs:" + self.user["id"]),
                accounts.grants(self.db, self.user["id"]), [lib.id for lib in self.hub.cfg.libraries])

    def test_renaming_changes_no_identifier_no_item_row_and_no_right(self):
        self.db.replace_library("films", [item("f1", title="Inception", lib="films")], [entry("e1", item_id="f1", lib="films")])
        self.db.kv_set("scanned:films", 123)
        self.db.kv_set("prefs:" + self.user["id"], {"music_view": "artists"})
        accounts.set_rights(self.db, self.user["id"], {lib.id for lib in self.libraries}, {"films": {"view": True, "play": False, "download": False}})
        before = self.snapshot()
        for label in ("Cinéma", "Salle obscure", ""):
            self.rename("films", {"label": label})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.get("/api/library/films").json()["total"], 1)                    # l'identifiant sert toujours de clé

    def test_the_configuration_object_is_not_touched(self):
        self.rename("films", {"label": "Cinéma"})
        self.assertEqual([lib.label for lib in self.hub.cfg.libraries], ["Films", "Séries", "Musique"])


class TestPersistence(LabelCase):
    def test_a_new_name_survives_a_restart(self):
        self.rename("series", {"label": "Feuilletons"})
        again = MiniHub(self.root, libs(self.root, *SPECS))                       # même base, autre processus
        reply = call(again, "GET", "/api/state", headers=again.cookie(accounts.by_name(again.db, "root")))[1]
        self.assertEqual({lib["id"]: lib["label"] for lib in reply.json()["libraries"]}["series"], "Feuilletons")

    def test_an_override_for_a_library_that_is_not_configured_is_ignored_and_comes_back_with_it(self):
        self.db.kv_set(labels.KEY, {"series": "Feuilletons", "fantome": "Revenant"})
        self.hub.cfg.libraries = [lib for lib in self.hub.cfg.libraries if lib.id != "series"]
        self.assertEqual([lib["id"] for lib in self.get("/api/state").json()["libraries"]], ["films", "musique"])
        self.assertEqual(self.rename("films", {"label": "Cinéma"})[0], 200)                            # écrire une autre surcharge ne perd pas celles des absentes
        self.assertEqual(labels.stored(self.db), {"series": "Feuilletons", "fantome": "Revenant", "films": "Cinéma"})
        self.hub.cfg.libraries = libs(self.root, *SPECS)
        self.assertEqual(self.label_of("series"), "Feuilletons")

    def test_a_damaged_stored_value_is_ignored(self):
        for junk in ("texte", ["a"], {"films": 5, "series": "", "musique": None}, None):
            self.db.kv_set(labels.KEY, junk)
            self.assertEqual(self.label_of("films"), "Films")
        self.assertEqual(self.rename("films", {"label": "Cinéma"})[0], 200)
        self.assertEqual(self.label_of("films"), "Cinéma")


class TestOneDecision(unittest.TestCase):
    def test_shown_prefers_the_override_over_the_configured_label(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        from hub.db import Database
        db = Database(root / "hub.db")
        lib = config.Library("films", "Films", "movies", root)
        self.assertEqual(labels.shown(db, lib), "Films")
        db.kv_set(labels.KEY, {"films": "Cinéma"})
        self.assertEqual(labels.shown(db, lib), "Cinéma")
        self.assertEqual(labels.describe(db, lib), {"id": "films", "label": "Cinéma", "default_label": "Films", "kind": "movies"})


if __name__ == "__main__":
    unittest.main()
