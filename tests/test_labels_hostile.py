"""Renommer une bibliothèque, côté hostile : Unicode qui ressemble à un autre nom, texte à ne jamais altérer, corps et chemins tordus, droits, deux renommages en même temps, valeurs abîmées dans `kv`.

Un test marqué `expectedFailure` décrirait un défaut connu du code (dit dans son commentaire) ; il n'y en a plus."""

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
        self.assertEqual(self.rename("series", {"label": "—"})[1]["label"], "—")
        self.assertEqual(self.rename("musique", {"label": "…"})[0], 400)          # « … » vaut « ... » une fois les formes de compatibilité ramenées

    def test_a_full_width_lookalike_of_another_name_is_refused(self):
        self.assertEqual(self.rename("musique", {"label": "ＦＩＬＭＳ"})[0], 400)                       # forme de compatibilité (NFKC)
        self.assertEqual(self.rename("musique", {"label": "ＳＥ\u0301ＲＩＥＳ"})[0], 400)             # pleine largeur et accent combinant ensemble, contre « Séries »

    def test_spaces_inside_the_name_do_not_hide_another_name(self):
        self.rename("series", {"label": "Mes séries"})
        for same in ("Mes  séries", "Mes\u00a0séries", "Mes\u3000séries", "Mes   SÉRIES", "mes\u2009séries"):          # double espace, insécable, idéographique, fine
            self.assertEqual(self.rename("films", {"label": same})[0], 400, repr(same))

    def test_a_compatibility_ligature_is_the_same_name(self):
        self.rename("series", {"label": "Office"})
        self.assertEqual(self.rename("films", {"label": "O\ufb03ce"})[0], 400)                        # « ﬃ » (U+FB03) vaut « ffi »

    def test_a_name_in_decomposed_form_is_kept_composed_and_counted_composed(self):
        self.assertEqual(self.rename("films", {"label": "e\u0301" * 40})[1]["label"], "é" * 40)       # 80 caractères avant NFC, 40 après : permis, gardé composé
        self.assertEqual(labels.stored(self.db), {"films": "é" * 40})
        self.assertEqual(self.rename("films", {"label": "e\u0301" * 41})[0], 400)

    def test_the_original_name_typed_in_decomposed_form_really_restores_it(self):
        self.rename("series", {"label": "Feuilletons"})
        self.assertEqual(self.rename("series", {"label": "Se\u0301ries"})[1]["label"], "Séries")        # le même nom : pas de surcharge « Séries » en NFD
        self.assertEqual(self.db.kv_get(labels.KEY), {})


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

    def test_a_body_that_is_not_a_json_object_is_refused_and_does_not_wipe_the_chosen_name(self):
        self.rename("films", {"label": "Cinéma"})
        for raw in (b"{nope", b'["Films"]', b"42", b'"Films"', b"null", b"true", b"", b"\xff\xfe", b'{"label": "a\xffb"}', b"label=Films", b"[" * 100000):
            self.assertEqual(self.post(raw), (400, {"error": "le corps doit être un objet JSON"}), raw[:20])
            self.assertEqual(self.label_of("films"), "Cinéma", raw[:20])

    def test_a_valid_object_without_a_usable_label_still_restores_the_original_name(self):
        for raw in (b"{}", b'{"label": null}', b'{"label": ""}', b'{"autre": 1}', b'  {"label": "   "} '):
            self.rename("films", {"label": "Cinéma"})
            self.assertEqual(self.post(raw), (200, {"id": "films", "label": "Films", "default_label": "Films", "kind": "movies"}), raw)


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
    def names(self):
        return [lib["label"] for lib in self.get("/api/state").json()["libraries"]]

    def test_two_libraries_with_the_same_stored_name_still_work_and_each_can_be_repaired(self):
        self.db.kv_set(labels.KEY, {"films": "Pareil", "series": "Pareil"})
        self.assertEqual(self.names(), ["Pareil", "Pareil", "Musique"])
        self.assertEqual(self.rename("musique", {"label": "pareil"})[0], 400)            # le nom reste pris : pas un troisième
        self.assertEqual(self.rename("films", {"label": "Cinéma"})[0], 200)
        self.assertEqual(self.names(), ["Cinéma", "Pareil", "Musique"])

    def test_a_stored_name_that_is_not_safe_to_show_is_ignored_and_the_original_name_shows(self):
        for bad in ("x" * 10000, "x" * 41, "   ", "", " Cinéma", "Cinéma ", "Ci\nnéma", "Ci\u200bnéma", "Ci\u202enéma", "Cine\u0301ma", 5, None, ["Cinéma"], {"a": 1}):
            self.db.kv_set(labels.KEY, {"films": bad})
            self.assertEqual(self.names(), ["Films", "Séries", "Musique"], repr(bad)[:30])
        self.assertEqual(self.rename("films", {"label": ""})[0], 200)
        self.assertEqual(self.db.kv_get(labels.KEY), {})

    def test_a_lone_surrogate_written_by_hand_in_the_database_is_ignored_not_a_500(self):
        # écrit comme une échappation JSON, seule façon de la faire entrer dans SQLite
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (labels.KEY, '{"films": "\\ud800", "series": "Feuilletons"}'))
        state = self.get("/api/state")
        self.assertEqual(state.status, 200)
        self.assertEqual([lib["label"] for lib in state.json()["libraries"]], ["Films", "Feuilletons", "Musique"])
        self.assertEqual(self.rename("films", {"label": ""})[0], 200)

    def test_a_usable_stored_name_is_shown_as_it_is(self):
        self.db.kv_set(labels.KEY, {"films": "Cinéma", "series": "é" * 40, "musique": "日本"})
        self.assertEqual(self.names(), ["Cinéma", "é" * 40, "日本"])

    def test_a_library_that_comes_back_never_shares_its_chosen_name_with_another(self):
        self.rename("series", {"label": "Feuilletons"})
        configured = self.hub.cfg.libraries
        self.hub.cfg.libraries = [lib for lib in configured if lib.id != "series"]               # le dossier n'est pas monté
        self.assertEqual(self.rename("films", {"label": "feuilletons"}), (400, {"error": "« Feuilletons » est déjà le nom d'une autre bibliothèque"}))
        self.assertEqual(self.rename("films", {"label": "ＦＥＵＩＬＬＥＴＯＮＳ"})[0], 400)
        self.assertEqual(self.rename("films", {"label": "Cinéma"})[0], 200)                       # un autre nom reste libre
        self.hub.cfg.libraries = configured
        self.assertEqual(self.names(), ["Cinéma", "Feuilletons", "Musique"])

    def test_an_absent_library_whose_stored_name_is_unusable_reserves_nothing(self):
        self.db.kv_set(labels.KEY, {"fantome": "Ci\nnéma", "autre": "  "})
        self.assertEqual(self.rename("films", {"label": "Cinéma"})[0], 200)


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


