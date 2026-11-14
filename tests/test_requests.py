"""Les demandes de films et de séries : la liste d'attente que l'administrateur lit (le hub ne télécharge rien). Demander, rejoindre, retirer, le plafond, ce qui est déjà là, le refus, le droit « Peut demander », l'état « ajouté » calculé à la lecture, l'accueil, les cases grisées des sagas et des personnes, la confidentialité d'un compte à l'autre et la migration de la base."""

from __future__ import annotations

import json
import shutil
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from tests import fixtures
from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, missing
from hub import db as database
from hub import scan as scanning
from hub.db import Database

PASSWORD = "un mot de passe assez long"
UNKNOWN = {"error": "demande inconnue"}
FORBIDDEN = "tu n'as pas le droit de demander des films ni des séries"


def film(tmdb, title=None, year="2001"):
    return {"title": title or f"Film {tmdb}", "year": year, "date": f"{year}-01-01", "poster": ""}


class RequestCase(unittest.TestCase):
    """Un hub à trois bibliothèques vidéo (films, séries, et « prive », un film que Bob ne voit pas), un administrateur et deux comptes, Alice et Bob."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.libs = [config.Library(ident, ident.title(), kind, self.root / ident) for ident, kind in (("films", "movies"), ("series", "shows"), ("prive", "movies"))]
        for lib in self.libs:
            lib.path.mkdir()
        self.hub = MiniHub(self.root, self.libs)
        self.db = self.hub.db
        self.boss = accounts.create(self.db, "boss", PASSWORD)
        self.alice = accounts.create(self.db, "alice", PASSWORD)
        self.bob = accounts.create(self.db, "bob", PASSWORD)
        accounts.set_rights(self.db, self.bob["id"], {lib.id for lib in self.libs}, {"prive": {"view": False}})
        self.fill("films", [item("m1", lib="films", title="Un", grp="Saga", meta={"tmdb": "movie/1", "directors": ["Jeanne Dupont"], "person_ids": {"Jeanne Dupont": 77}})],
                  [entry("v1", "m1", lib="films")])
        missing.save(self.db, {"films": {str(n): film(n) for n in (2, 3, 4)}, "collections": {"Saga": {"id": 9, "films": [1, 2, 3], "at": int(time.time())}}})
        self.db.kv_set("personinfo:77", {"at": int(time.time()), "films": [{"tmdb": n, "role": "director", **film(n)} for n in (4, 3, 2, 1)]})

    def fill(self, lib, items, entries=()):
        self.db.replace_library(lib, items, list(entries))

    def headers(self, who):
        return self.hub.cookie(accounts.by_id(self.db, who["id"]))

    def get(self, who, target):
        _, reply = call(self.hub, "GET", target, headers=self.headers(who))
        return reply

    def post(self, who, target, body=None):
        _, reply = call(self.hub, "POST", target, {} if body is None else body, self.headers(who))
        return reply

    def ask(self, who, tmdb, kind="movie", **more):
        return self.post(who, "/api/requests", {"kind": kind, "tmdb": tmdb, **more})

    def stored(self):
        return [tuple(r) for r in self.db.query("SELECT r.kind, r.tmdb, r.status, u.user_id FROM requests r JOIN request_users u ON u.request_id = r.id ORDER BY r.tmdb, u.user_id")]

    def mine(self, who):
        reply = self.get(who, "/api/requests?mine=1")
        self.assertEqual(reply.status, 200)
        return reply.json()["requests"]

    def everything(self, query=""):
        reply = self.get(self.boss, "/api/requests" + query)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json()["requests"]

    def set_status(self, rid, status, reason=None, who=None):
        return self.post(who or self.boss, f"/api/requests/{rid}/status", {"status": status, **({"reason": reason} if reason is not None else {})})

    def arrive(self, lib, arrivals, kind="movie"):
        """Ces films (ou séries) `arrivals` ([(élément, identifiant TMDB)]) arrivent d'un coup dans la bibliothèque `lib`."""
        rows = [dict(r) for r in self.db.query("SELECT * FROM items WHERE lib=?", (lib,))]
        new = [item(ident, kind, lib=lib, title=f"Arrivé {tmdb}", meta={"tmdb": f"movie/{tmdb}"} if kind == "movie" else {"tmdb": tmdb}) for ident, tmdb in arrivals]
        self.fill(lib, rows + new, [dict(r) for r in self.db.query("SELECT * FROM entries WHERE lib=?", (lib,))])

    def own(self, lib, ident, tmdb, kind="movie"):
        """Le film (ou la série) `tmdb` arrive dans la bibliothèque `lib`, sous l'élément `ident`."""
        self.arrive(lib, [(ident, tmdb)], kind)

    def saga_missing(self, who):
        sagas = self.get(who, "/api/item/m1").json()["sagas"]
        return {f["tmdb"]: f.get("request") for f in sagas[0]["missing"]}

    def person_missing(self, who):
        return {f["tmdb"]: f.get("request") for f in self.get(who, "/api/person?name=" + quote("Jeanne Dupont")).json()["missing"]}

    def statements_of(self, who, target):
        """Les requêtes SQL passées pendant cet appel (le gestionnaire tourne dans ce fil : sa connexion est la nôtre)."""
        statements, conn = [], self.db.connection()
        conn.set_trace_callback(statements.append)
        try:
            self.assertEqual(self.get(who, target).status, 200, target)
        finally:
            conn.set_trace_callback(None)
        return statements

    def reads_of(self, who, target):
        """Combien de lectures des demandes (tables `requests` et `request_users`) pendant la requête."""
        return sum("request" in s and s.lstrip().upper().startswith("SELECT") for s in self.statements_of(who, target))

    def together(self, jobs):
        """Les appels `jobs` ([(méthode, adresse, corps, en-têtes)]), chacun dans son fil, lâchés au même signal ; [(gestionnaire, réponse)] dans l'ordre."""
        barrier, out = threading.Barrier(len(jobs)), [None] * len(jobs)

        def run(i, job):
            try:
                barrier.wait(timeout=30)
                out[i] = call(self.hub, *job)
            except Exception as e:      # un fil qui échoue se voit au rendu, pas dans un fil perdu
                out[i] = e
        threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual([o for o in out if not isinstance(o, tuple)], [])
        return out

    def snapshot(self):
        """Tout ce que les demandes (et le droit de demander) ont en base : deux instantanés égaux, rien n'a changé."""
        return {table: [tuple(r) for r in self.db.query(f"SELECT * FROM {table} ORDER BY 1, 2")] for table in ("requests", "request_users")} | {
            "droits": [tuple(r) for r in self.db.query("SELECT id, can_request FROM users ORDER BY id")]}

    def crowd(self, count):
        """`count` demandes de plus, faites directement en base (poser des centaines de lignes par HTTP n'apprendrait rien de plus) : des films, une série sur sept, une refusée sur onze, chacune par un à trois des comptes (Alice, Bob, l'administrateur et deux comptes disparus). Leurs numéros TMDB suivent ceux d'avant, à partir de 2."""
        start = self.db.one("SELECT COUNT(*) FROM requests")[0]
        who, now = [self.alice["id"], self.bob["id"], self.boss["id"], "disparu1", "disparu2"], int(time.time())
        asked, askers = [], []
        for n in range(start, start + count):
            asked.append((n + 1, "show" if n % 7 == 6 else "movie", n + 2, f"Titre {n}", "2001", "declined" if n % 11 == 10 else "pending", now - n, now - n))
            askers += [(n + 1, who[(n + k) % 5], "", now - n, 0) for k in range(1 + n % 3)]
        with self.db.write_lock:
            conn = self.db.connection()
            conn.executemany("INSERT INTO requests(id, kind, tmdb, title, year, status, created, updated) VALUES(?,?,?,?,?,?,?,?)", asked)
            conn.executemany("INSERT INTO request_users(request_id, user_id, note, created, ack) VALUES(?,?,?,?,?)", askers)
            conn.commit()


