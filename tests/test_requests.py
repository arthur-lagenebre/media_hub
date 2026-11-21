"""Les demandes de films et de séries : la liste d'attente que l'administrateur lit (le hub ne télécharge rien). Demander, rejoindre, retirer, le plafond, ce qui est déjà là, le refus, le droit « Peut demander », l'état « ajouté » calculé à la lecture, l'accueil, les cases grisées des sagas et des personnes, la confidentialité d'un compte à l'autre et la migration de la base."""

from __future__ import annotations

import json
import os
import shutil
import math
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import quote

from tests import fixtures
from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, missing
from hub import db as database
from hub import requests as hub_requests
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

    def test_every_request_gives_the_tmdb_page_of_its_film_or_its_show(self):
        pages = {"movie": "https://www.themoviedb.org/movie/2", "show": "https://www.themoviedb.org/tv/2"}
        self.ask(self.alice, 2)
        shown = self.ask(self.alice, 2, "show", title="Une série").json()
        self.assertEqual({r["kind"]: r["url"] for r in self.mine(self.alice)}, pages)
        self.assertEqual({r["kind"]: r["url"] for r in self.everything()}, pages)
        self.assertEqual(self.set_status(shown["id"], "declined").json()["url"], pages["show"])

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

    def test_a_tmdb_number_goes_from_one_to_two_to_the_fifty_third_excluded(self):
        for bad in (0, 1 << 53, (1 << 53) + 1):
            reply = self.ask(self.alice, bad)
            self.assertEqual((reply.status, reply.json()), (400, {"error": "identifiant TMDB attendu : un entier positif"}), bad)
        self.assertEqual(self.stored(), [])
        for good in ((1 << 53) - 1, 5):
            self.assertEqual(self.ask(self.alice, good, title="Un titre").status, 201, good)

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


class TestWhatTheServerKnows(RequestCase):
    """Ce que la médiathèque a (`owned`), ce qu'on sait d'un film (`known_film`), la description demandée à TMDB (`create`) et les marques de la recherche : appelés comme le font les routes."""

    def trace(self, call):
        """(ce que `call()` rend, les requêtes SQL qu'il a passées)."""
        statements, conn = [], self.db.connection()
        conn.set_trace_callback(statements.append)
        try:
            return call(), statements
        finally:
            conn.set_trace_callback(None)

    def test_each_element_is_listed_once_and_one_without_a_tmdb_number_is_left_out(self):
        self.fill("films", [item("m1", lib="films", title="Un", meta={"tmdb": "movie/1"}), item("m9", lib="films", title="Sans numéro")], [entry("v1", "m1", lib="films")])
        self.fill("series", [item("s1", "show", lib="series", title="Série", meta={"tmdb": 5})],
                  [entry(name, "s1", lib="series", meta={"info": {"tmdb": "movie/7"}}) for name in ("e1", "e2")])           # deux fichiers du même long-métrage
        found = hub_requests.owned(self.db, self.libs)
        self.assertEqual(found, {("movie", 1): [("m1", "films")], ("show", 5): [("s1", "series")], ("movie", 7): [("s1", "series")]})

    def test_without_a_video_library_nothing_is_read_and_with_only_one_kind_that_kind_is(self):
        music = config.Library("musique", "Musique", "music", self.root / "musique")
        self.fill("series", [item("s1", "show", lib="series", title="Série", meta={"tmdb": 5})])
        found, statements = self.trace(lambda: hub_requests.owned(self.db, [music]))
        self.assertEqual((found, statements), ({}, []))
        self.assertEqual(hub_requests.owned(self.db, [lib for lib in self.libs if lib.kind == "movies"]), {("movie", 1): [("m1", "films")]})
        self.assertEqual(hub_requests.owned(self.db, [lib for lib in self.libs if lib.kind == "shows"]), {("show", 5): [("s1", "series")]})

    def test_the_marks_of_a_search_without_a_result_read_nothing_and_with_results_say_what_is_asked_and_what_is_there(self):
        asked = self.ask(self.alice, 2).json()
        (marks, found), statements = self.trace(lambda: hub_requests.search_marks(self.db, self.hub.cfg, self.alice["id"], set()))
        self.assertEqual(((marks, found), statements), (({}, {}), []))
        marks, found = hub_requests.search_marks(self.db, self.hub.cfg, self.alice["id"], {("movie", 2), ("movie", 1), ("show", 2)})
        self.assertEqual(marks, {("movie", 2): {"status": "pending", "mine": True, "id": asked["id"]}})
        self.assertIn(("movie", 1), found)

    def test_the_film_of_a_person_is_found_by_its_own_number_among_the_others_and_the_entries_that_are_not_films(self):
        self.db.kv_set("personinfo:90", {"at": 0, "films": ["texte", {"tmdb": 7, "title": "Sept", "year": "1990"}, {"tmdb": 5, "title": "Cinq", "year": "1999"}]})
        self.assertEqual(hub_requests.known_film(self.db, 5), ("Cinq", "1999"))

    def test_the_description_is_asked_for_a_new_element_the_server_does_not_know_and_only_then(self):
        asked = []

        def describe(kind, tmdb):
            asked.append((kind, tmdb))
            return "Décrit", "1999"
        for tmdb in (3, 50):
            hub_requests.create(self.db, self.hub.cfg, self.alice["id"], {"kind": "movie", "tmdb": tmdb, "title": "Du corps", "year": "1950"}, None, None, describe)
        self.assertEqual(asked, [("movie", 50)])
        self.assertEqual({r["tmdb"]: (r["title"], r["year"]) for r in self.mine(self.alice)}, {3: ("Film 3", "2001"), 50: ("Décrit", "1999")})

    def test_the_description_is_not_asked_again_for_a_request_that_already_exists(self):
        self.ask(self.alice, 50, title="Du corps")
        asked = []
        hub_requests.create(self.db, self.hub.cfg, self.bob["id"], {"kind": "movie", "tmdb": 50}, None, None, lambda kind, tmdb: asked.append(tmdb))
        self.assertEqual(asked, [])