class TestTasksAreTheSameRequestWhateverTheLabel(LabelCase):
    def setUp(self):
        super().setUp()
        self.jobs = Jobs(self.hub.cfg, self.db)
        self.release = threading.Event()
        self.addCleanup(self.finish)
        self.jobs._submit("Occupé", lambda log: self.release.wait(10))                      # tient le fil des tâches : les suivantes restent en attente

    def finish(self):
        self.release.set()
        for _ in range(200):
            if not self.jobs.busy():
                return
            time.sleep(0.05)

    def test_a_scan_asked_again_after_a_rename_joins_the_one_already_waiting(self):
        first = self.jobs.scan(self.libraries[1])
        self.rename("series", {"label": "Feuilletons"})
        self.assertIs(self.jobs.scan(self.libraries[1]), first)
        self.assertEqual(first.label, "Mise à jour · Séries")                                    # l'intitulé est celui du moment de la demande

    def test_a_library_renamed_all_does_not_swallow_the_scan_of_everything(self):
        self.rename("series", {"label": "tout"})
        one = self.jobs.scan(self.libraries[1])
        everything = self.jobs.scan_all()
        self.assertIsNot(everything, one)
        self.assertEqual((one.label, everything.label), ("Mise à jour · tout", "Mise à jour · tout"))     # deux tâches, même intitulé
        self.assertIs(self.jobs.scan_all(), everything)
        self.assertIs(self.jobs.scan(self.libraries[1]), one)

    def test_two_libraries_with_the_same_label_in_the_database_do_not_share_their_tasks(self):
        self.db.kv_set(labels.KEY, {"films": "Pareil", "series": "Pareil"})                   # écrit à la main : le serveur ne le permet plus
        self.assertIsNot(self.jobs.scan(self.libraries[0]), self.jobs.scan(self.libraries[1]))
        self.assertIsNot(self.jobs.enrich(self.libraries[0]), self.jobs.enrich(self.libraries[1]))
        self.assertIsNot(self.jobs.enrich_artists(self.libraries[0]), self.jobs.enrich_artists(self.libraries[1]))

    def test_each_kind_of_task_of_a_library_is_its_own_request(self):
        lib = self.libraries[1]
        kinds = [self.jobs.scan(lib), self.jobs.enrich(lib), self.jobs.enrich_sagas(lib), self.jobs.enrich_artists(lib)]
        self.assertEqual(len({id(job) for job in kinds}), 4)
        self.assertIs(self.jobs.enrich(lib), kinds[1])
        self.assertIs(self.jobs.enrich_sagas(lib), kinds[2])
        self.assertIs(self.jobs.enrich_artists(lib), kinds[3])

    def test_the_people_task_is_one_request_per_set_of_libraries(self):
        both = self.jobs.enrich_people(self.libraries[:2], "deux")
        self.assertIs(self.jobs.enrich_people(self.libraries[:2], "autre intitulé"), both)
        self.assertIsNot(self.jobs.enrich_people(self.libraries[:1], "deux"), both)


if __name__ == "__main__":
    unittest.main()