class TestAsking(RequestCase):
    def test_a_film_is_requested_then_joined_and_asking_twice_changes_nothing(self):
        first = self.ask(self.alice, 2, note="Pour Noël")
        self.assertEqual(first.status, 201)
        rid = first.json()["id"]
        self.assertEqual(first.json(), {"id": rid, "kind": "movie", "tmdb": 2, "status": "pending", "mine": True})
        joined = self.ask(self.bob, 2)
        self.assertEqual((joined.status, joined.json()), (200, {"id": rid, "kind": "movie", "tmdb": 2, "status": "pending", "mine": True}))
        again = self.ask(self.alice, 2, note="autre message")
        self.assertEqual((again.status, again.json()["id"]), (200, rid))
        self.assertEqual(self.stored(), sorted([("movie", 2, "pending", self.alice["id"]), ("movie", 2, "pending", self.bob["id"])]))
        self.assertEqual(self.mine(self.alice)[0]["note"], "Pour Noël")

    def test_a_show_is_accepted_and_kept_apart_from_a_film_of_the_same_number(self):
        self.assertEqual(self.ask(self.alice, 2, "show", title="Une série", year="2019").status, 201)
        self.assertEqual(self.ask(self.alice, 2).status, 201)
        shows = [r for r in self.mine(self.alice) if r["kind"] == "show"]
        self.assertEqual([(r["tmdb"], r["title"], r["year"], r["status"]) for r in shows], [(2, "Une série", "2019", "pending")])

    def test_title_and_year_come_from_what_the_server_knows_before_the_body(self):
        self.ask(self.alice, 3, title="Autre chose", year="1950")
        self.db.kv_set("personinfo:88", {"at": 0, "films": [{"tmdb": 40, "role": "actor", **film(40, "Seulement chez une personne", "1990")}]})
        self.ask(self.alice, 40, title="Faux", year="1950")
        self.ask(self.alice, 50, title="  Inconnu du serveur ", year=1977)
        found = {r["tmdb"]: (r["title"], r["year"]) for r in self.mine(self.alice)}
        self.assertEqual(found, {3: ("Film 3", "2001"), 40: ("Seulement chez une personne", "1990"), 50: ("Inconnu du serveur", "1977")})

    def test_a_poster_kept_by_the_enrichment_is_the_cover(self):
        (self.hub.cfg.data / "tmdb").mkdir()
        (self.hub.cfg.data / "tmdb" / missing.poster_name(2)).write_bytes(b"jpeg")
        self.ask(self.alice, 2)
        self.ask(self.alice, 3)
        self.assertEqual({r["tmdb"]: r["cover"] for r in self.mine(self.alice)}, {2: "/img/film/2", 3: ""})

    def test_a_bad_body_is_a_400_in_french_and_nothing_is_stored(self):
        cases = [({"kind": "album", "tmdb": 2}, "sorte de demande inconnue (movie ou show)"), ({"tmdb": 2}, "sorte de demande inconnue (movie ou show)")]
        cases += [({"kind": "movie", "tmdb": bad}, "identifiant TMDB attendu : un entier positif") for bad in ("2", 0, -3, True, 1.5, None, [2], 1 << 60)]
        cases += [({"kind": "movie", "tmdb": 2, "note": "x" * 201}, "message trop long (200 caractères au plus)"), ({"kind": "movie", "tmdb": 2, "note": 5}, "message : un texte est attendu"),
                  ({"kind": "movie", "tmdb": 99, "title": "x" * 201}, "titre trop long (200 caractères au plus)"), ({"kind": "movie", "tmdb": 99, "year": "99"}, "année attendue : 4 chiffres"),
                  ({"kind": "movie", "tmdb": 99, "year": True}, "année attendue : 4 chiffres")]
        for body, message in cases:
            reply = self.post(self.alice, "/api/requests", body)
            self.assertEqual((reply.status, reply.json()), (400, {"error": message}), body)
        for raw in (b"", b"[1]", b"pas du json"):
            _, reply = call(self.hub, "POST", "/api/requests", raw, self.headers(self.alice))
            self.assertEqual(reply.status, 400, raw)
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.ask(self.alice, 2, note="x" * 200).status, 201)            # 200 caractères passent

    def test_the_body_is_limited_like_the_others(self):
        _, reply = call(self.hub, "POST", "/api/requests", b"", {**self.headers(self.alice), "Content-Length": str((1 << 20) + 1)})
        self.assertEqual(reply.status, 413)
        self.assertEqual(self.stored(), [])

    def test_without_a_session_it_is_a_401(self):
        _, reply = call(self.hub, "POST", "/api/requests", {"kind": "movie", "tmdb": 2})
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.stored(), [])