class TestStatusRace(RequestCase):
    def test_a_request_deleted_between_the_change_and_the_reply_is_a_404_not_a_500(self):
        rid = self.ask(self.alice, 2).json()["id"]
        with mock.patch.object(hub_requests, "everything", return_value=[]):          # une autre requête l'a supprimée entre-temps
            reply = self.set_status(rid, "added")
        self.assertEqual((reply.status, reply.json()), (404, UNKNOWN))


class TestHomeShelf(RequestCase):
    def test_the_account_hides_the_shelf_of_arrivals_and_nothing_is_computed(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.own("films", "m2", 2)
        self.assertEqual(self.post(self.alice, "/api/me/settings", {"home_requests": "hide"}).json()["settings"]["home_requests"], "hide")
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])               # le champ reste là, vide
        statements, conn = [], self.db.connection()
        conn.set_trace_callback(statements.append)
        try:
            self.get(self.alice, "/api/home")
        finally:
            conn.set_trace_callback(None)
        self.assertEqual([s for s in statements if "request" in s], [])
        self.assertEqual(self.post(self.alice, "/api/me/settings", {"home_requests": "show"}).status, 200)
        self.assertEqual([r["id"] for r in self.get(self.alice, "/api/home").json()["requests_ready"]], [rid])          # rien n'a été acquitté pendant qu'il était caché

    def test_the_shelf_is_shown_by_default_and_only_show_or_hide_is_accepted(self):
        self.assertEqual(self.get(self.alice, "/api/me/settings").json()["settings"]["home_requests"], "show")
        reply = self.post(self.alice, "/api/me/settings", {"home_requests": "maybe"})
        self.assertEqual((reply.status, reply.json()), (400, {"error": "valeur inconnue pour home_requests : maybe"}))


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
        shared = self.ask(self.alice, 2, note="message d'Alice").json()["id"]
        self.ask(self.bob, 2, note="message de Bob")
        self.ask(self.alice, 3, note="seulement Alice")
        mine = self.mine(self.bob)
        self.assertEqual([(r["tmdb"], r["note"], r["mine"]) for r in mine], [(2, "message de Bob", True)])
        ours, theirs = {"status": "pending", "mine": True, "id": shared}, {"status": "pending", "mine": False}          # l'identifiant seulement quand c'est la sienne : rien d'une demande d'autrui
        self.assertEqual(self.saga_missing(self.bob), {2: ours, 3: theirs})
        self.assertEqual(self.person_missing(self.bob), {4: None, 3: theirs, 2: ours})
        library = self.get(self.bob, "/api/library/films?group=Saga").json()["missing"]
        self.assertEqual({f["tmdb"]: f.get("request") for f in library}, {2: ours, 3: theirs})
        for found in (self.saga_missing(self.bob), self.person_missing(self.bob)):
            self.assertNotIn("id", found[3])
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

    def test_a_request_whose_film_is_there_cannot_be_put_back_on_hold(self):
        """La lecture suivante la repasserait à « ajoutée » : le serveur refuse plutôt que de promettre ce qu'il ne tiendra pas. Rien ne change, même pas l'accusé de réception."""
        rid = self.ask(self.alice, 2).json()["id"]
        self.own("films", "m2", 2)
        self.mine(self.alice)
        self.post(self.alice, f"/api/requests/{rid}/ack")
        reply = self.set_status(rid, "pending")
        self.assertEqual((reply.status, reply.json()), (409, {"error": "L'élément est déjà dans la médiathèque : la demande ne peut pas rester en attente."}))
        self.assertEqual(self.mine(self.alice)[0]["status"], "added")
        self.assertEqual(self.get(self.alice, "/api/home").json()["requests_ready"], [])

    def test_a_declined_request_whose_film_is_absent_can_be_put_back_on_hold(self):
        rid = self.ask(self.alice, 2).json()["id"]
        self.set_status(rid, "declined", "non")
        reply = self.set_status(rid, "pending")
        self.assertEqual((reply.status, reply.json()["status"]), (200, "pending"))

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

    def test_a_body_nested_without_end_is_never_a_500(self):
        """Un corps `[[[[…` fait lever RecursionError à `json.loads` : `safe_json` (donc `body_json` : droit « Peut demander », retrait, acquittement) l'ignore comme un corps illisible, `strict_body_dict` (création, statut) le refuse en 400 ; jamais une 500."""
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


