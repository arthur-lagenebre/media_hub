"""Les suggestions : une idée ou une remarque sur le hub lui-même, envoyée par un compte à l'administrateur. Proposer, les catégories, l'absence de plafond, la confidentialité d'un compte à l'autre, l'état et la réponse de l'administrateur, le retrait, la suppression d'un compte, `/api/state` et la migration de la base."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, suggestions
from hub import db as database
from hub.db import Database

PASSWORD = "un mot de passe assez long"
UNKNOWN = {"error": "suggestion inconnue"}
ADMIN_ONLY = {"error": "réservé à l'administrateur"}


class SuggestionCase(unittest.TestCase):
    """Un hub sans bibliothèque, un administrateur et deux comptes, Alice et Bob."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.hub = MiniHub(self.root)
        self.db = self.hub.db
        self.boss = accounts.create(self.db, "boss", PASSWORD)
        self.alice = accounts.create(self.db, "alice", PASSWORD)
        self.bob = accounts.create(self.db, "bob", PASSWORD)

    def headers(self, who):
        return self.hub.cookie(accounts.by_id(self.db, who["id"]))

    def get(self, who, target):
        _, reply = call(self.hub, "GET", target, headers=self.headers(who))
        return reply

    def post(self, who, target, body=None):
        _, reply = call(self.hub, "POST", target, {} if body is None else body, self.headers(who))
        return reply

    def suggest(self, who, text="Le lecteur devrait reprendre au bon endroit", category="improvement"):
        return self.post(who, "/api/suggestions", {"category": category, "text": text})

    def mine(self, who):
        reply = self.get(who, "/api/suggestions?mine=1")
        self.assertEqual(reply.status, 200)
        return reply.json()["suggestions"]

    def everything(self, query=""):
        reply = self.get(self.boss, "/api/suggestions" + query)
        self.assertEqual(reply.status, 200)
        return reply.json()["suggestions"]

    def set_status(self, sid, status, reply=None, who=None):
        return self.post(who or self.boss, f"/api/suggestions/{sid}/status", {"status": status, **({"reply": reply} if reply is not None else {})})

    def snapshot(self):
        return [tuple(r) for r in self.db.query("SELECT * FROM suggestions ORDER BY id")]


class TestSuggesting(SuggestionCase):
    def test_a_suggestion_is_created_new_and_listed_for_its_author(self):
        reply = self.suggest(self.alice, "  Un mode sombre plus sombre  ", "evolution")
        self.assertEqual(reply.status, 201)
        created = reply.json()
        self.assertEqual({k: created[k] for k in ("category", "text", "status", "reply")}, {"category": "evolution", "text": "Un mode sombre plus sombre", "status": "new", "reply": ""})
        self.assertEqual([s["id"] for s in self.mine(self.alice)], [created["id"]])
        self.assertEqual(set(self.mine(self.alice)[0]), {"id", "category", "text", "status", "reply", "created", "updated", "item", "entry", "cause", "subject"})
        self.assertEqual({k: self.mine(self.alice)[0][k] for k in ("item", "entry", "cause", "subject")}, {"item": "", "entry": "", "cause": "", "subject": ""})

    def test_every_category_is_accepted(self):
        for category in ("evolution", "improvement", "bug", "other"):
            with self.subTest(category=category):
                self.assertEqual(self.suggest(self.alice, category=category).status, 201)

    def test_there_is_no_ceiling_on_the_number_of_suggestions(self):
        for n in range(60):
            self.assertEqual(self.suggest(self.bob, f"Idée {n}").status, 201)
        listed = self.mine(self.bob)
        self.assertEqual(len(listed), 60)
        self.assertEqual(listed[0]["text"], "Idée 59")                    # la plus récente d'abord

    def test_a_bad_body_is_a_400_in_french_and_nothing_is_stored(self):
        cases = {
            "sans catégorie": ({"text": "x"}, "catégorie inconnue (evolution, improvement, bug ou other)"),
            "catégorie inconnue": ({"category": "film", "text": "x"}, "catégorie inconnue (evolution, improvement, bug ou other)"),
            "sans texte": ({"category": "bug"}, "le texte est vide"),
            "texte blanc": ({"category": "bug", "text": "   \n "}, "le texte est vide"),
            "texte qui n'en est pas": ({"category": "bug", "text": 12}, "texte : un texte est attendu"),
            "texte trop long": ({"category": "bug", "text": "x" * 1001}, "texte trop long (1000 caractères au plus)"),
            "pas un objet": ([1], "le corps doit être un objet JSON"),
        }
        for name, (body, message) in cases.items():
            with self.subTest(name):
                reply = self.post(self.alice, "/api/suggestions", body)
                self.assertEqual((reply.status, reply.json()), (400, {"error": message}))
        self.assertEqual(self.snapshot(), [])

    def test_a_text_of_exactly_the_limit_is_accepted(self):
        self.assertEqual(self.suggest(self.alice, "é" * 1000).status, 201)

    def test_without_a_session_every_route_is_a_401_and_changes_nothing(self):
        sid = self.suggest(self.alice).json()["id"]
        before = self.snapshot()
        for method, target in (("GET", "/api/suggestions?mine=1"), ("GET", "/api/suggestions"), ("POST", "/api/suggestions"),
                               ("POST", f"/api/suggestions/{sid}/status"), ("POST", f"/api/suggestions/{sid}/delete")):
            with self.subTest(target=target):
                _, reply = call(self.hub, method, target, {"category": "bug", "text": "x", "status": "done"} if method == "POST" else None)
                self.assertEqual(reply.status, 401)
        self.assertEqual(self.snapshot(), before)