class TestLimits(RequestCase):
    def test_beyond_the_ceiling_a_request_is_refused_joining_too_but_the_admin_has_none(self):
        self.hub.cfg.request_max = 2
        self.ask(self.alice, 2)
        self.ask(self.alice, 3)
        self.ask(self.bob, 4)
        for tmdb in (5, 4):
            reply = self.ask(self.alice, tmdb)
            self.assertEqual((reply.status, reply.json()), (409, {"error": "trop de demandes en attente (2)"}), tmdb)
        self.assertEqual(self.ask(self.alice, 2).status, 200)                       # redemander ce qu'on a déjà demandé n'ajoute rien
        rid = self.mine(self.alice)[0]["id"]
        self.set_status(rid, "added")
        self.assertEqual(self.ask(self.alice, 5).status, 201)                       # une demande arrivée ne compte plus
        for tmdb in range(10, 15):
            self.assertEqual(self.ask(self.boss, tmdb).status, 201)

    def test_a_film_already_in_a_library_the_account_sees_is_a_409_with_its_item(self):
        reply = self.ask(self.alice, 1)
        self.assertEqual((reply.status, reply.json()), (409, {"error": "déjà dans la médiathèque", "item": "m1"}))
        self.own("prive", "p1", 2)
        self.assertEqual(self.ask(self.alice, 2).json(), {"error": "déjà dans la médiathèque", "item": "p1"})
        self.assertEqual(self.stored(), [])

    def test_a_film_in_a_library_the_account_cannot_see_is_asked_and_already_added(self):
        self.own("prive", "p1", 2)
        reply = self.ask(self.bob, 2)
        self.assertEqual((reply.status, reply.json()["status"]), (201, "added"))
        shown = self.mine(self.bob)[0]
        self.assertEqual(shown["status"], "added")
        self.assertNotIn("item", shown)                                             # il ne la voit pas : pas de lien
        self.assertNotIn("p1", json.dumps(self.mine(self.bob)))
        self.assertEqual(self.get(self.bob, "/api/home").json()["requests_ready"], [])

    def test_a_show_already_there_is_recognised_by_its_tmdb_sheet(self):
        self.own("series", "s1", 1399, "show")
        self.assertEqual(self.ask(self.alice, 1399, "show").json(), {"error": "déjà dans la médiathèque", "item": "s1"})
        self.fill("series", [item("s2", "show", lib="series", meta={"tmdb_pinned": "1400"})])
        self.assertEqual(self.ask(self.alice, 1400, "show").json()["item"], "s2")

    def test_a_film_filed_in_a_library_of_shows_is_recognised_by_its_file(self):
        self.fill("series", [item("s1", "show", lib="series")], [entry("e1", "s1", lib="series", meta={"info": {"tmdb": "movie/2"}})])
        self.assertEqual(self.ask(self.alice, 2).json(), {"error": "déjà dans la médiathèque", "item": "s1"})

    def test_a_declined_request_blocks_new_ones_until_it_is_put_back(self):
        rid = self.ask(self.alice, 2).json()["id"]
        reply = self.set_status(rid, "declined", "Introuvable en bonne qualité")
        self.assertEqual((reply.status, reply.json()["status"], reply.json()["reason"]), (200, "declined", "Introuvable en bonne qualité"))
        for who in (self.alice, self.bob):
            refused = self.ask(who, 2)
            self.assertEqual((refused.status, refused.json()), (409, {"error": "cette demande a été refusée : Introuvable en bonne qualité"}))
        self.set_status(rid, "declined")
        self.assertEqual(self.ask(self.bob, 2).json(), {"error": "cette demande a été refusée"})
        self.assertEqual(self.mine(self.alice)[0]["status"], "declined")
        self.set_status(rid, "pending")
        self.assertEqual((self.ask(self.bob, 2).status, self.mine(self.alice)[0]["reason"]), (200, ""))

    def test_a_status_is_set_by_the_admin_only_and_must_be_known(self):
        rid = self.ask(self.alice, 2).json()["id"]
        reply = self.set_status(rid, "added", who=self.alice)
        self.assertEqual((reply.status, reply.json()), (403, {"error": "réservé à l'administrateur"}))
        for bad in ("done", "", None, 3):
            reply = self.post(self.boss, f"/api/requests/{rid}/status", {"status": bad})
            self.assertEqual((reply.status, reply.json()), (400, {"error": "statut inconnu (pending, added ou declined)"}), bad)
        self.assertEqual(self.set_status(rid, "declined", "x" * 201).json(), {"error": "motif trop long (200 caractères au plus)"})
        self.assertEqual(self.set_status(999, "added").status, 404)
        self.assertEqual(self.mine(self.alice)[0]["status"], "pending")