class ArtistCase(RequestCase):
    """RequestCase avec deux bibliothèques de musique de plus : « musique » (que tous voient) et « musprive » (que Bob ne voit pas)."""

    def setUp(self):
        super().setUp()
        for ident in ("musique", "musprive"):
            lib = config.Library(ident, ident.title(), "music", self.root / ident)
            lib.path.mkdir()
            self.hub.cfg.libraries.append(lib)
        accounts.set_rights(self.db, self.bob["id"], {lib.id for lib in self.hub.cfg.libraries}, {"prive": {"view": False}, "musprive": {"view": False}})

    def ask_artist(self, who, name, **more):
        return self.post(who, "/api/requests", {"kind": "artist", "name": name, **more})

    def album(self, lib, ident, artist, artist_id="ar1"):
        return item(ident, "album", lib=lib, title=f"Album {ident}", grp=artist, meta={"artist_id": artist_id} if artist_id else {})

    def scanned(self, lib, *albums):
        """Ces albums `albums` arrivent dans la bibliothèque de musique `lib` (ceux d'avant y restent)."""
        rows = [dict(r) for r in self.db.query("SELECT * FROM items WHERE lib=?", (lib,))]
        self.fill(lib, rows + list(albums))

    def kinds(self, who):
        return [(r["kind"], r["title"], r["status"]) for r in self.mine(who)]

    def home(self, who):
        return self.get(who, "/api/home").json()["requests_ready"]

    def stored_artists(self):
        return [tuple(r) for r in self.db.query("SELECT r.kind, r.status FROM requests r JOIN request_users u ON u.request_id = r.id WHERE r.kind='artist'")]


class TestAskingForAnArtist(ArtistCase):
    def test_an_artist_is_asked_by_a_name_and_the_answer_says_nothing_about_tmdb(self):
        first = self.ask_artist(self.alice, "Daft Punk", note="Pour la voiture")
        rid = first.json()["id"]
        self.assertEqual((first.status, first.json()), (201, {"id": rid, "kind": "artist", "status": "pending", "mine": True}))
        shown = self.mine(self.alice)
        self.assertEqual(len(shown), 1)
        got = shown[0]
        self.assertEqual((got["id"], got["kind"], got["title"], got["status"], got["note"], got["ack"], got["mine"]), (rid, "artist", "Daft Punk", "pending", "Pour la voiture", False, True))
        self.assertFalse(got.get("cover"))
        self.assertFalse(got.get("poster"))
        self.assertNotIn("tmdb", got)
        self.assertNotIn("url", got)
        self.assertNotIn("item", got)

    def test_the_name_is_kept_as_the_first_requester_typed_it_without_the_edge_spaces(self):
        self.ask_artist(self.alice, "  daft   Punk  ")
        self.ask_artist(self.bob, "DAFT PUNK")
        self.assertEqual([r["title"] for r in self.mine(self.alice)], ["daft   Punk"])
        self.assertEqual([r["title"] for r in self.mine(self.bob)], ["daft   Punk"])

    def test_the_same_name_written_differently_joins_the_same_request(self):
        for first, second in (("Daft Punk", "daft  punk"), ("Beyoncé", "Beyonce"), ("Björk", "BJORK"), ("Daft Punk", "Ｄａｆｔ　Ｐｕｎｋ"), ("Sigur Rós", "Sigur Rós")):
            with self.subTest(first=first, second=second):
                self.db.execute("DELETE FROM request_users")
                self.db.execute(hub_requests.ORPHANS)
                rid = self.ask_artist(self.alice, first).json()["id"]
                joined = self.ask_artist(self.bob, second)
                self.assertEqual((joined.status, joined.json()), (200, {"id": rid, "kind": "artist", "status": "pending", "mine": True}))
                self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 1)
                self.assertEqual(self.db.one("SELECT COUNT(*) FROM request_users WHERE request_id=?", (rid,))[0], 2)

    def test_the_same_account_asking_again_changes_nothing_and_another_name_is_another_request(self):
        rid = self.ask_artist(self.alice, "Air", note="d'abord").json()["id"]
        again = self.ask_artist(self.alice, "air", note="ensuite")
        self.assertEqual((again.status, again.json()["id"]), (200, rid))
        self.assertEqual(self.mine(self.alice)[0]["note"], "d'abord")
        self.assertEqual(self.ask_artist(self.alice, "Airs").status, 201)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 2)

    def test_an_artist_and_a_film_never_meet_whatever_the_name(self):
        self.ask(self.alice, 2)
        self.assertEqual(self.ask_artist(self.alice, "Film 2").status, 201)
        self.assertEqual(sorted(self.kinds(self.alice)), [("artist", "Film 2", "pending"), ("movie", "Film 2", "pending")])

    def test_the_admin_sees_the_requesters_of_an_artist_and_their_messages(self):
        rid = self.ask_artist(self.alice, "Daft Punk", note="un").json()["id"]
        self.ask_artist(self.bob, "daft punk", note="deux")
        listed = self.everything()
        self.assertEqual([(r["id"], r["kind"], r["title"]) for r in listed], [(rid, "artist", "Daft Punk")])
        self.assertEqual(sorted((u["name"], u["note"]) for u in listed[0]["users"]), [("alice", "un"), ("bob", "deux")])
        self.assertNotIn("tmdb", listed[0])
        self.assertNotIn("url", listed[0])

    def test_a_name_goes_from_one_to_two_hundred_characters_without_control_characters(self):
        control = "nom d'artiste : aucun caractère de contrôle permis"
        bad = {"": "nom d'artiste vide", "   ": "nom d'artiste vide", "́": "nom d'artiste vide", "x" * 201: "nom d'artiste trop long (200 caractères au plus)",
               "Dai\x00ft": control, "Daft\nPunk": control, "Daft\tPunk": control, "Daft​Punk": control, "Daft Punk": control}
        for name, message in bad.items():
            reply = self.ask_artist(self.alice, name)
            self.assertEqual((reply.status, reply.json()), (400, {"error": message}), repr(name))
        for value in (None, 7, ["Air"], {"x": 1}, True):
            reply = self.ask_artist(self.alice, value)
            self.assertEqual((reply.status, reply.json()), (400, {"error": "nom d'artiste attendu : un texte"}), repr(value))
        reply = self.post(self.alice, "/api/requests", {"kind": "artist"})
        self.assertEqual((reply.status, reply.json()), (400, {"error": "nom d'artiste attendu : un texte"}))
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 0)
        self.assertEqual(self.ask_artist(self.alice, "x").status, 201)
        self.assertEqual(self.ask_artist(self.alice, "é" * 200).status, 201)
        self.assertEqual(self.ask_artist(self.alice, "  " + "y" * 200 + "  ").status, 201)            # les espaces de bord ne comptent pas

    def test_a_message_too_long_is_refused_for_an_artist_too_and_a_tmdb_number_is_ignored(self):
        reply = self.ask_artist(self.alice, "Air", note="x" * 201)
        self.assertEqual((reply.status, reply.json()), (400, {"error": "message trop long (200 caractères au plus)"}))
        self.assertEqual(self.ask_artist(self.alice, "Air", tmdb=603, year="1999").status, 201)
        got = self.mine(self.alice)[0]
        self.assertEqual((got["title"], got["year"]), ("Air", ""))
        self.assertNotIn("tmdb", got)

    def test_a_film_still_needs_its_tmdb_number_and_never_a_name(self):
        reply = self.post(self.alice, "/api/requests", {"kind": "movie", "name": "Matrix"})
        self.assertEqual((reply.status, reply.json()), (400, {"error": "identifiant TMDB attendu : un entier positif"}))
        self.assertEqual(self.post(self.alice, "/api/requests", {"kind": "album", "name": "Air"}).status, 400)

    def test_an_artist_request_has_no_poster_and_the_poster_route_says_404(self):
        rid = self.ask_artist(self.alice, "Air").json()["id"]
        (self.hub.cfg.data / "tmdb").mkdir()
        (self.hub.cfg.data / "tmdb" / "None.jpg").write_bytes(b"jpeg")
        self.assertEqual(self.get(self.alice, f"/img/request/{rid}").status, 404)
        self.assertFalse(self.mine(self.alice)[0].get("cover"))

    def test_asking_for_an_artist_never_starts_the_poster_task(self):
        self.ask_artist(self.alice, "Air")
        self.hub.jobs.request_posters.assert_not_called()