class TestPrivacy(SuggestionCase):
    def test_an_account_sees_only_its_own_suggestions(self):
        self.suggest(self.alice, "D'Alice")
        self.suggest(self.bob, "De Bob")
        self.assertEqual([s["text"] for s in self.mine(self.alice)], ["D'Alice"])
        self.assertEqual([s["text"] for s in self.mine(self.bob)], ["De Bob"])

    def test_the_full_list_is_for_the_admin_only_with_the_authors(self):
        self.suggest(self.alice, "D'Alice")
        self.suggest(self.bob, "De Bob", "bug")
        reply = self.get(self.alice, "/api/suggestions")
        self.assertEqual((reply.status, reply.json()), (403, ADMIN_ONLY))
        listed = self.everything()
        self.assertEqual([(s["text"], s["user"]) for s in listed], [("De Bob", {"id": self.bob["id"], "name": "bob"}), ("D'Alice", {"id": self.alice["id"], "name": "alice"})])

    def test_the_full_list_filters_by_status_and_category(self):
        a = self.suggest(self.alice, "Un", "bug").json()["id"]
        b = self.suggest(self.alice, "Deux", "evolution").json()["id"]
        self.suggest(self.bob, "Trois", "bug")
        self.set_status(a, "done")
        self.assertEqual([s["text"] for s in self.everything("?status=done")], ["Un"])
        self.assertEqual([s["text"] for s in self.everything("?category=bug")], ["Trois", "Un"])
        self.assertEqual([s["text"] for s in self.everything("?status=new&category=bug")], ["Trois"])
        self.assertEqual([s["id"] for s in self.everything("?category=evolution")], [b])
        for query, message in (("?status=pending", "statut inconnu (new, planned, done ou declined)"), ("?category=film", "catégorie inconnue (evolution, improvement, bug ou other)")):
            with self.subTest(query=query):
                reply = self.get(self.boss, "/api/suggestions" + query)
                self.assertEqual((reply.status, reply.json()), (400, {"error": message}))

    def test_the_state_counts_new_suggestions_for_the_admin_only(self):
        first = self.suggest(self.alice).json()["id"]
        self.suggest(self.bob)
        self.set_status(first, "planned")
        self.assertEqual(self.get(self.boss, "/api/state").json()["suggestions_new"], 1)
        self.assertNotIn("suggestions_new", self.get(self.alice, "/api/state").json())


