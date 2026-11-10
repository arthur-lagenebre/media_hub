"""Renommer une bibliothèque, côté hostile : Unicode qui ressemble à un autre nom, texte à ne jamais altérer, corps et chemins tordus, droits, deux renommages en même temps, valeurs abîmées dans `kv`.

Les tests marqués `expectedFailure` décrivent un défaut du code (dit dans leur commentaire) : ils passent au rouge « réussite inattendue » le jour où il est corrigé, et il faut alors retirer la marque."""

from __future__ import annotations

import io
import json
import threading
import time
import unittest

from tests.rig import Rig, call
from tests.test_labels import LabelCase
from tests.test_server_views import entry, item
from hub import accounts, labels
from hub.server.jobs import Jobs

REFUSED_CHARACTER = {"error": "le libellé ne doit contenir ni caractère de contrôle ni caractère de mise en forme"}
HOSTILE = "<b>x</b> & \"q\" 'a' \\ %s {} {0} %(n)d"                # rien de tout cela ne doit être échappé ni interprété par le serveur


class TestUnicode(LabelCase):
    def test_a_name_with_a_combining_accent_is_the_same_name_as_the_composed_one(self):
        decomposed_series = "Séries"                                        # « e » + accent aigu combinant : une marque (Mn), pas un caractère de contrôle
        self.assertEqual(self.rename("films", {"label": decomposed_series})[0], 400)
        self.rename("series", {"label": "Feuilletons Été"})
        self.assertEqual(self.rename("films", {"label": "feuilletons Été"})[0], 400)       # le libellé choisi d'une autre, accents combinants et casse mêlés
        self.assertEqual(self.label_of("films"), "Films")

    def test_every_kind_of_space_around_the_name_is_trimmed_not_refused(self):
        for space in (" ", " ", " ", "　", "\r\n", "\t", "\u0085", " ", " ", "\x0b\x0c"):
            self.assertEqual(self.rename("films", {"label": f"{space}Cinéma{space}"})[1]["label"], "Cinéma", repr(space))

    def test_a_space_of_any_kind_around_another_name_does_not_hide_it(self):
        for space in (" ", " ", "　", "\r\n"):
            self.assertEqual(self.rename("films", {"label": f"{space}séries{space}"})[0], 400, repr(space))

    def test_control_and_format_characters_inside_the_name_are_refused(self):
        for bad in ("a\rb", "a\x85b", "a b", "a b", "a​b", "a‍b", "a‮b", "a﻿b", "a­b", "a⁠b", "a\U000e0001b",    # contrôles, séparateurs, mise en forme
                    "ab", "a\U000f0000b", "a͸b"):                           # usage privé (Co), non attribué (Cn)
            self.assertEqual(self.rename("films", {"label": bad}), (400, REFUSED_CHARACTER), repr(bad))
        self.assertEqual(self.label_of("films"), "Films")

    def test_a_lone_surrogate_sent_as_a_json_escape_is_refused(self):
        reply = call(self.hub, "POST", "/api/library/films/label", b'{"label": "a\\ud800b"}', headers=self.admin)[1]
        self.assertEqual((reply.status, reply.json()), (400, REFUSED_CHARACTER))
        self.assertEqual(self.label_of("films"), "Films")

    def test_the_length_is_counted_in_characters_after_the_trim(self):
        self.assertEqual(self.rename("films", {"label": "🎵" * 40})[0], 200)                  # un emoji est un caractère
        self.assertEqual(self.rename("films", {"label": "🎵" * 41})[0], 400)
        self.assertEqual(self.rename("films", {"label": "  " + "é" * 40 + "  "})[1]["label"], "é" * 40)       # les espaces de bout ne comptent pas
        self.assertEqual(self.rename("films", {"label": " " + "é" * 41})[0], 400)

    def test_a_name_made_of_punctuation_only_is_a_name(self):
        self.assertEqual(self.rename("films", {"label": "..."})[1]["label"], "...")
        self.assertEqual(self.rename("series", {"label": "…"})[1]["label"], "…")

    @unittest.expectedFailure
    def test_a_full_width_lookalike_of_another_name_is_refused(self):
        """DÉFAUT (hub/labels.py, `_taken_by_another` : `fold` retire casse, accents et ligatures œ/æ, pas la forme de compatibilité) : « ＦＩＬＭＳ » (pleine largeur) passe à côté de « Films »."""
        self.assertEqual(self.rename("musique", {"label": "ＦＩＬＭＳ"})[0], 400)