class TestRequestPoster(RequestCase):
    """`GET /img/request/<id>` : l'affiche d'une série demandée, pour ses demandeurs et l'administrateur seulement ; jamais pour un artiste."""

    def setUp(self):
        super().setUp()
        (self.hub.cfg.data / "tmdb").mkdir()
        (self.hub.cfg.data / "tmdb" / "5.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"poster" * 10)
        self.rid = self.ask(self.alice, 5, "show", title="Une série").json()["id"]
        self.hub.images.file.return_value = (b"image", "image/jpeg", "clé")

    def test_the_asker_and_the_admin_get_the_poster_another_account_and_an_unknown_request_do_not(self):
        for who, status in ((self.alice, 200), (self.boss, 200), (self.bob, 404)):
            self.assertEqual(self.get(who, f"/img/request/{self.rid}").status, status, who["name"])
        reply = self.get(self.alice, "/img/request/9999")
        self.assertEqual((reply.status, reply.json()), (404, {"error": "pas d'affiche"}))
        self.assertEqual({call.args[1] for call in self.hub.images.file.call_args_list}, {self.hub.cfg.data / "tmdb" / "5.jpg"})

    def test_an_artist_request_never_gives_one_even_for_the_admin_or_with_a_file_that_looks_right(self):
        (self.hub.cfg.data / "tmdb" / "None.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 40)
        artist = self.post(self.alice, "/api/requests", {"kind": "artist", "name": "Air"}).json()["id"]
        for who in (self.alice, self.boss):
            self.assertEqual(self.get(who, f"/img/request/{artist}").status, 404)
        self.hub.images.file.assert_not_called()


class TestArtistRules(ArtistCase):
    def test_the_right_to_request_is_the_same_one(self):
        self.post(self.boss, f"/api/user/{self.alice['id']}/request", {"request": False})
        reply = self.ask_artist(self.alice, "Air")
        self.assertEqual((reply.status, reply.json()), (403, {"error": FORBIDDEN}))
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 0)
        self.assertEqual(self.ask_artist(self.boss, "Air").status, 201)

    def test_the_ceiling_counts_artists_with_films_and_the_admin_has_none(self):
        self.hub.cfg.request_max = 2
        self.ask(self.alice, 2)
        self.ask_artist(self.alice, "Air")
        reply = self.ask_artist(self.alice, "Daft Punk")
        self.assertEqual((reply.status, reply.json()), (409, {"error": "trop de demandes en attente (2)"}))
        self.assertEqual(self.ask(self.alice, 3).status, 409)
        self.ask_artist(self.bob, "Phoenix")
        self.assertEqual(self.ask_artist(self.alice, "Phoenix").status, 409)                  # rejoindre compte aussi
        self.assertEqual(self.ask_artist(self.alice, "AIR").status, 200)                     # ce qu'on a déjà demandé n'ajoute rien
        for name in ("a", "b", "c", "d"):
            self.assertEqual(self.ask_artist(self.boss, name).status, 201)

    def test_an_arrived_artist_no_longer_counts_toward_the_ceiling(self):
        self.hub.cfg.request_max = 1
        rid = self.ask_artist(self.alice, "Air").json()["id"]
        self.assertEqual(self.ask_artist(self.alice, "Daft Punk").status, 409)
        self.set_status(rid, "added")
        self.assertEqual(self.ask_artist(self.alice, "Daft Punk").status, 201)

    def test_a_declined_artist_blocks_a_new_request_whatever_the_spelling(self):
        rid = self.ask_artist(self.alice, "Daft Punk").json()["id"]
        self.assertEqual(self.set_status(rid, "declined", "introuvable").json()["status"], "declined")
        for who, name in ((self.alice, "Daft Punk"), (self.bob, "daft  punk")):
            reply = self.ask_artist(who, name)
            self.assertEqual((reply.status, reply.json()), (409, {"error": "cette demande a été refusée : introuvable"}), name)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM request_users")[0], 1)
        self.assertEqual(self.set_status(rid, "declined", "").json()["reason"], "")
        self.assertEqual(self.ask_artist(self.bob, "Daft Punk").json(), {"error": "cette demande a été refusée"})

    def test_asking_is_always_known_as_already_asked_without_a_name_or_a_number(self):
        rid = self.ask_artist(self.alice, "Daft Punk", note="secret d'Alice").json()["id"]
        joined = self.ask_artist(self.bob, "daft punk")
        self.assertEqual((joined.status, joined.json()), (200, {"id": rid, "kind": "artist", "status": "pending", "mine": True}))
        self.assertNotIn(b"alice", joined.body)
        self.assertNotIn(b"secret", joined.body)
        self.assertEqual(set(self.mine(self.bob)[0]), {"id", "kind", "title", "year", "status", "reason", "created", "updated", "cover", "note", "ack", "asked", "mine"})
        self.assertNotIn(b"secret", self.get(self.bob, "/api/requests?mine=1").body)
        self.assertEqual(self.get(self.bob, "/api/requests").status, 403)
        carol = accounts.create(self.db, "carol", PASSWORD)
        self.assertEqual(self.mine(carol), [])
        self.assertEqual(self.post(carol, f"/api/requests/{rid}/delete").json(), UNKNOWN)

    def test_an_account_withdraws_its_part_and_the_last_one_takes_the_request_away(self):
        rid = self.ask_artist(self.alice, "Air").json()["id"]
        self.ask_artist(self.bob, "air")
        self.assertEqual(self.post(self.alice, f"/api/requests/{rid}/delete").json(), {"ok": True})
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 1)
        self.assertEqual(self.post(self.bob, f"/api/requests/{rid}/delete").json(), {"ok": True})
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 0)
        self.assertEqual(self.ask_artist(self.alice, "Air").status, 201)

    def test_deleting_an_account_drops_its_artist_requests_and_the_orphans(self):
        self.ask_artist(self.alice, "Air")
        self.ask_artist(self.alice, "Phoenix")
        self.ask_artist(self.bob, "phoenix")
        accounts.delete(self.db, self.alice["id"])
        self.assertEqual([r["title"] for r in self.db.query("SELECT title FROM requests")], ["Phoenix"])

    def test_the_admin_marks_an_artist_added_or_declined_by_hand_and_counts_it_pending(self):
        rid = self.ask_artist(self.alice, "Dayft Punk").json()["id"]                  # une faute de frappe ne se reconnaît pas
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], 1)
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        self.assertEqual(self.mine(self.alice)[0]["status"], "pending")
        marked = self.set_status(rid, "added").json()
        self.assertEqual((marked["status"], marked["kind"], "artist" in marked), ("added", "artist", False))        # arrivée à la main : aucune fiche à ouvrir
        self.assertEqual(self.get(self.boss, "/api/state").json()["requests_pending"], 0)
        self.assertEqual(self.home(self.alice), [])
        self.assertEqual(self.set_status(rid, "pending").json()["status"], "pending")


