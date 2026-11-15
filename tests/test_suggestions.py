"""Les suggestions : une idée ou une remarque sur le hub lui-même, envoyée par un compte à l'administrateur. Proposer, les catégories, l'absence de plafond, la confidentialité d'un compte à l'autre, l'état et la réponse de l'administrateur, le retrait, la suppression d'un compte, `/api/state` et la migration de la base."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.rig import MiniHub, call
from hub import accounts
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
        self.assertEqual(set(self.mine(self.alice)[0]), {"id", "category", "text", "status", "reply", "created", "updated"})

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
        self.assertEqual(database.SCHEMA_VERSION, 9)
        self.assertEqual(db.kv_get("schema"), 9)
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [(alice["id"], "e1", 12.5)])
        self.assertEqual(db.ratings_for(alice["id"]), {"m1": "up"})
        self.assertEqual([r["title"] for r in db.query("SELECT title FROM requests")], ["Sept"])
        self.assertEqual(db.query("SELECT * FROM suggestions"), [])
        self.assertEqual(Database(path).kv_get("schema"), 9)          # rouverte : rien n'est rejoué


if __name__ == "__main__":
    unittest.main()