class TestAnswering(SuggestionCase):
    def test_the_admin_sets_a_status_and_a_reply_the_author_reads(self):
        sid = self.suggest(self.alice).json()["id"]
        reply = self.set_status(sid, "planned", "  Bonne idée, pour la prochaine version  ")
        self.assertEqual(reply.status, 200)
        self.assertEqual({k: reply.json()[k] for k in ("status", "reply")}, {"status": "planned", "reply": "Bonne idée, pour la prochaine version"})
        self.assertEqual({k: self.mine(self.alice)[0][k] for k in ("status", "reply")}, {"status": "planned", "reply": "Bonne idée, pour la prochaine version"})

    def test_a_status_without_reply_keeps_the_reply_and_an_empty_one_clears_it(self):
        sid = self.suggest(self.alice).json()["id"]
        self.set_status(sid, "planned", "Prévu")
        self.assertEqual(self.set_status(sid, "done").json()["reply"], "Prévu")
        self.assertEqual(self.set_status(sid, "done", "").json()["reply"], "")

    def test_a_status_is_set_by_the_admin_only_must_be_known_and_the_reply_is_limited(self):
        sid = self.suggest(self.alice).json()["id"]
        before = self.snapshot()
        reply = self.set_status(sid, "done", who=self.alice)
        self.assertEqual((reply.status, reply.json()), (403, ADMIN_ONLY))
        for status, answer, message in (("pending", None, "statut inconnu (new, planned, done ou declined)"), ("done", "x" * 501, "réponse trop longue (500 caractères au plus)"),
                                        ("done", 3, "réponse : un texte est attendu")):
            with self.subTest(status=status, answer=answer):
                reply = self.set_status(sid, status, answer)
                self.assertEqual((reply.status, reply.json()), (400, {"error": message}))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.set_status(999, "done").status, self.set_status(999, "done").json()), (404, UNKNOWN))


class TestWithdrawing(SuggestionCase):
    def test_the_author_withdraws_its_suggestion_and_another_account_gets_a_404(self):
        sid = self.suggest(self.alice).json()["id"]
        reply = self.post(self.bob, f"/api/suggestions/{sid}/delete")
        self.assertEqual((reply.status, reply.json()), (404, UNKNOWN))
        self.assertEqual(len(self.snapshot()), 1)
        self.assertEqual(self.post(self.alice, f"/api/suggestions/{sid}/delete").json(), {"ok": True})
        self.assertEqual(self.snapshot(), [])
        self.assertEqual(self.post(self.alice, f"/api/suggestions/{sid}/delete").status, 404)

    def test_the_admin_deletes_any_suggestion(self):
        sid = self.suggest(self.alice).json()["id"]
        self.assertEqual(self.post(self.boss, f"/api/suggestions/{sid}/delete").json(), {"ok": True})
        self.assertEqual(self.snapshot(), [])

    def test_deleting_an_account_takes_its_suggestions_away(self):
        self.suggest(self.alice, "D'Alice")
        self.suggest(self.bob, "De Bob")
        accounts.delete(self.db, self.alice["id"])
        self.assertEqual([s["text"] for s in self.everything()], ["De Bob"])


class TestMigration(unittest.TestCase):
    def test_a_version_8_base_gains_the_suggestions_and_keeps_everything_else(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "hub.db"
        db = Database(path)
        alice = accounts.create(db, "boss", PASSWORD) and accounts.create(db, "alice", PASSWORD)
        db.save_progress(alice["id"], "e1", 12.5, 100, False)
        db.set_rating(alice["id"], "m1", "up")
        db.execute("INSERT INTO requests(kind, tmdb, title, created, updated) VALUES('movie', 7, 'Sept', 1, 1)")
        db.connection().close()
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE suggestions; UPDATE kv SET value='8' WHERE key='schema';")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(database.SCHEMA_VERSION, 12)
        self.assertEqual(db.kv_get("schema"), 12)
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [(alice["id"], "e1", 12.5)])
        self.assertEqual(db.ratings_for(alice["id"]), {"m1": "up"})
        self.assertEqual([r["title"] for r in db.query("SELECT title FROM requests")], ["Sept"])
        self.assertEqual(db.query("SELECT * FROM suggestions"), [])
        self.assertEqual(Database(path).kv_get("schema"), 12)         # rouverte : rien n'est rejoué