class TestArtistArrival(ArtistCase):
    def test_a_request_turns_added_when_an_album_artist_of_that_name_arrives_and_stays_so(self):
        rid = self.ask_artist(self.alice, "Daft Punk").json()["id"]
        self.ask_artist(self.bob, "daft punk")
        self.assertEqual(self.kinds(self.alice), [("artist", "Daft Punk", "pending")])
        self.scanned("musique", self.album("musique", "a1", "Daft  PUNK", "ar9"))
        self.assertEqual(self.stored_artists(), [("artist", "pending")] * 2)              # rien ne tourne en fond : c'est la lecture qui le voit
        got = self.mine(self.alice)[0]
        self.assertEqual((got["status"], got["artist"]), ("added", {"lib": "musique", "id": "ar9", "name": "Daft  PUNK"}))
        self.assertNotIn("item", got)
        self.assertEqual(self.stored_artists(), [("artist", "added")] * 2)
        self.fill("musique", [])
        self.assertEqual((self.mine(self.alice)[0]["status"], self.everything()[0]["id"]), ("added", rid))      # l'artiste reparti, la demande reste arrivée

    def test_the_arrival_goes_by_the_normalised_name_accents_and_case_included(self):
        self.ask_artist(self.alice, "Beyonce")
        self.scanned("musique", self.album("musique", "a1", "Beyoncé"))
        self.assertEqual(self.mine(self.alice)[0]["status"], "added")

    def test_a_missing_album_does_not_make_the_artist_arrive(self):
        self.ask_artist(self.alice, "Daft Punk")
        self.db.kv_set("wanted:musique", {"ar1": [{"year": "1997", "title": "Homework", "key": "k", "cover": ""}]})
        self.scanned("musique", self.album("musique", "a2", "Air", "ar2"))
        self.assertEqual(self.mine(self.alice)[0]["status"], "pending")
        self.assertEqual(self.home(self.alice), [])

    def test_only_the_album_artist_of_a_music_library_counts(self):
        self.ask_artist(self.alice, "Daft Punk")
        credited = self.album("musique", "a1", "Various Artists", "var")                    # il n'est crédité que sur une piste
        credited["meta"] = json.dumps({"artist_id": "var", "artist": "Daft Punk", "artists": ["Daft Punk"]})
        self.scanned("musique", credited)
        self.fill("films", [dict(r) for r in self.db.query("SELECT * FROM items WHERE lib='films'")] + [item("saga", "movie", lib="films", grp="Daft Punk", title="Daft Punk")])    # un film du même nom
        self.assertEqual(self.mine(self.alice)[0]["status"], "pending")

    def test_an_album_without_a_stable_artist_page_gives_the_name_only(self):
        self.ask_artist(self.alice, "Air")
        self.scanned("musique", self.album("musique", "a1", "Air", artist_id=""))
        self.assertEqual(self.mine(self.alice)[0]["artist"], {"lib": "musique", "name": "Air"})
        self.assertEqual([r["artist"] for r in self.home(self.alice)], [{"lib": "musique", "name": "Air"}])

    def test_the_home_lists_an_arrived_artist_until_it_is_acknowledged(self):
        rid = self.ask_artist(self.alice, "Daft Punk").json()["id"]
        self.ask_artist(self.bob, "daft punk")
        self.assertEqual(self.home(self.alice), [])
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        self.assertEqual(self.home(self.alice), [{"id": rid, "kind": "artist", "title": "Daft Punk", "year": "", "artist": {"lib": "musique", "id": "ar1", "name": "Daft Punk"}}])
        self.assertEqual(self.post(self.alice, f"/api/requests/{rid}/ack").json(), {"ok": True})
        self.assertEqual(self.home(self.alice), [])
        self.assertEqual(len(self.home(self.bob)), 1)                                                       # chacun acquitte pour soi
        self.assertEqual(self.mine(self.alice)[0]["ack"], True)

    def test_the_home_shows_films_and_artists_together_each_in_its_shape(self):
        self.ask(self.alice, 2)
        self.ask_artist(self.alice, "Air")
        self.own("films", "m2", 2)
        self.scanned("musique", self.album("musique", "a1", "Air", "ar2"))
        got = {r["kind"]: r for r in self.home(self.alice)}
        self.assertEqual(set(got), {"movie", "artist"})
        self.assertEqual((got["movie"]["item"], got["movie"]["card"]["id"], got["movie"]["tmdb"]), ("m2", "m2", 2))
        self.assertNotIn("card", got["artist"])
        self.assertNotIn("tmdb", got["artist"])

    def test_an_arrival_in_a_library_the_account_cannot_see_waits_for_one_it_sees(self):
        rid = self.ask_artist(self.bob, "Air").json()["id"]
        self.scanned("musprive", self.album("musprive", "p1", "Air", "ar7"))
        self.assertEqual(self.everything()[0]["artist"], {"lib": "musprive", "id": "ar7", "name": "Air"})                # l'administrateur voit tout
        shown = self.mine(self.bob)[0]
        self.assertEqual(shown["status"], "added")
        self.assertNotIn("artist", shown)
        self.assertEqual(self.home(self.bob), [])
        self.scanned("musique", self.album("musique", "a1", "Air", "ar2"))
        self.assertEqual(self.mine(self.bob)[0]["artist"]["lib"], "musique")
        self.assertEqual([r["id"] for r in self.home(self.bob)], [rid])

    def test_asking_for_an_artist_already_in_a_library_the_account_sees_is_a_409_with_the_artist(self):
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        reply = self.ask_artist(self.alice, "daft punk")
        self.assertEqual((reply.status, reply.json()), (409, {"error": "déjà dans la médiathèque", "artist": {"lib": "musique", "id": "ar1", "name": "Daft Punk"}}))
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 0)

    def test_an_artist_only_in_a_library_the_account_cannot_see_is_asked_and_already_added(self):
        self.scanned("musprive", self.album("musprive", "p1", "Daft Punk"))
        reply = self.ask_artist(self.bob, "Daft Punk")
        self.assertEqual((reply.status, reply.json()["status"]), (201, "added"))
        self.assertEqual(self.home(self.bob), [])

    def test_the_admin_cannot_put_back_on_hold_an_artist_that_is_there(self):
        rid = self.ask_artist(self.alice, "Daft Punk").json()["id"]
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        self.mine(self.alice)
        reply = self.set_status(rid, "pending")
        self.assertEqual((reply.status, reply.json()), (409, {"error": "L'élément est déjà dans la médiathèque : la demande ne peut pas rester en attente."}))
        self.assertEqual(self.set_status(rid, "declined", "non").status, 200)

    def test_the_library_is_read_once_for_many_reads_until_the_next_scan(self):
        self.ask_artist(self.alice, "Air")
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        self.mine(self.alice)
        for target in ("/api/requests?mine=1", "/api/home", "/api/state"):
            self.assertEqual([s for s in self.statements_of(self.alice, target) if "FROM items" in s and "album" in s], [], target)
        self.scanned("musique", self.album("musique", "a2", "Air", "ar2"))
        self.assertEqual(self.mine(self.alice)[0]["status"], "added")

    def test_requests_for_films_only_never_make_the_albums_be_read(self):
        self.ask(self.alice, 2)
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        self.own("films", "m2", 2)
        for target in ("/api/requests?mine=1", "/api/requests", "/api/home", "/api/state", "/api/item/m1"):
            self.assertEqual([s for s in self.statements_of(self.boss, target) if "type='album'" in s], [], target)

    def test_a_request_arrives_from_a_real_scan_and_a_missing_album_is_not_enough(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        folders = fixtures.build_library(base / "srv")
        lib = config.Library("reel", "Réel", "music", folders["musique"])
        self.hub.cfg.libraries.append(lib)

        def scan():
            scanning.scan_library(self.db, lib, log=lambda _m: None)
        scan()
        self.assertEqual(self.ask_artist(self.alice, "daft punk").status, 409)                  # Daft Punk est déjà là
        rid = self.ask_artist(self.alice, "Aphex Twin").json()["id"]
        homework = self.ask_artist(self.alice, "Homework").json()["id"]
        (folders["musique"] / "Daft Punk" / "1997 - Homework.png").write_bytes(fixtures.PNG)         # un album manquant : une pochette seule
        scan()
        self.assertEqual([w["title"] for albums in self.db.kv_get("wanted:reel").values() for w in albums], ["Homework"])
        self.assertEqual({r["id"]: r["status"] for r in self.mine(self.alice)}, {rid: "pending", homework: "pending"})           # il n'est pas un artiste de la médiathèque
        fixtures.write_flac(folders["musique"] / "Aphex Twin" / "1992 - Selected Ambient Works" / "01 - Xtal.flac", "Xtal", "Selected Ambient Works", "Aphex Twin", 1, 1)
        scan()
        got = next(r for r in self.mine(self.alice) if r["id"] == rid)
        album = self.db.one("SELECT meta FROM items WHERE lib='reel' AND grp='Aphex Twin'")
        self.assertEqual((got["id"], got["status"], got["artist"]["lib"], got["artist"]["name"], got["artist"].get("id")), (rid, "added", "reel", "Aphex Twin", json.loads(album["meta"]).get("artist_id")))
        self.assertEqual([r["artist"]["name"] for r in self.home(self.alice)], ["Aphex Twin"])


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
        self.assertEqual(database.SCHEMA_VERSION, 12)
        self.assertEqual([tuple(r) for r in db.query("SELECT name, can_request FROM users ORDER BY name")], [("alice", 1), ("boss", 1)])
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [(alice["id"], "e1", 12.5)])
        self.assertEqual([tuple(r) for r in db.query("SELECT p.name, t.entry_id FROM playlists p JOIN playlist_tracks t ON t.playlist_id = p.id")], [("Été", "t1")])
        self.assertEqual(db.ratings_for(alice["id"]), {"m1": "up"})
        self.assertEqual(db.query("SELECT * FROM requests"), [])
        self.assertTrue(accounts.public(accounts.by_id(db, alice["id"]))["can_request"])
        self.assertEqual(Database(path).kv_get("schema"), database.SCHEMA_VERSION)          # rouverte : rien n'est rejoué