class TestWithdrawing(RequestCase):
    def test_each_requester_leaves_and_the_last_one_takes_the_request_away(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.ask(self.bob, 2)
        self.assertEqual((self.post(self.alice, f"/api/requests/{rid}/delete").status), 200)
        self.assertEqual(self.stored(), [("movie", 2, "pending", self.bob["id"])])
        again = self.post(self.alice, f"/api/requests/{rid}/delete")
        self.assertEqual((again.status, again.json()), (404, UNKNOWN))
        self.assertEqual(self.post(self.bob, f"/api/requests/{rid}/delete").json(), {"ok": True})
        self.assertEqual(self.db.query("SELECT * FROM requests"), [])

    def test_the_request_of_another_answers_like_an_unknown_one(self):
        rid = self.ask(self.alice, 2).json()["id"]
        theirs = self.post(self.bob, f"/api/requests/{rid}/delete")
        unknown = self.post(self.bob, "/api/requests/999/delete")
        self.assertEqual((theirs.status, theirs.body), (unknown.status, unknown.body))
        self.assertEqual(theirs.json(), UNKNOWN)
        self.assertEqual(self.stored(), [("movie", 2, "pending", self.alice["id"])])

    def test_the_admin_deletes_the_whole_request(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.ask(self.bob, 2)
        self.assertEqual(self.post(self.boss, f"/api/requests/{rid}/delete").json(), {"ok": True})
        self.assertEqual((self.db.query("SELECT * FROM requests"), self.db.query("SELECT * FROM request_users")), ([], []))
        self.assertEqual(self.ask(self.alice, 2).status, 201)                        # supprimée, elle peut être refaite

    def test_deleting_an_account_takes_it_off_its_requests(self):
        shared = self.ask(self.alice, 2).json()["id"]
        self.ask(self.bob, 2)
        alone = self.ask(self.alice, 3).json()["id"]
        accounts.delete(self.db, self.alice["id"])
        self.assertEqual(self.stored(), [("movie", 2, "pending", self.bob["id"])])
        self.assertEqual([r["id"] for r in self.db.query("SELECT id FROM requests")], [shared])
        self.assertNotEqual(shared, alone)


class TestRight(RequestCase):
    def test_the_right_is_given_by_default_taken_back_by_the_admin_and_refused_with_a_403(self):
        self.assertTrue(self.get(self.alice, "/api/state").json()["can_request"])
        reply = self.post(self.boss, f"/api/user/{self.alice['id']}/request", {"request": False})
        self.assertEqual((reply.status, reply.json()["can_request"], reply.json()["id"]), (200, False, self.alice["id"]))
        refused = self.ask(self.alice, 2)
        self.assertEqual((refused.status, refused.json()), (403, {"error": FORBIDDEN}))
        state = self.get(self.alice, "/api/state").json()
        self.assertEqual((state["can_request"], state["user"]["can_request"]), (False, False))
        self.assertEqual({u["name"]: u["can_request"] for u in self.get(self.boss, "/api/users").json()["users"]}, {"boss": True, "alice": False, "bob": True})
        self.assertEqual(self.stored(), [])
        self.post(self.boss, f"/api/user/{self.alice['id']}/request", {"request": True})
        self.assertEqual(self.ask(self.alice, 2).status, 201)

    def test_the_right_is_set_like_the_upload_one(self):
        reply = self.post(self.boss, f"/api/user/{self.boss['id']}/request", {"request": False})
        self.assertEqual((reply.status, reply.json()), (400, {"error": "un administrateur peut toujours demander"}))
        for bad in ("non", 0, None):
            reply = self.post(self.boss, f"/api/user/{self.alice['id']}/request", {"request": bad})
            self.assertEqual((reply.status, reply.json()), (400, {"error": "« request » vaut vrai ou faux"}), bad)
        self.assertEqual(self.post(self.boss, "/api/user/inconnu/request", {"request": True}).status, 404)
        self.assertEqual(self.post(self.bob, f"/api/user/{self.alice['id']}/request", {"request": False}).status, 403)
        self.assertTrue(accounts.public(accounts.by_id(self.db, self.alice["id"]))["can_request"])

    def test_the_admin_always_asks_whatever_the_column_says(self):
        self.db.execute("UPDATE users SET can_request=0 WHERE id=?", (self.boss["id"],))
        self.assertEqual(self.ask(self.boss, 2).status, 201)

    def test_the_state_counts_pending_requests_for_the_admin_only(self):
        self.ask(self.alice, 2)
        self.ask(self.bob, 2)
        self.ask(self.bob, 3)
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], 2)
        self.assertNotIn("requests_pending", self.get(self.alice, "/api/state").json())
        self.own("films", "m3", 3)
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], 1)