class TestTextIsNeverAltered(LabelCase):
    def setUp(self):
        super().setUp()
        self.assertLessEqual(len(HOSTILE), labels.MAX_LENGTH)
        self.db.replace_library("films", [item("f1", title="Inception", lib="films")], [entry("e1", item_id="f1", lib="films")])
        self.assertEqual(self.rename("films", {"label": HOSTILE}), (200, {"id": "films", "label": HOSTILE, "default_label": "Films", "kind": "movies"}))

    def test_state_home_search_library_and_item_give_it_back_as_it_was_typed(self):
        self.assertEqual(self.label_of("films"), HOSTILE)
        self.assertEqual(next(r["label"] for r in self.get("/api/home").json()["libraries"] if r["id"] == "films"), HOSTILE)
        self.assertEqual(self.get("/api/search?q=incep").json()["results"][0]["label"], HOSTILE)
        self.assertEqual(self.get("/api/library/films").json()["library"]["label"], HOSTILE)
        self.assertEqual(self.get("/api/item/f1").json()["library"]["label"], HOSTILE)

    def test_it_travels_as_plain_json_not_as_markup(self):
        reply = self.get("/api/state")
        self.assertEqual(reply.headers["content-type"], "application/json; charset=utf-8")         # un navigateur ne l'interprète pas comme une page
        self.assertIn(json.dumps(HOSTILE, ensure_ascii=False).encode(), reply.body)                # échappé comme du JSON, pas comme du HTML
        self.assertNotIn(b"&lt;", reply.body)
        self.assertNotIn(b"&amp;", reply.body)

    def test_the_task_title_and_its_journal_survive_braces_and_percent_signs(self):
        jobs = Jobs(self.hub.cfg, self.db)
        job = jobs.scan(self.libraries[0])
        for _ in range(100):
            if job.status in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual((job.status, job.label), ("done", f"Mise à jour · {HOSTILE}"))
        self.assertTrue(job.log and all(line.startswith(f"[{HOSTILE}]") for line in job.log), list(job.log))

    def test_the_inbox_warning_carries_it_untouched(self):
        self.hub.inbox.root = self.libraries[0].path / "envois"
        self.assertIn(f"« {HOSTILE} »", self.hub.inbox.check())

    def test_the_refusal_of_a_taken_name_quotes_it_untouched(self):
        self.assertEqual(self.rename("series", {"label": HOSTILE.upper()}), (400, {"error": f"« {HOSTILE} » est déjà le nom d'une autre bibliothèque"}))


class TestBodies(LabelCase):
    def post(self, raw, lib_id="films"):
        reply = call(self.hub, "POST", f"/api/library/{lib_id}/label", raw, headers=self.admin)[1]
        return reply.status, reply.json()

    def test_a_body_over_a_mebibyte_is_a_413_and_changes_nothing(self):
        self.rename("films", {"label": "Cinéma"})
        self.assertEqual(self.post(b'{"label": "' + b"a" * (2 << 20) + b'"}'), (413, {"error": "corps trop gros"}))
        self.assertEqual(self.label_of("films"), "Cinéma")

    def test_extra_fields_are_ignored_and_the_identifier_in_the_body_changes_nothing(self):
        status, reply = self.post(json.dumps({"label": "Cinéma", "id": "series", "lib": "series", "default_label": "Z", "kind": "music"}).encode())
        self.assertEqual((status, reply), (200, {"id": "films", "label": "Cinéma", "default_label": "Films", "kind": "movies"}))
        self.assertEqual(self.label_of("series"), "Séries")

    def test_when_a_key_is_repeated_the_last_one_counts(self):
        self.assertEqual(self.post(b'{"label": "Premier", "label": "Dernier"}')[1]["label"], "Dernier")

    @unittest.expectedFailure
    def test_a_body_that_is_not_a_json_object_does_not_wipe_the_chosen_name(self):
        """DÉFAUT (hub/server/app.py `body_json` : « une liste, du bruit » vaut `{}`, et hub/server/app.py:2724 lit alors `label` absent) : un corps illisible rétablit le nom d'origine (200) au lieu d'être refusé (400)."""
        self.rename("films", {"label": "Cinéma"})
        for raw in (b"{nope", b'["Films"]', b"42", b"\xff\xfe"):
            self.assertEqual(self.post(raw)[0], 400, raw)
            self.assertEqual(self.label_of("films"), "Cinéma", raw)