V11_REQUESTS = """
CREATE TABLE requests(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL CHECK (kind IN ('movie', 'show')),
    tmdb    INTEGER NOT NULL,
    title   TEXT NOT NULL DEFAULT '',
    year    TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'added', 'declined')),
    reason  TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    UNIQUE(kind, tmdb)
)"""
REQUEST_COLUMNS = "id, kind, tmdb, title, year, status, reason, created, updated"
USER_COLUMNS = "request_id, user_id, note, created, ack"
INDEXES_OF_REQUESTS = "SELECT name FROM sqlite_master WHERE tbl_name='requests' AND type='index' AND sql IS NOT NULL"


class TestRequestsTableMigration(unittest.TestCase):
    """La version 12 refait la table `requests` (tmdb facultatif, nom normalisé, sorte `artist`) : SQLite ne change pas une contrainte en place, la table est recréée et rien de ce qu'elle gardait ne se perd."""

    REQUESTS = [(1, "movie", 603, "Matrix", "1999", "pending", "", 100, 100), (2, "show", 1399, "Une série", "", "added", "", 110, 150),
                (3, "movie", 27205, "Inception", "2010", "declined", "mauvaise version", 120, 160), (5, "movie", 11, "Un autre", "1977", "added", "", 130, 170)]
    USERS = [(1, "u1", "pour ce soir", 100, 0), (1, "u2", "", 105, 0), (2, "u1", "", 110, 1), (3, "u2", "bof", 120, 0), (5, "u1", "", 130, 1), (5, "u3", "vite", 131, 0)]

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "hub.db"

    def v11_base(self, extra=""):
        """Une base en version 11 : l'ancienne table `requests` (trois contraintes), des demandes dans chaque état, leurs demandeurs ; une demande est partie après la dernière (la suite des identifiants en garde la trace). `extra` : du SQL en plus."""
        Database(self.path).connection().close()
        conn = sqlite3.connect(self.path)
        conn.executescript("DROP TABLE requests;" + V11_REQUESTS)
        conn.executemany(f"INSERT INTO requests({REQUEST_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?)", self.REQUESTS)
        conn.execute("INSERT INTO requests(id, kind, tmdb, created, updated) VALUES(6, 'movie', 99, 1, 1)")
        conn.execute("DELETE FROM requests WHERE id=6")                  # l'identifiant 6 ne doit jamais être redonné
        conn.executemany(f"INSERT INTO request_users({USER_COLUMNS}) VALUES(?,?,?,?,?)", self.USERS)
        conn.executescript(extra + "UPDATE kv SET value='11' WHERE key='schema';")
        conn.commit()
        conn.close()

    def dump(self, db):
        return ([tuple(r) for r in db.query(f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY id")], [tuple(r) for r in db.query(f"SELECT {USER_COLUMNS} FROM request_users ORDER BY request_id, user_id")])

    def test_a_version_11_base_keeps_every_request_requester_message_status_and_ack(self):
        self.v11_base()
        db = Database(self.path)
        self.assertEqual(db.kv_get("schema"), 12)
        self.assertEqual(self.dump(db), (self.REQUESTS, self.USERS))
        again = Database(self.path)
        self.assertEqual((again.kv_get("schema"), self.dump(again)), (12, (self.REQUESTS, self.USERS)))          # rouverte : rien n'est rejoué

    def test_the_new_table_has_the_new_shape_and_no_leftover(self):
        self.v11_base()
        db = Database(self.path)
        columns = {r["name"]: r["notnull"] for r in db.query("SELECT name, \"notnull\" FROM pragma_table_info('requests')")}
        self.assertEqual((columns["tmdb"], "name_key" in columns), (0, True))
        self.assertEqual([r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE name LIKE 'requests%' AND type='table'")], ["requests"])
        self.assertEqual(sorted(r["name"] for r in db.query(INDEXES_OF_REQUESTS)), ["requests_artist", "requests_tmdb"])

    def test_an_identifier_given_before_is_never_given_again(self):
        self.v11_base()
        db = Database(self.path)
        db.execute("INSERT INTO requests(kind, name_key, title, created, updated) VALUES('artist', 'daft punk', 'Daft Punk', 200, 200)")
        db.execute("INSERT INTO requests(kind, tmdb, created, updated) VALUES('movie', 7, 200, 200)")
        self.assertEqual([r["id"] for r in db.query("SELECT id FROM requests WHERE id > 5 ORDER BY id")], [7, 8])
        self.assertEqual([tuple(r) for r in db.query("SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'requests%'")], [("requests", 8)])          # une seule ligne de suite, celle de la nouvelle table

    def test_the_sequence_is_kept_even_when_no_request_is_left(self):
        self.v11_base("DELETE FROM request_users; DELETE FROM requests;")
        db = Database(self.path)
        self.assertEqual(db.query("SELECT id FROM requests"), [])
        self.assertEqual(db.execute("INSERT INTO requests(kind, tmdb, created, updated) VALUES('movie', 7, 1, 1)").lastrowid, 7)
        self.assertEqual([tuple(r) for r in db.query("SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'requests%'")], [("requests", 7)])

    def test_the_table_accepts_what_it_must_and_refuses_what_it_must_not(self):
        self.v11_base()
        db = Database(self.path)
        insert = "INSERT INTO requests(kind, tmdb, name_key, created, updated) VALUES(?,?,?,1,1)"
        for ok in (("artist", None, "daft punk"), ("artist", None, "beyonce"), ("show", 603, None)):          # des artistes sans tmdb ; la valeur TMDB d'un film, pour une série
            db.execute(insert, ok)
        for bad, why in ((("movie", 603, None), "deux demandes du même film"), (("artist", None, "daft punk"), "deux demandes du même artiste"), (("movie", None, None), "un film sans tmdb"),
                         (("show", None, "x"), "une série sans tmdb"), (("artist", None, None), "un artiste sans nom"), (("artist", None, ""), "un artiste au nom vide"), (("album", 1, None), "une sorte inconnue")):
            with self.assertRaises(sqlite3.IntegrityError, msg=why):
                db.execute(insert, bad)

    def test_a_migration_that_fails_halfway_leaves_the_old_table_and_says_why(self):
        self.v11_base()
        with mock.patch.object(database, "REQUEST_INDEXES", ("CREATE UNIQUE INDEX requests_oups ON requests(colonne_inconnue)",)), self.assertRaises(RuntimeError) as caught:
            Database(self.path)
        message = str(caught.exception)
        self.assertIn("vers la version 12 a échoué", message)
        self.assertIn("colonne_inconnue", message)
        self.assertIn("la base est restée en version 11", message)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM kv WHERE key='schema'").fetchone()[0], "11")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pragma_table_info('requests') WHERE name='name_key'").fetchone()[0], 0)       # l'ancienne table, entière
        self.assertEqual(conn.execute(f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY id").fetchall(), self.REQUESTS)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='requests_new'").fetchone()[0], 0)
        self.assertEqual(self.dump(Database(self.path)), (self.REQUESTS, self.USERS))           # sans l'accident, la fois suivante elle passe

    def test_a_trigger_or_a_view_on_the_table_stops_the_migration_with_nothing_touched(self):
        for n, what in enumerate(("CREATE TRIGGER garde AFTER DELETE ON requests BEGIN SELECT 1; END;", "CREATE VIEW attentes AS SELECT id FROM requests WHERE status='pending';")):
            with self.subTest(what=what):
                self.path = self.root / f"trigger{n}.db"
                self.v11_base(what)
                with self.assertRaises(RuntimeError) as caught:
                    Database(self.path)
                self.assertIn("la base est restée en version 11", str(caught.exception))
                self.assertIn("déclencheur ou une vue", str(caught.exception))
                conn = sqlite3.connect(self.path)
                self.addCleanup(conn.close)
                self.assertEqual(conn.execute(f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY id").fetchall(), self.REQUESTS)

    def test_a_new_base_has_the_indexes_from_the_start(self):
        db = Database(self.path)
        self.assertEqual(sorted(r["name"] for r in db.query(INDEXES_OF_REQUESTS)), ["requests_artist", "requests_tmdb"])
        with self.assertRaises(sqlite3.IntegrityError):
            for _ in range(2):
                db.execute("INSERT INTO requests(kind, name_key, created, updated) VALUES('artist', 'x', 1, 1)")


class TestStartup(unittest.TestCase):
    def test_the_hub_starts_before_media_library_is_on_the_path(self):
        """`python -m hub` charge les comptes, donc les demandes, avant de mettre media_library sur le chemin : dans un processus neuf, ces imports ne doivent pas en dépendre."""
        env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
        done = subprocess.run([sys.executable, "-c", "import hub.__main__"], cwd=Path(__file__).resolve().parent.parent, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