class TestPrivacy(RequestCase):
    def test_an_account_sees_its_requests_and_only_that_someone_else_asked(self):
        self.ask(self.alice, 2, note="message d'Alice")
        self.ask(self.bob, 2, note="message de Bob")
        self.ask(self.alice, 3, note="seulement Alice")
        mine = self.mine(self.bob)
        self.assertEqual([(r["tmdb"], r["note"], r["mine"]) for r in mine], [(2, "message de Bob", True)])
        self.assertEqual(self.saga_missing(self.bob), {2: {"status": "pending", "mine": True}, 3: {"status": "pending", "mine": False}})
        self.assertEqual(self.person_missing(self.bob), {4: None, 3: {"status": "pending", "mine": False}, 2: {"status": "pending", "mine": True}})
        library = self.get(self.bob, "/api/library/films?group=Saga").json()["missing"]
        self.assertEqual({f["tmdb"]: f.get("request") for f in library}, {2: {"status": "pending", "mine": True}, 3: {"status": "pending", "mine": False}})
        for target in ("/api/requests?mine=1", "/api/item/m1", "/api/person?name=" + quote("Jeanne Dupont"), "/api/library/films?group=Saga", "/api/home", "/api/state"):
            text = self.get(self.bob, target).body.decode()
            for secret in ("alice", self.alice["id"], "message d'Alice", "seulement Alice"):
                self.assertNotIn(secret, text, (target, secret))

    def test_a_film_nobody_asked_has_no_request_field(self):
        self.assertEqual(self.saga_missing(self.alice), {2: None, 3: None})
        self.assertNotIn('"request"', self.get(self.alice, "/api/person?name=" + quote("Jeanne Dupont")).body.decode())

    def test_the_full_list_is_for_the_admin_only(self):
        self.ask(self.alice, 2, note="message d'Alice")
        self.ask(self.bob, 2)
        for target in ("/api/requests", "/api/requests?status=pending", "/api/requests?mine=0"):
            reply = self.get(self.alice, target)
            self.assertEqual((reply.status, reply.json()), (403, {"error": "réservé à l'administrateur"}), target)
        listed = self.everything()
        self.assertEqual([(u["name"], u["note"]) for u in listed[0]["users"]], [("alice", "message d'Alice"), ("bob", "")])
        self.assertEqual((listed[0]["mine"], listed[0]["status"], listed[0]["title"]), (False, "pending", "Film 2"))

    def test_the_full_list_filters_by_status(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.ask(self.alice, 3)
        self.set_status(rid, "declined", "non")
        self.assertEqual([r["tmdb"] for r in self.everything("?status=declined")], [2])
        self.assertEqual([r["tmdb"] for r in self.everything("?status=pending")], [3])
        reply = self.get(self.boss, "/api/requests?status=bof")
        self.assertEqual((reply.status, reply.json()), (400, {"error": "statut inconnu (pending, added ou declined)"}))


class TestArrival(RequestCase):
    def test_a_request_turns_added_when_the_film_arrives_and_stays_so(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.own("films", "m2", 2)
        self.assertEqual(self.stored(), [("movie", 2, "pending", self.alice["id"])])          # rien ne tourne en fond : c'est la lecture qui le voit
        shown = self.mine(self.alice)[0]
        self.assertEqual((shown["status"], shown["item"], shown["cover"]), ("added", "m2", "/img/item/m2"))
        self.assertEqual(self.stored(), [("movie", 2, "added", self.alice["id"])])
        self.fill("films", [dict(r) for r in self.db.query("SELECT * FROM items WHERE id='m1'")])
        self.assertEqual((self.mine(self.alice)[0]["status"], self.everything()[0]["id"]), ("added", rid))      # le film reparti, la demande reste arrivée

    def test_the_home_lists_what_arrived_until_it_is_acknowledged(self):
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])
        rid = self.ask(self.alice, 2).json()["id"]
        self.ask(self.bob, 2)
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])
        self.own("films", "m2", 2)
        ready = self.get(self.alice, "/api/home").json()["requests_ready"]
        self.assertEqual([(r["id"], r["kind"], r["tmdb"], r["title"], r["year"], r["item"], r["card"]["id"], r["card"]["cover"]) for r in ready],
                         [(rid, "movie", 2, "Film 2", "2001", "m2", "m2", "/img/item/m2")])
        self.assertEqual(self.post(self.alice, f"/api/requests/{rid}/ack").json(), {"ok": True})
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])
        self.assertEqual(len(self.get(self.bob, "/api/home").json()["requests_ready"]), 1)          # chacun acquitte pour soi
        self.assertEqual(self.mine(self.alice)[0]["ack"], True)

    def test_an_ack_on_a_request_that_is_not_ours_is_a_404(self):
        rid = self.ask(self.alice, 2).json()["id"]
        for target in (f"/api/requests/{rid}/ack", "/api/requests/999/ack"):
            reply = self.post(self.bob, target)
            self.assertEqual((reply.status, reply.json()), (404, UNKNOWN), target)

    def test_an_arrival_marked_by_the_admin_without_the_file_has_no_item(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.set_status(rid, "added")
        self.assertNotIn("item", self.mine(self.alice)[0])
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])

    def test_the_admin_can_put_back_an_arrival_and_the_next_one_shows_again(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.own("films", "m2", 2)
        self.mine(self.alice)
        self.post(self.alice, f"/api/requests/{rid}/ack")
        self.set_status(rid, "declined", "mauvais fichier")
        self.assertEqual(self.mine(self.alice)[0]["status"], "declined")             # un refus n'est pas défait par la médiathèque
        self.set_status(rid, "added")
        self.assertEqual(len(self.get(self.alice, "/api/home").json()["requests_ready"]), 1)


    def test_a_show_turns_added_when_its_sheet_arrives(self):
        rid = self.ask(self.alice, 1399, "show", title="Une série").json()["id"]
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])
        self.own("series", "s1", 1399, "show")
        ready = self.get(self.alice, "/api/home").json()["requests_ready"]
        self.assertEqual([(r["id"], r["kind"], r["tmdb"], r["item"], r["card"]["id"]) for r in ready], [(rid, "show", 1399, "s1", "s1")])
        self.assertEqual((self.mine(self.alice)[0]["status"], self.mine(self.alice)[0]["item"]), ("added", "s1"))

    def test_an_arrival_in_a_library_the_account_cannot_see_waits_for_one_it_sees(self):
        rid = self.ask(self.bob, 2).json()["id"]
        self.own("prive", "p1", 2)
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], 0)
        self.assertEqual(self.everything()[0]["item"], "p1")                          # l'administrateur voit tout
        shown = self.mine(self.bob)[0]
        self.assertEqual(shown["status"], "added")
        self.assertNotIn("item", shown)
        self.assertEqual(self.get(self.bob, "/api/home").json()["requests_ready"], [])
        self.own("films", "m2", 2)                                                    # puis dans une bibliothèque qu'il voit
        self.assertEqual(self.mine(self.bob)[0]["item"], "m2")
        self.assertEqual([r["id"] for r in self.get(self.bob, "/api/home").json()["requests_ready"]], [rid])

    def test_an_acknowledged_arrival_does_not_come_back_when_the_film_leaves_and_returns(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.own("films", "m2", 2)
        self.assertEqual(len(self.get(self.alice, "/api/home").json()["requests_ready"]), 1)
        self.post(self.alice, f"/api/requests/{rid}/ack")
        self.fill("films", [dict(r) for r in self.db.query("SELECT * FROM items WHERE id='m1'")])
        self.own("films", "m2", 2)
        self.assertEqual((self.get(self.alice, "/api/home").json()["requests_ready"], self.mine(self.alice)[0]["status"]), ([], "added"))

    def test_putting_back_a_request_whose_film_is_there_makes_it_arrive_again_on_the_next_read(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.own("films", "m2", 2)
        self.mine(self.alice)
        self.post(self.alice, f"/api/requests/{rid}/ack")
        reply = self.set_status(rid, "pending")                                       # le film est là : la lecture le voit aussitôt
        self.assertEqual((reply.status, reply.json()["status"], reply.json()["item"]), (200, "added", "m2"))
        self.assertEqual([r["id"] for r in self.get(self.alice, "/api/home").json()["requests_ready"]], [rid])

    def test_readers_at_the_same_time_all_see_the_arrival_and_it_is_written_once(self):
        self.ask(self.alice, 2)
        self.ask(self.bob, 2)
        self.own("films", "m2", 2)
        reads = [("GET", "/api/requests?mine=1", None, self.headers(self.alice)), ("GET", "/api/requests?mine=1", None, self.headers(self.bob)), ("GET", "/api/requests", None, self.headers(self.boss)),
                 ("GET", "/api/home", None, self.headers(self.alice)), ("GET", "/api/state", None, self.headers(self.boss)), ("GET", "/api/item/m1", None, self.headers(self.bob))]
        out = self.together(reads)
        self.assertEqual([reply.status for _, reply in out], [200] * len(reads))
        self.assertEqual([r["status"] for r in out[0][1].json()["requests"] + out[2][1].json()["requests"]], ["added", "added"])
        self.assertEqual(self.stored(), sorted([("movie", 2, "added", self.alice["id"]), ("movie", 2, "added", self.bob["id"])]))


class TestAtTheSameTime(RequestCase):
    """Plusieurs fils à la fois, comme le serveur en a : une seule ligne par film, jamais de 500 ni de violation UNIQUE remontée."""

    def asks(self, who, tmdb):
        return ("POST", "/api/requests", {"kind": "movie", "tmdb": tmdb}, self.headers(who))

    def orphans(self):
        """Des demandeurs sans demande, ou des demandes sans demandeur."""
        return ([tuple(r) for r in self.db.query("SELECT request_id FROM request_users WHERE request_id NOT IN (SELECT id FROM requests)")],
                [tuple(r) for r in self.db.query("SELECT id FROM requests WHERE id NOT IN (SELECT request_id FROM request_users)")])

    def test_accounts_asking_the_same_film_at_once_share_one_request(self):
        for tmdb in range(100, 108):
            out = self.together([self.asks(who, tmdb) for who in (self.alice, self.bob, self.boss)])
            self.assertEqual(sorted(reply.status for _, reply in out), [200, 200, 201], tmdb)          # un seul crée, les autres rejoignent
            self.assertEqual(len({reply.json()["id"] for _, reply in out}), 1, tmdb)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 8)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM request_users")[0], 24)

    def test_the_same_account_asking_twice_at_once_is_one_request_and_one_row(self):
        for tmdb in range(100, 106):
            out = self.together([self.asks(self.alice, tmdb), self.asks(self.alice, tmdb)])
            self.assertEqual(sorted(reply.status for _, reply in out), [200, 201], tmdb)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 6)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM request_users")[0], 6)

    def test_the_ceiling_holds_when_one_account_asks_many_films_at_once(self):
        self.hub.cfg.request_max = 2
        out = self.together([self.asks(self.alice, tmdb) for tmdb in range(100, 106)])
        self.assertEqual(sorted(reply.status for _, reply in out), [201, 201, 409, 409, 409, 409])
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM request_users WHERE user_id=?", (self.alice["id"],))[0], 2)
        self.assertIn(self.ask(self.bob, 100).status, (200, 201))                                    # le plafond de l'une n'est pas celui de l'autre

    def test_leaving_and_joining_at_once_never_leaves_an_orphan(self):
        for tmdb in range(100, 108):
            rid = self.ask(self.alice, tmdb).json()["id"]
            out = self.together([("POST", f"/api/requests/{rid}/delete", {}, self.headers(self.alice)), self.asks(self.bob, tmdb)])
            self.assertEqual((out[0][1].status, out[1][1].status in (200, 201)), (200, True), tmdb)
            self.assertEqual(self.orphans(), ([], []), tmdb)
        self.assertEqual(self.stored(), [("movie", n, "pending", self.bob["id"]) for n in range(100, 108)])      # Bob l'a, quel qu'ait été l'ordre