class TestRouting(LabelCase):
    def test_an_identifier_that_is_not_exactly_a_configured_one_is_a_404_and_changes_nothing(self):
        for target in ("../label", "%2e%2e/label", "FILMS/label", "filmé/label", "films%2Flabel/label", "a" * 5000 + "/label", "films/label/"):
            reply = call(self.hub, "POST", f"/api/library/{target}", {"label": "Piège"}, headers=self.admin)[1]
            self.assertEqual(reply.status, 404, target[:30])
        self.assertEqual([lib["label"] for lib in self.get("/api/state").json()["libraries"]], ["Films", "Séries", "Musique"])

    def test_only_post_renames_the_other_methods_are_not_routed(self):
        for method in ("GET", "PUT", "DELETE"):                          # 404 (« introuvable »), pas 405 : le serveur ne connaît que les couples méthode + chemin déclarés
            reply = call(self.hub, method, "/api/library/films/label", {"label": "Piège"}, headers=self.admin)[1]
            self.assertEqual(reply.status, 404, method)
        self.assertEqual(self.label_of("films"), "Films")

    def test_a_query_string_is_ignored(self):
        reply = call(self.hub, "POST", "/api/library/films/label?label=Piège&x=1", {"label": "Cinéma"}, headers=self.admin)[1]
        self.assertEqual((reply.status, reply.json()["label"]), (200, "Cinéma"))


class TestWhoMayRename(LabelCase):
    def test_an_account_allowed_to_upload_is_still_not_an_administrator(self):
        accounts.set_can_upload(self.db, self.user["id"], True)
        self.assertEqual(self.rename("films", {"label": "Cinéma"}, headers=self.hub.cookie(accounts.by_id(self.db, self.user["id"])))[0], 403)
        self.assertEqual(self.label_of("films"), "Films")

    def test_a_bearer_token_of_the_administrator_is_enough(self):
        row = accounts.by_name(self.db, "root")
        reply = call(self.hub, "POST", "/api/library/films/label", {"label": "Cinéma"}, headers={"Authorization": "Bearer " + self.hub.session_token(row)})[1]
        self.assertEqual((reply.status, reply.json()["label"]), (200, "Cinéma"))

    def test_the_media_token_of_the_administrator_and_a_token_in_the_url_do_not_rename(self):
        row = accounts.by_name(self.db, "root")
        for token in (self.hub.media_token(row), self.hub.session_token(row)):             # `?token=` ne vaut que sur les fichiers et les sous-titres
            reply = call(self.hub, "POST", f"/api/library/films/label?token={token}", {"label": "Piège"}, headers={})[1]
            self.assertEqual(reply.status, 401, token)
        self.assertEqual(self.label_of("films"), "Films")

    def test_a_session_that_was_revoked_no_longer_renames(self):
        row = accounts.by_name(self.db, "root")
        accounts.revoke_sessions(self.db, row["id"])                                      # « déconnecter les autres appareils » : l'ancien cookie ne vaut plus
        self.assertEqual(self.rename("films", {"label": "Piège"}, headers=self.admin)[0], 401)
        fresh = self.hub.cookie(accounts.by_name(self.db, "root"))
        self.assertEqual(self.rename("films", {"label": "Cinéma"}, headers=fresh)[0], 200)

    def test_a_refused_request_leaves_the_stored_names_as_they_were(self):
        self.rename("series", {"label": "Feuilletons"})
        before = self.db.kv_get(labels.KEY)
        self.rename("films", {"label": "Piège"}, headers=self.user_cookie)
        call(self.hub, "POST", "/api/library/films/label", {"label": "Piège"}, headers={})
        self.rename("films", {"label": "feuilletons"})
        self.rename("films", {"label": "a\nb"})
        self.assertEqual(self.db.kv_get(labels.KEY), before)