OLD_SUGGESTIONS = """CREATE TABLE suggestions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('evolution', 'improvement', 'bug', 'other')), text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'planned', 'done', 'declined')), reply TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL, updated INTEGER NOT NULL)"""
REPORT_FIELDS = ("item", "entry", "cause", "subject")
CAUSE_REQUIRED = "la cause est obligatoire (image, sound, subtitles, playback ou other)"


class TestReportsMigration(unittest.TestCase):
    def test_a_version_9_base_keeps_its_suggestions_and_they_get_empty_report_columns(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "hub.db"
        db = Database(path)
        alice = accounts.create(db, "alice", PASSWORD)
        db.save_progress(alice["id"], "e1", 12.5, 100, False)
        db.connection().close()
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE suggestions;" + OLD_SUGGESTIONS + ";")
        conn.execute("INSERT INTO suggestions(user_id, category, text, status, reply, created, updated) VALUES(?, 'bug', 'Un bug', 'planned', 'Noté', 5, 6)", (alice["id"],))
        conn.execute("INSERT INTO suggestions(user_id, category, text, created, updated) VALUES(?, 'other', 'Autre chose', 7, 7)", (alice["id"],))
        conn.execute("UPDATE kv SET value='9' WHERE key='schema'")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.kv_get("schema"), database.SCHEMA_VERSION)
        rows = db.query("SELECT * FROM suggestions ORDER BY id")
        self.assertEqual([(r["category"], r["text"], r["status"], r["reply"], r["created"], r["updated"]) for r in rows], [("bug", "Un bug", "planned", "Noté", 5, 6), ("other", "Autre chose", "new", "", 7, 7)])
        self.assertEqual([(r["item_id"], r["entry_id"], r["cause"], r["subject"], r["context"]) for r in rows], [("", "", "", "", "")] * 2)
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [(alice["id"], "e1", 12.5)])
        with self.assertRaises(sqlite3.IntegrityError):                 # la contrainte de `cause` vaut aussi sur la colonne ajoutée
            db.execute("UPDATE suggestions SET cause='nimporte' WHERE id=1")