class TestDamagedBodies(RequestCase):
    """Sur chaque route qui écrit, un corps qui n'est pas ce qu'on attend n'efface ni ne change rien."""
    OBJECT = {"error": "le corps doit être un objet JSON"}
    BODIES = {"vide": b"", "pas du json": b"pas du json", "liste": b"[1]", "nombre": b"42", "texte": b'"texte"', "null": b"null", "UTF-8 invalide": b'\xff\xfe{"kind": "movie"}',
              "tronqué": b'{"kind": "movie", "tmdb"'}
    DEEP = b"[" * 200_000                                   # du JSON emboîté sans fin : l'analyse déborde la pile

    def setUp(self):
        super().setUp()
        self.rid = self.ask(self.alice, 2, note="Pour Noël").json()["id"]
        self.ask(self.bob, 2)
        self.declined = self.ask(self.alice, 3).json()["id"]
        self.set_status(self.declined, "declined", "non")
        self.post(self.bob, f"/api/requests/{self.rid}/ack")
        self.before = self.snapshot()

    def writes(self):
        """(compte, adresse) de chaque route d'écriture des demandes."""
        return [(self.alice, "/api/requests"), (self.boss, f"/api/requests/{self.rid}/status"), (self.boss, f"/api/user/{self.alice['id']}/request"),
                (self.alice, f"/api/requests/{self.rid}/delete"), (self.alice, f"/api/requests/{self.rid}/ack")]

    def test_a_body_that_is_not_an_object_is_a_400_and_changes_nothing(self):
        expected = {"/api/requests": self.OBJECT, f"/api/requests/{self.rid}/status": self.OBJECT, f"/api/user/{self.alice['id']}/request": {"error": "« request » vaut vrai ou faux"}}
        for who, target in self.writes()[:3]:
            for name, raw in self.BODIES.items():
                with self.subTest(target=target, body=name):
                    _, reply = call(self.hub, "POST", target, raw, self.headers(who))
                    self.assertEqual((reply.status, reply.json()), (400, expected[target]))
                    self.assertEqual(self.snapshot(), self.before)

    @unittest.expectedFailure
    def test_a_body_nested_without_end_is_never_a_500(self):
        """DÉFAUT CONNU (hub/server/app.py, `safe_json` ligne 87, utilisé par `body_json`) : `safe_json` n'attrape que ValueError, or `json.loads` d'un corps `[[[[…` lève RecursionError : 500 « erreur interne : Stack overflow » au lieu d'un corps ignoré ou d'un 400. Les routes `strict_body_dict` (création, statut) attrapent déjà RecursionError ; celles qui passent par `body_json` (droit « Peut demander », retrait, acquittement, et toutes les autres routes POST) non. Rien n'est modifié en base, mais c'est une 500 sur une entrée d'un client."""
        failed = []
        for who, target in self.writes():
            _, reply = call(self.hub, "POST", target, self.DEEP, self.headers(who))
            if reply.status >= 500:
                failed.append((target, reply.status))
        self.assertEqual(failed, [])

    def test_a_body_too_big_is_a_413_and_changes_nothing(self):
        for who, target in self.writes():
            for name, raw, more in (("en-tête", b"", {"Content-Length": str((1 << 20) + 1)}), ("octets", b" " * ((1 << 20) + 1), {})):
                with self.subTest(target=target, body=name):
                    _, reply = call(self.hub, "POST", target, raw, {**self.headers(who), **more})
                    self.assertEqual((reply.status, reply.json()), (413, {"error": "corps trop gros"}))
                    self.assertEqual(self.snapshot(), self.before)

    def test_an_unreadable_length_is_a_400_and_changes_nothing(self):
        for who, target in self.writes():
            for length in ("abc", "-1"):
                with self.subTest(target=target, length=length):
                    _, reply = call(self.hub, "POST", target, b"{}", {**self.headers(who), "Content-Length": length})
                    self.assertEqual(reply.status, 400)
                    self.assertEqual(self.snapshot(), self.before)

    def test_a_body_of_exactly_one_mebibyte_is_read(self):
        raw = b'{"kind": "movie", "tmdb": 9}'
        _, reply = call(self.hub, "POST", "/api/requests", raw + b" " * ((1 << 20) - len(raw)), self.headers(self.alice))
        self.assertEqual(reply.status, 201)

    def test_fields_of_the_wrong_type_are_a_400_and_change_nothing(self):
        for body in ({"kind": ["movie"], "tmdb": 9}, {"kind": {}, "tmdb": 9}, {"kind": "movie", "tmdb": {"a": 1}}, {"kind": "movie", "tmdb": 9, "title": 5},
                     {"kind": "movie", "tmdb": 9, "title": ["x"]}, {"kind": "movie", "tmdb": 9, "year": ["2001"]}, {"kind": "movie", "tmdb": 9, "year": 20.01}, {"kind": "movie", "tmdb": 9, "note": {"a": 1}}):
            with self.subTest(body=body):
                self.assertEqual(self.post(self.alice, "/api/requests", body).status, 400)
                self.assertEqual(self.snapshot(), self.before)
        for body in ({"status": ["added"]}, {"status": {"a": 1}}, {"status": True}, {"status": "declined", "reason": 5}, {"status": "declined", "reason": ["x"]}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.post(self.boss, f"/api/requests/{self.rid}/status", body).status, 400)
                self.assertEqual(self.snapshot(), self.before)

    def test_a_title_that_is_not_text_is_a_400_in_french(self):
        reply = self.post(self.alice, "/api/requests", {"kind": "movie", "tmdb": 9, "title": 5})
        self.assertEqual((reply.status, reply.json()), (400, {"error": "titre : un texte est attendu"}))

    def test_a_refused_caller_changes_nothing_whatever_the_body(self):
        self.post(self.boss, f"/api/user/{self.alice['id']}/request", {"request": False})
        before = self.snapshot()
        for target, who in ((f"/api/requests/{self.rid}/status", self.alice), ("/api/requests", self.alice), (f"/api/user/{self.bob['id']}/request", self.alice)):
            for name, raw in self.BODIES.items():
                with self.subTest(target=target, body=name):
                    _, reply = call(self.hub, "POST", target, raw, self.headers(who))
                    self.assertEqual(reply.status, 403)
                    self.assertEqual(self.snapshot(), before)

    def test_without_a_session_every_write_route_is_a_401_and_changes_nothing(self):
        for _, target in self.writes():
            with self.subTest(target=target):
                _, reply = call(self.hub, "POST", target, {"status": "added", "request": False, "kind": "movie", "tmdb": 9})
                self.assertEqual(reply.status, 401)
                self.assertEqual(self.snapshot(), self.before)