class TestSeveralRenames(LabelCase):
    def rename_together(self, *asks):
        """Les renommages `(identifiant, texte)` envoyés au même instant, chacun dans son fil ; {identifiant: statut}."""
        gate, replies = threading.Barrier(len(asks)), {}

        def ask(lib_id, text):
            rig = Rig(self.hub, "POST", f"/api/library/{lib_id}/label", json.dumps({"label": text}).encode(), self.admin)
            gate.wait()
            rig.dispatch()                                                # sans `call` : il détourne stderr, global au processus
            replies[lib_id] = rig.reply().status
        threads = [threading.Thread(target=ask, args=pair) for pair in asks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return replies

    def test_a_name_cannot_go_to_two_libraries_even_when_both_ask_at_the_same_instant(self):
        for round_ in range(40):
            for lib in self.libraries[:2]:
                labels.rename(self.db, self.libraries, lib, "")
            replies = self.rename_together(("films", "Cinéma"), ("series", "CINEMA"))
            self.assertEqual(sorted(replies.values()), [200, 400], f"tour {round_} : {replies}")
            self.assertEqual(len({labels.shown(self.db, lib).casefold() for lib in self.libraries}), 3)

    def test_two_renames_of_different_libraries_both_stay(self):
        self.assertEqual(self.rename_together(("films", "Cinéma"), ("series", "Feuilletons")), {"films": 200, "series": 200})
        self.assertEqual(labels.stored(self.db), {"films": "Cinéma", "series": "Feuilletons"})

    def test_chosen_names_can_be_swapped_in_three_steps_but_original_names_never(self):
        self.rename("films", {"label": "Alpha"})
        self.rename("series", {"label": "Beta"})
        self.assertEqual(self.rename("films", {"label": "Beta"})[0], 400)
        self.assertEqual(self.rename("films", {"label": "Provisoire"})[0], 200)
        self.assertEqual(self.rename("series", {"label": "Alpha"})[0], 200)
        self.assertEqual(self.rename("films", {"label": "Beta"})[0], 200)
        self.assertEqual((self.label_of("films"), self.label_of("series")), ("Beta", "Alpha"))
        # les noms d'origine, eux, restent réservés à leur bibliothèque : « Films » et « Séries » ne s'échangent pas
        self.assertEqual(self.rename("films", {"label": "Séries"})[0], 400)
        self.assertEqual(self.rename("series", {"label": "Films"})[0], 400)

    def test_restoring_one_library_keeps_the_names_chosen_for_the_others(self):
        self.rename("films", {"label": "Cinéma"})
        self.rename("series", {"label": "Feuilletons"})
        self.assertEqual(self.rename("films", {"label": ""})[1]["label"], "Films")
        self.assertEqual(self.rename("musique", {"label": None})[1]["label"], "Musique")           # rétablir ce qui n'a jamais changé : rien à faire, rien d'effacé
        self.assertEqual(labels.stored(self.db), {"series": "Feuilletons"})


class TestStoredValuesWrittenByHand(LabelCase):
    def test_two_libraries_with_the_same_stored_name_still_work_and_each_can_be_repaired(self):
        self.db.kv_set(labels.KEY, {"films": "Pareil", "series": "Pareil"})
        self.assertEqual([lib["label"] for lib in self.get("/api/state").json()["libraries"]], ["Pareil", "Pareil", "Musique"])
        self.assertEqual(self.rename("musique", {"label": "pareil"})[0], 400)            # le nom reste pris : pas un troisième
        self.assertEqual(self.rename("films", {"label": "Cinéma"})[0], 200)
        self.assertEqual([lib["label"] for lib in self.get("/api/state").json()["libraries"]], ["Cinéma", "Pareil", "Musique"])

    def test_restoring_a_library_clears_an_unusable_stored_name_whatever_it_is(self):
        self.db.kv_set(labels.KEY, {"films": "x" * 10000, "series": "   "})                 # plus long que permis, et blanc : seule une main a pu les écrire
        for lib_id, original in (("films", "Films"), ("series", "Séries")):
            self.assertEqual(self.rename(lib_id, {"label": ""})[1]["label"], original)
        self.assertEqual(labels.stored(self.db), {})

    def test_a_lone_surrogate_written_by_hand_in_the_database_is_repaired_by_a_rename(self):
        # écrit comme une échappation JSON, seule façon de la faire entrer dans SQLite ; /api/state échoue (500) tant qu'elle y est
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (labels.KEY, '{"films": "\\ud800"}'))
        self.assertEqual(self.get("/api/state").status, 500)
        self.assertEqual(self.rename("films", {"label": ""})[0], 200)
        self.assertEqual(self.label_of("films"), "Films")

    @unittest.expectedFailure
    def test_a_library_that_comes_back_does_not_share_its_chosen_name_with_another(self):
        """DÉFAUT (hub/labels.py `_taken_by_another` ne regarde que `libraries`, donc pas la surcharge gardée d'une bibliothèque absente) : « Feuilletons » est libre tant que `series` n'est pas configurée,
        `films` le prend ; quand `series` revient, deux bibliothèques s'affichent « Feuilletons » (et la tâche « Mise à jour · Feuilletons » de l'une se fond dans celle de l'autre, voir Jobs._submit)."""
        self.rename("series", {"label": "Feuilletons"})
        configured = self.hub.cfg.libraries
        self.hub.cfg.libraries = [lib for lib in configured if lib.id != "series"]
        self.rename("films", {"label": "Feuilletons"})
        self.hub.cfg.libraries = configured
        shown = [lib["label"] for lib in self.get("/api/state").json()["libraries"]]
        self.assertEqual(len(shown), len(set(shown)), shown)