class ReportCase(SuggestionCase):
    """Un hub à cinq bibliothèques (films, séries, musique, livres, une privée que Bob ne voit pas), un administrateur et deux comptes."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        libs = [config.Library(ident, ident.title(), kind, self.root / ident) for ident, kind in (("films", "movies"), ("series", "shows"), ("musique", "music"), ("livres", "books"), ("prive", "movies"))]
        for lib in libs:
            lib.path.mkdir()
        self.hub = MiniHub(self.root, libs)
        self.db = self.hub.db
        self.boss = accounts.create(self.db, "boss", PASSWORD)
        self.alice = accounts.create(self.db, "alice", PASSWORD)
        self.bob = accounts.create(self.db, "bob", PASSWORD)
        accounts.set_rights(self.db, self.bob["id"], {lib.id for lib in libs}, {"prive": {"view": False}})
        self.fill_films()
        self.db.replace_library("series", [item("s1", "show", lib="series", title="Serie")], [entry("e1", "s1", lib="series", season=1, number=1, title="Pilote"), entry("e2", "s1", lib="series", season=1, number=2)])
        self.db.replace_library("musique", [item("al", "album", lib="musique", title="Album")], [entry("t1", "al", kind="audio", lib="musique", title="Piste un")])
        self.db.replace_library("livres", [item("bk", "series", lib="livres", title="Livre")], [entry("k1", "bk", kind="book", lib="livres")])
        self.db.replace_library("prive", [item("p1", lib="prive", title="Secret")], [entry("v9", "p1", lib="prive")])

    def fill_films(self, titles=(("m1", "Alpha"), ("m2", "Beta"))):
        self.db.replace_library("films", [item(i, lib="films", title=t) for i, t in titles], [entry("v" + i[1:], i, lib="films") for i, _ in titles])

    def report(self, who, **body):
        return self.post(who, "/api/suggestions", body)

    def shown(self, reply):
        self.assertEqual(reply.status, 201, reply.body)
        return reply.json()


class TestReporting(ReportCase):
    def test_a_film_is_reported_as_a_bug_with_its_cause_and_no_text(self):
        created = self.shown(self.report(self.alice, item="m1", cause="image"))
        self.assertEqual({k: created[k] for k in ("category", "text", "status", "item", "entry", "cause", "subject", "link")},
                         {"category": "bug", "text": "", "status": "new", "item": "m1", "entry": "", "cause": "image", "subject": "Alpha", "link": {"item": "m1"}})
        self.assertNotIn("context", created)
        self.assertEqual(self.mine(self.alice), [created])

    def test_an_episode_a_track_and_a_book_are_reported_with_their_own_file(self):
        episode = self.shown(self.report(self.alice, item="s1", entry="e1", cause="subtitles", text="  Décalés de trois secondes "))
        self.assertEqual({k: episode[k] for k in ("text", "item", "entry", "cause", "subject", "link")},
                         {"text": "Décalés de trois secondes", "item": "s1", "entry": "e1", "cause": "subtitles", "subject": "Serie · Pilote", "link": {"item": "s1", "entry": "e1"}})
        track = self.shown(self.report(self.alice, item="al", entry="t1", cause="sound"))
        self.assertEqual((track["subject"], track["entry"]), ("Album · Piste un", "t1"))
        album = self.shown(self.report(self.alice, item="al", cause="other"))
        self.assertEqual((album["subject"], album["entry"], album["link"]), ("Album", "", {"item": "al"}))
        self.assertEqual(self.shown(self.report(self.alice, item="bk", entry="k1", cause="playback"))["item"], "bk")

    def test_the_subject_is_the_title_as_seen_and_is_bounded(self):
        self.fill_films((("m1", "T" * 300),))
        created = self.shown(self.report(self.alice, item="m1", cause="image"))
        self.assertEqual(created["subject"], "T" * 200)
        self.fill_films((("m1", "Renommé"),))
        self.assertEqual(self.mine(self.alice)[0]["subject"], "T" * 200)       # le titre qu'elle a vu, pas celui d'aujourd'hui

    def test_a_cause_is_required_and_must_be_known(self):
        for cause in ("image", "sound", "subtitles", "playback", "other"):
            with self.subTest(cause=cause):
                self.assertEqual(self.report(self.alice, item="m1", cause=cause).status, 201)
        before = self.snapshot()
        cases = {"sans cause": ({"item": "m1"}, CAUSE_REQUIRED),
                 "cause inconnue": ({"item": "m1", "cause": "smell"}, CAUSE_REQUIRED),
                 "cause vide": ({"item": "m1", "cause": ""}, CAUSE_REQUIRED),
                 "cause qui n'en est pas une": ({"item": "m1", "cause": 3}, CAUSE_REQUIRED),
                 "élément qui n'en est pas un": ({"item": 5, "cause": "image"}, "item : un identifiant est attendu"),
                 "fichier qui n'en est pas un": ({"item": "m1", "entry": 5, "cause": "image"}, "entry : un identifiant est attendu"),
                 "texte trop long": ({"item": "m1", "cause": "image", "text": "x" * 1001}, "texte trop long (1000 caractères au plus)"),
                 "texte qui n'en est pas un": ({"item": "m1", "cause": "image", "text": 4}, "texte : un texte est attendu")}
        for name, (body, message) in cases.items():
            with self.subTest(name):
                reply = self.post(self.alice, "/api/suggestions", body)
                self.assertEqual((reply.status, reply.json()), (400, {"error": message}))
        self.assertEqual(self.snapshot(), before)

    def test_a_file_of_another_item_or_an_unknown_one_is_refused_the_same_way(self):
        before = self.snapshot()
        for entry_id in ("v2", "e1", "nope", "v9"):                       # un autre élément, une autre bibliothèque, inconnu, un fichier que le compte ne voit pas
            with self.subTest(entry=entry_id):
                reply = self.report(self.bob, item="m1", entry=entry_id, cause="image")
                self.assertEqual((reply.status, reply.json()), (400, {"error": "ce fichier n'est pas dans cet élément"}))
        self.assertEqual(self.snapshot(), before)

    def test_an_item_the_account_cannot_see_answers_like_one_that_does_not_exist(self):
        before = self.snapshot()
        hidden = self.report(self.bob, item="p1", cause="image")
        unknown = self.report(self.bob, item="nope", cause="image")
        self.assertEqual((hidden.status, hidden.json()), (404, {"error": "élément inconnu"}))
        self.assertEqual((unknown.status, unknown.json()), (404, {"error": "élément inconnu"}))
        self.assertEqual(self.report(self.bob, item="p1", entry="v9", cause="image").status, 404)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.report(self.alice, item="p1", cause="image").status, 201)

    def test_a_category_given_with_an_item_is_not_used_and_cause_without_item_is_ignored(self):
        self.assertEqual(self.shown(self.report(self.alice, item="m1", cause="image", category="evolution"))["category"], "bug")
        plain = self.shown(self.post(self.alice, "/api/suggestions", {"category": "other", "text": "x", "cause": "image", "entry": "v1"}))
        self.assertEqual({k: plain[k] for k in REPORT_FIELDS}, {"item": "", "entry": "", "cause": "", "subject": ""})

    def test_without_item_a_suggestion_still_needs_its_text(self):
        reply = self.post(self.alice, "/api/suggestions", {"category": "bug", "cause": "image"})
        self.assertEqual((reply.status, reply.json()), (400, {"error": "le texte est vide"}))


class TestReportsRead(ReportCase):
    def test_a_vanished_item_leaves_the_subject_and_no_link_and_the_list_still_works(self):
        self.report(self.alice, item="m1", cause="image")
        self.report(self.alice, item="s1", entry="e2", cause="sound")
        self.fill_films((("m2", "Beta"),))
        self.db.replace_library("series", [item("s1", "show", lib="series", title="Serie")], [entry("e1", "s1", lib="series", season=1, number=1, title="Pilote")])
        for listed in (self.mine(self.alice), self.everything()):
            by_item = {s["item"]: s for s in listed}
            self.assertEqual((by_item["m1"]["subject"], by_item["m1"]["cause"], "link" in by_item["m1"]), ("Alpha", "image", False))
            self.assertEqual(by_item["s1"]["subject"], "Serie · Titre e2")
            self.assertEqual(by_item["s1"]["link"], {"item": "s1"})        # l'élément est là, le fichier signalé n'y est plus : le lien ne porte pas ce fichier

    def test_the_link_follows_what_the_reader_can_see(self):
        self.report(self.alice, item="p1", cause="image")
        self.assertEqual(self.mine(self.alice)[0]["link"], {"item": "p1"})
        accounts.set_rights(self.db, self.alice["id"], {"prive"}, {"prive": {"view": False}})
        self.assertNotIn("link", self.mine(self.alice)[0])                 # l'auteur ne voit plus l'élément : le sujet reste, pas de lien
        self.assertEqual(self.mine(self.alice)[0]["subject"], "Secret")
        self.assertEqual(self.everything()[0]["link"], {"item": "p1"})     # l'administrateur voit tout

    def test_the_context_is_the_last_decision_for_that_account_and_that_file_and_only_the_admin_reads_it(self):
        decide = self.hub.activity.decide
        decide(self.alice["id"], "alice", "203.0.113.7", "Mozilla Firefox/120 Windows", "pc", "v1", "Alpha", {"video": ["h264"], "height": 1080}, "direct", [])
        decide(self.alice["id"], "alice", "203.0.113.7", "Mozilla Firefox/120 Windows", "phone", "v1", "Alpha", {"video": ["h264"], "audio": ["aac"], "height": 720, "hdr": False}, "transcode", ["Audio DTS non lu"])
        decide(self.alice["id"], "alice", "203.0.113.7", "x", "tv", "v2", "Beta", {}, "remux", ["autre fichier"])
        decide(self.bob["id"], "bob", "198.51.100.9", "x", "tablet", "v1", "Alpha", {}, "direct", ["un autre compte"])
        self.report(self.alice, item="m1", entry="v1", cause="playback")
        self.assertNotIn("context", self.mine(self.alice)[0])
        context = self.everything()[0]["context"]
        self.assertEqual({k: context[k] for k in ("mode", "why", "device", "caps")}, {"mode": "transcode", "why": ["Audio DTS non lu"], "device": "phone", "caps": {"video": ["h264"], "audio": ["aac"], "height": 720, "hdr": False}})
        self.assertIsInstance(context["at"], int)
        self.assertEqual(set(context), {"at", "mode", "why", "device", "caps"})
        for text in (json.dumps(self.everything()), json.dumps(self.mine(self.alice)), json.dumps(self.snapshot(), default=str)):
            self.assertNotIn("203.0.113.7", text)
            self.assertNotIn("Firefox", text)

    def test_the_context_of_an_item_reported_without_a_file_comes_from_any_of_its_files_and_is_null_without_a_decision(self):
        self.report(self.alice, item="m2", cause="image")
        self.assertIsNone(self.everything()[0]["context"])
        self.hub.activity.decide(self.alice["id"], "alice", "203.0.113.7", "x", "pc", "v2", "Beta", {}, "direct", [])
        self.report(self.alice, item="m2", cause="image")
        self.assertEqual([s["context"] and s["context"]["mode"] for s in self.everything()], ["direct", None])

    def test_the_context_stays_small_whatever_the_player_said(self):
        self.hub.activity.decide(self.alice["id"], "alice", "203.0.113.7", "x", "pc", "v1", "Alpha", {"video": ["h264"] * 50, "audio": ["w" * 500] * 50}, "direct", ['"' * 400] * 50)       # les guillemets sont doublés une fois écrits en JSON : le pire cas
        self.report(self.alice, item="m1", cause="playback")
        self.assertLess(len(self.db.one("SELECT context FROM suggestions")[0]), 8000)

    def test_the_context_is_kept_in_plain_characters_not_escaped(self):
        self.hub.activity.decide(self.alice["id"], "alice", "203.0.113.7", "x", "pc", "v1", "Alpha", {}, "transcode", ["Piste audio « française » non lue"])
        self.report(self.alice, item="m1", entry="v1", cause="playback")
        raw = self.db.one("SELECT context FROM suggestions")[0]
        self.assertIn("« française »", raw)
        self.assertNotIn("\\u", raw)

    def test_everything_lists_all_suggestions_and_only_the_reports_when_asked(self):
        self.suggest(self.alice, "Une idée")
        self.report(self.alice, item="m1", cause="image")
        self.assertEqual([s["category"] for s in suggestions.everything(self.db)], ["bug", "improvement"])
        self.assertEqual([s["category"] for s in suggestions.everything(self.db, reports=True)], ["bug"])
        self.assertEqual(len(suggestions.everything(self.db, reports=False)), 2)

    def test_the_badge_counts_a_new_report_and_reports_are_filtered_for_the_admin(self):
        first = self.shown(self.report(self.alice, item="m1", cause="image"))
        self.suggest(self.bob, "Une idée")
        self.assertEqual(self.get(self.boss, "/api/state").json()["suggestions_new"], 2)
        self.assertEqual([s["id"] for s in self.everything("?reports=1")], [first["id"]])
        self.assertEqual(len(self.everything()), 2)
        self.set_status(first["id"], "done", "Corrigé")
        self.assertEqual(self.get(self.boss, "/api/state").json()["suggestions_new"], 1)
        self.assertEqual(self.mine(self.alice)[0]["reply"], "Corrigé")
        reply = self.get(self.alice, "/api/suggestions?reports=1")
        self.assertEqual((reply.status, reply.json()), (403, ADMIN_ONLY))

    def test_another_account_never_sees_a_report(self):
        self.report(self.alice, item="m1", cause="image", text="Flou")
        self.assertEqual(self.mine(self.bob), [])
        sid = self.mine(self.alice)[0]["id"]
        self.assertEqual(self.post(self.bob, f"/api/suggestions/{sid}/delete").status, 404)
        self.assertEqual(len(self.mine(self.alice)), 1)
        self.assertEqual(self.post(self.alice, f"/api/suggestions/{sid}/delete").json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