class TestVolume(RequestCase):
    """Des centaines de demandes : ce que coûte une lecture ne dépend pas de leur nombre."""

    def targets(self):
        return [(self.boss, "/api/requests"), (self.alice, "/api/requests?mine=1"), (self.alice, "/api/home"), (self.alice, "/api/item/m1"), (self.alice, "/api/library/films?group=Saga"),
                (self.alice, "/api/person?name=" + quote("Jeanne Dupont")), (self.boss, "/api/state")]

    def counts(self):
        """{adresse: nombre de requêtes SQL} ; chaque adresse est appelée une fois avant, pour que ce que le serveur garde en mémoire (voir Database.derived) soit prêt."""
        out = {}
        for who, target in self.targets():
            self.statements_of(who, target)
            out[target] = len(self.statements_of(who, target))
        return out

    def test_the_number_of_sql_statements_does_not_grow_with_the_requests(self):
        self.crowd(30)
        few = self.counts()
        self.crowd(570)
        many = self.counts()
        self.assertEqual(many, few)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 600)

    def test_the_lists_hold_every_request_of_a_crowd(self):
        self.crowd(600)
        everything = self.everything()
        self.assertEqual(len(everything), 600)
        self.assertEqual(sorted(r["id"] for r in everything), list(range(1, 601)))
        self.assertTrue(all(r["users"] for r in everything))
        asked = [self.db.one("SELECT COUNT(*) FROM request_users WHERE user_id=?", (who["id"],))[0] for who in (self.alice, self.bob)]
        self.assertEqual([len(self.mine(self.alice)), len(self.mine(self.bob))], asked)
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], self.db.one("SELECT COUNT(*) FROM requests WHERE status='pending'")[0])
        self.assertEqual({tmdb: (mark or {}).get("mine") for tmdb, mark in self.saga_missing(self.alice).items()}, {2: True, 3: False})          # la saga garde ses films absents, et eux seuls

    def test_a_crowd_of_arrivals_is_written_in_chunks_and_read_once(self):
        self.crowd(700)
        arriving = [r["tmdb"] for r in self.db.query("SELECT tmdb FROM requests WHERE kind='movie' AND status='pending'")]
        self.assertGreater(len(arriving), 500)                                                  # de quoi passer la limite d'un paquet
        self.arrive("films", [(f"n{tmdb}", tmdb) for tmdb in arriving])
        statements = self.statements_of(self.boss, "/api/requests")
        self.assertEqual(sum(s.lstrip().upper().startswith("UPDATE REQUESTS") for s in statements), math.ceil(len(arriving) / 500))
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests WHERE kind='movie' AND status='pending'")[0], 0)
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], self.db.one("SELECT COUNT(*) FROM requests WHERE status='pending'")[0])

    def test_the_home_lists_a_crowd_of_arrivals_with_a_few_statements(self):
        self.crowd(700)
        self.arrive("films", [(f"n{r['tmdb']}", r["tmdb"]) for r in self.db.query("SELECT tmdb FROM requests WHERE kind='movie'")])
        expected = self.db.one("SELECT COUNT(*) FROM requests r JOIN request_users u ON u.request_id = r.id WHERE u.user_id=? AND r.kind='movie' AND r.status='pending'", (self.alice["id"],))[0]
        statements = self.statements_of(self.alice, "/api/home")
        ready = self.get(self.alice, "/api/home").json()["requests_ready"]
        self.assertEqual(len(ready), expected)
        self.assertEqual(len({r["id"] for r in ready}), expected)
        self.assertLess(sum("request" in s and s.lstrip().upper().startswith("SELECT") for s in statements), 2)
        self.assertLessEqual(sum(s.lstrip().upper().startswith("UPDATE REQUESTS") for s in statements), 2)
        self.assertLess(len(statements), 40)