class TestNothingButTheLabelsChanges(LabelCase):
    def dump(self):
        """Toutes les tables, sauf la clé des libellés : ce qu'un renommage ne doit jamais toucher."""
        tables = [row["name"] for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for table in tables:
            sql = f"SELECT * FROM {table}" + (f" WHERE key != '{labels.KEY}'" if table == "kv" else "")
            out[table] = sorted(repr(tuple(row)) for row in self.db.query(sql))
        return out

    def test_a_rename_a_restore_and_a_refusal_change_no_table_but_the_label_key(self):
        self.db.replace_library("films", [item("f1", title="Inception", lib="films")], [entry("e1", item_id="f1", lib="films")])
        self.db.replace_library("series", [item("s1", title="Dark", lib="series", kind="show")], [entry("e2", item_id="s1", lib="series")])
        self.db.kv_set("scanned:series", 456)
        self.db.kv_set("prefs:" + self.user["id"], {"music_view": "artists"})
        accounts.set_rights(self.db, self.user["id"], {lib.id for lib in self.libraries}, {"series": {"view": True, "play": False, "download": False}})
        before = self.dump()
        self.assertGreater(len(before["items"]), 1)                                    # le test ne ment pas : il y a de quoi comparer
        for body in ({"label": "Feuilletons"}, {"label": "Cinéma"}, {"label": "feuilletons"}, {"label": "a\nb"}, {"label": ""}):
            self.rename("series", body)
        self.rename("films", {"label": "Salle obscure"})
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.db.kv_get(labels.KEY), {"films": "Salle obscure"})


class TestTaskTitleFollowsAnOpenRename(LabelCase):
    @unittest.expectedFailure
    def test_a_scan_asked_again_after_a_rename_joins_the_one_already_waiting(self):
        """DÉFAUT (hub/server/jobs.py `_submit` : une tâche en attente ou en cours est reconnue à son INTITULÉ, et l'intitulé porte maintenant le libellé affiché) : renommer une bibliothèque dont la mise à jour attend
        ou tourne, puis la redemander, met une seconde mise à jour de la même bibliothèque en file au lieu de rendre la première."""
        jobs = Jobs(self.hub.cfg, self.db)
        release = threading.Event()
        self.addCleanup(release.set)
        jobs._submit("Occupé", lambda log: release.wait(10))                      # tient le fil des tâches : la suivante reste en attente
        first = jobs.scan(self.libraries[1])
        self.rename("series", {"label": "Feuilletons"})
        self.assertIs(jobs.scan(self.libraries[1]), first)


if __name__ == "__main__":
    unittest.main()