class TestReads(RequestCase):
    def test_each_route_reads_the_requests_once(self):
        for n in (2, 3):
            self.ask(self.alice, n)
        self.ask(self.bob, 3)
        self.own("films", "m4", 4)
        for target in ("/api/item/m1", "/api/library/films?group=Saga", "/api/person?name=" + quote("Jeanne Dupont"), "/api/home", "/api/requests?mine=1"):
            self.assertEqual(self.reads_of(self.alice, target), 1, target)
        for target in ("/api/requests", "/api/state"):
            self.assertEqual(self.reads_of(self.boss, target), 1, target)
        self.assertEqual(self.reads_of(self.alice, "/api/state"), 0)
        self.assertEqual(self.reads_of(self.alice, "/api/library/films"), 0)          # sans case grisée, rien à lire

    def test_many_arrivals_are_written_in_one_statement(self):
        for n in range(20, 30):
            self.ask(self.alice, n)
        self.fill("films", [item(f"n{n}", lib="films", meta={"tmdb": f"movie/{n}"}) for n in range(20, 30)])
        statements, conn = [], self.db.connection()
        conn.set_trace_callback(statements.append)
        try:
            self.mine(self.alice)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(sum(s.lstrip().upper().startswith("UPDATE REQUESTS") for s in statements), 1)
        self.assertEqual({r["status"] for r in self.mine(self.alice)}, {"added"})


class TestWithTheFakeLibrary(unittest.TestCase):
    """La médiathèque factice (tests/fixtures.build_library), scannée pour de vrai : Inception (movie/27205) arrive après la demande."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.folders = fixtures.build_library(self.root / "srv")
        self.films = config.Library("films", "Films", "movies", self.folders["films"])
        self.hub = MiniHub(self.root, [self.films])
        self.boss, self.alice = accounts.create(self.hub.db, "boss", PASSWORD), accounts.create(self.hub.db, "alice", PASSWORD)

    def scan(self):
        scanning.scan_library(self.hub.db, self.films, log=lambda _m: None)

    def files(self):
        """Tout ce que la médiathèque contient : chemin, taille et date de chaque fichier."""
        return sorted((str(p.relative_to(self.root)), p.stat().st_size, p.stat().st_mtime_ns) for p in (self.root / "srv").rglob("*") if p.is_file())

    def test_a_request_turns_added_after_the_film_is_scanned_in(self):
        cookie = self.hub.cookie(self.alice)
        away = self.root / "ailleurs"
        (self.folders["films"] / "Inception (2010)").rename(away)
        self.scan()
        _, reply = call(self.hub, "POST", "/api/requests", {"kind": "movie", "tmdb": 27205, "title": "Inception", "year": "2010"}, cookie)
        self.assertEqual((reply.status, reply.json()["status"]), (201, "pending"))
        _, taken = call(self.hub, "POST", "/api/requests", {"kind": "movie", "tmdb": 49521}, cookie)
        self.assertEqual((taken.status, taken.json()["error"]), (409, "déjà dans la médiathèque"))            # Man of Steel est déjà là
        away.rename(self.folders["films"] / "Inception (2010)")
        self.scan()
        inception = self.hub.db.one("SELECT id FROM items WHERE title='Inception'")["id"]
        _, reply = call(self.hub, "GET", "/api/requests?mine=1", headers=cookie)
        self.assertEqual([(r["tmdb"], r["status"], r.get("item")) for r in reply.json()["requests"]], [(27205, "added", inception)])
        _, home = call(self.hub, "GET", "/api/home", headers=cookie)
        self.assertEqual([r["item"] for r in home.json()["requests_ready"]], [inception])

    def test_requests_never_write_in_the_library(self):
        self.scan()
        before = self.files()
        mine, boss = self.hub.cookie(self.alice), self.hub.cookie(self.boss)
        _, asked = call(self.hub, "POST", "/api/requests", {"kind": "movie", "tmdb": 603, "title": "Matrix", "year": "1999"}, mine)
        self.assertEqual(asked.status, 201)
        rid = asked.json()["id"]
        call(self.hub, "POST", "/api/requests", {"kind": "movie", "tmdb": 27205}, mine)                      # déjà là : 409
        for who, method, target, body in ((boss, "POST", f"/api/requests/{rid}/status", {"status": "declined", "reason": "non"}), (boss, "POST", f"/api/requests/{rid}/status", {"status": "added"}),
                                          (mine, "POST", f"/api/requests/{rid}/ack", {}), (mine, "GET", "/api/requests?mine=1", None), (boss, "GET", "/api/requests", None), (mine, "GET", "/api/home", None),
                                          (boss, "GET", "/api/state", None), (mine, "POST", f"/api/requests/{rid}/delete", {})):
            _, reply = call(self.hub, method, target, body, who)
            self.assertEqual(reply.status, 200, target)
        self.assertEqual(self.files(), before)


class TestMigration(unittest.TestCase):
    def test_a_version_7_base_gains_the_requests_and_keeps_everything_else(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "hub.db"
        db = Database(path)
        alice = accounts.create(db, "boss", PASSWORD) and accounts.create(db, "alice", PASSWORD)
        db.save_progress(alice["id"], "e1", 12.5, 100, False)
        db.execute("INSERT INTO playlists(id, profile, name, created, updated) VALUES('p1', ?, 'Été', 1, 1)", (alice["id"],))
        db.execute("INSERT INTO playlist_tracks(playlist_id, pos, entry_id) VALUES('p1', 0, 't1')")
        db.set_rating(alice["id"], "m1", "up")
        db.connection().close()
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE requests; DROP TABLE request_users; ALTER TABLE users DROP COLUMN can_request; UPDATE kv SET value='7' WHERE key='schema';")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.kv_get("schema"), database.SCHEMA_VERSION)
        self.assertEqual(database.SCHEMA_VERSION, 8)
        self.assertEqual([tuple(r) for r in db.query("SELECT name, can_request FROM users ORDER BY name")], [("alice", 1), ("boss", 1)])
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [(alice["id"], "e1", 12.5)])
        self.assertEqual([tuple(r) for r in db.query("SELECT p.name, t.entry_id FROM playlists p JOIN playlist_tracks t ON t.playlist_id = p.id")], [("Été", "t1")])
        self.assertEqual(db.ratings_for(alice["id"]), {"m1": "up"})
        self.assertEqual(db.query("SELECT * FROM requests"), [])
        self.assertTrue(accounts.public(accounts.by_id(db, alice["id"]))["can_request"])
        self.assertEqual(Database(path).kv_get("schema"), database.SCHEMA_VERSION)          # rouverte : rien n'est rejoué


if __name__ == "__main__":
    unittest.main()
