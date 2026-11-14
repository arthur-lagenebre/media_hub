"""Demander depuis une recherche dans TMDB : la route `GET /api/requests/search` (films, séries, les deux), ses garde-fous (droit « Peut demander », longueur du texte, clé TMDB, un seul essai,
deux recherches à la fois, trente par minute et par compte, aucun cache disque), ce qu'elle dit de chaque résultat (déjà là, déjà demandé), la création d'une demande depuis un résultat
(titre et année redemandés à TMDB), la tâche « Affiches des demandes » et l'image d'une demande. TMDB est simulé : aucun appel réseau."""

from __future__ import annotations

import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote

from tests.rig import call
from tests.test_requests import RequestCase, item
from tests.test_jobs import JobsCase, Waiting
from hub import accounts, request_search
from hub import requests as hub_requests
from hub.server import jobs
from libraries.video import tmdb as tmdb_module
from libraries.video.tmdb import TmdbAuthError, TmdbError

KEY = "cle-tmdb-tres-secrete-0123456789"
SEARCH_FORBIDDEN = "tu n'as pas le droit de demander des films ni des séries"
UNREACHABLE = {"error": "TMDB ne répond pas, réessaie plus tard"}


def not_found():
    """L'erreur que le client de media_library lève pour un 404 : un TmdbError dont la cause est la réponse HTTP."""
    error = TmdbError("introuvable sur TMDB (HTTP 404)")
    error.__cause__ = HTTPError("https://api.themoviedb.org/3/x", 404, "Not Found", {}, None)
    return error


def movie(n, title=None, date="2001-01-01", **more):
    return {"id": n, "title": title or f"Film {n}", "release_date": date, "overview": f"Résumé {n}", "adult": False, **more}


def show(n, name=None, date="2019-03-04", **more):
    return {"id": n, "name": name or f"Série {n}", "first_air_date": date, "overview": f"Résumé {n}", **more}


class FakeTmdb:
    """TMDB simulé : `answers` donne la réponse de chaque point d'entrée sans sa requête (`search/movie`, `tv/77`) ; un point inconnu répond 404. `error` est levée à chaque appel ;
    `hold` (un Event) est attendu dans chaque appel, `entered` compte les appels commencés. Les images ont leurs chemins dans `failing` pour échouer."""

    def __init__(self, answers=None, error=None, hold=None, failing=()):
        self.answers, self.error, self.hold, self.failing = answers or {}, error, hold, set(failing)
        self.calls, self.images = [], []
        self.entered = threading.Semaphore(0)

    def get(self, endpoint, language=None):
        self.calls.append(endpoint)
        self.entered.release()
        if self.hold is not None:
            self.hold.wait(10)
        if self.error is not None:
            raise self.error
        path = endpoint.split("?")[0]
        if path not in self.answers:
            raise not_found()
        return self.answers[path]

    def save_image(self, image_path, size, dest):
        self.images.append((image_path, size))
        if image_path in self.failing:
            raise TmdbError("téléchargement incomplet")
        Path(dest).write_bytes(b"\xff\xd8\xff" + image_path.encode())


SEARCHES = {"search/movie": {"results": [movie(n) for n in range(2, 16)] + [movie(1, "Un"), movie(7, "Privé"), movie(600, "À venir", "2031-06-01"), movie(666, "Adulte", adult=True)]},
            "search/tv": {"results": [show(50, "Une série possédée"), show(77, "Une série à venir", "2030-01-01")] + [show(n) for n in range(100, 115)]}}


class SearchCase(RequestCase):
    """Les comptes de RequestCase (l'administrateur, Alice, Bob qui ne voit pas « prive »), une clé TMDB, et un TMDB simulé ; le film 7 est dans « prive », la série 50 dans « series »."""

    def setUp(self):
        super().setUp()
        self.hub.cfg.tmdb_key = KEY
        self.hub.finder = request_search.Finder(self.hub.cfg)
        self.tmdb = FakeTmdb(dict(SEARCHES))
        self.hub.finder.make = lambda cfg: self.tmdb
        self.fill("prive", [item("p7", lib="prive", title="Privé", meta={"tmdb": "movie/7"})])
        self.fill("series", [item("s50", "show", lib="series", title="Une série possédée", meta={"tmdb": 50})])

    def search(self, who, text="film", kind=None):
        target = "/api/requests/search?q=" + quote(text) + (f"&kind={kind}" if kind else "")
        return self.get(who, target)

    def found(self, who, text="film", kind=None):
        reply = self.search(who, text, kind)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json()["results"]


class TestSearching(SearchCase):
    def test_films_are_searched_ten_at_most_with_their_text_only(self):
        results = self.found(self.alice, "Le parrain", "movie")
        self.assertEqual(len(results), 10)
        self.assertEqual(results[0], {"kind": "movie", "tmdb": 2, "title": "Film 2", "year": "2001", "date": "2001-01-01", "overview": "Résumé 2"})
        self.assertEqual({r["kind"] for r in results}, {"movie"})
        self.assertEqual(self.tmdb.calls, ["search/movie?query=Le%20parrain"])

    def test_shows_are_searched_with_their_first_air_date(self):
        results = self.found(self.alice, "série", "show")
        self.assertEqual(len(results), 10)
        self.assertEqual(results[1], {"kind": "show", "tmdb": 77, "title": "Une série à venir", "year": "2030", "date": "2030-01-01", "overview": "Résumé 77"})
        self.assertEqual(self.tmdb.calls, ["search/tv?query=" + quote("série", safe="")])

    def test_without_a_kind_both_are_searched_films_first(self):
        results = self.found(self.alice, "a/b&c")
        self.assertEqual([r["kind"] for r in results], ["movie"] * 10 + ["show"] * 10)
        self.assertEqual(self.tmdb.calls, ["search/movie?query=a%2Fb%26c", "search/tv?query=a%2Fb%26c"])

    def test_no_adult_result_and_no_adult_switch(self):
        self.tmdb.answers["search/movie"] = {"results": [movie(666, "Adulte", adult=True), movie(3)]}
        self.assertEqual([r["tmdb"] for r in self.found(self.alice, kind="movie")], [3])
        self.assertNotIn("adult", self.tmdb.calls[0])

    def test_an_overview_is_cut_and_a_coming_release_stays(self):
        self.tmdb.answers["search/movie"] = {"results": [movie(600, "À venir", "2031-06-01", overview="x" * 450)]}
        [result] = self.found(self.alice, kind="movie")
        self.assertEqual((len(result["overview"]), result["date"], result["year"]), (300, "2031-06-01", "2031"))

    def test_odd_answers_from_tmdb_are_skipped_not_a_500(self):
        self.tmdb.answers["search/movie"] = {"results": [None, {"id": "12"}, {"id": True}, {"id": 0}, {"title": "sans id"}, movie(4, date="bientôt", overview=None), {"id": 5}]}
        self.assertEqual([(r["tmdb"], r["title"], r["date"], r["overview"]) for r in self.found(self.alice, kind="movie")], [(4, "Film 4", "", ""), (5, "", "", "")])
        self.tmdb.answers["search/movie"] = ["pas", "un", "objet"]
        self.assertEqual(self.found(self.alice, "autre", kind="movie"), [])

    def test_the_same_search_is_asked_once_within_ten_minutes(self):
        self.found(self.alice, "Dune", "movie")
        self.found(self.bob, "  dune ", "movie")
        self.assertEqual(self.tmdb.calls, ["search/movie?query=Dune"])

    def test_the_text_and_the_kind_are_checked_before_tmdb(self):
        message = {"error": "texte à chercher : de 2 à 100 caractères"}
        for text in ("", " a ", "x" * 101):
            reply = self.search(self.alice, text)
            self.assertEqual((reply.status, reply.json()), (400, message), text)
        reply = self.get(self.alice, "/api/requests/search")
        self.assertEqual((reply.status, reply.json()), (400, message))
        reply = self.search(self.alice, "film", "album")
        self.assertEqual((reply.status, reply.json()), (400, {"error": "sorte de demande inconnue (movie ou show)"}))
        self.assertEqual(self.tmdb.calls, [])
        self.assertEqual(self.search(self.alice, "  " + "x" * 100 + "  ").status, 200)
        self.assertEqual(self.search(self.alice, "ab").status, 200)

    def test_an_account_without_the_right_is_refused_and_tmdb_is_not_asked(self):
        self.post(self.boss, f"/api/user/{self.bob['id']}/request", {"request": False})
        reply = self.search(self.bob)
        self.assertEqual((reply.status, reply.json()), (403, {"error": SEARCH_FORBIDDEN}))
        self.assertEqual(self.tmdb.calls, [])
        self.assertEqual(self.search(self.alice).status, 200)

    def test_the_admin_searches_too(self):
        self.assertEqual(len(self.found(self.boss)), 20)

    def test_without_a_session_it_is_a_401(self):
        _, reply = call(self.hub, "GET", "/api/requests/search?q=film")
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.tmdb.calls, [])


class TestConfiguration(SearchCase):
    def test_without_a_tmdb_key_the_search_is_a_503(self):
        self.hub.cfg.tmdb_key = ""
        reply = self.search(self.alice)
        self.assertEqual((reply.status, reply.json()), (503, {"error": "la recherche n'est pas configurée sur ce serveur"}))
        self.assertEqual(self.tmdb.calls, [])

    def test_the_state_says_whether_the_account_can_search(self):
        self.assertIs(self.get(self.alice, "/api/state").json()["request_search"], True)
        self.assertIs(self.get(self.boss, "/api/state").json()["request_search"], True)
        self.post(self.boss, f"/api/user/{self.bob['id']}/request", {"request": False})
        self.assertIs(self.get(self.bob, "/api/state").json()["request_search"], False)
        self.hub.cfg.tmdb_key = ""
        self.assertIs(self.get(self.alice, "/api/state").json()["request_search"], False)

    def test_the_client_tries_once_for_ten_seconds_without_a_disk_cache(self):
        client = request_search.client(types.SimpleNamespace(tmdb_key=KEY))
        self.assertEqual((client.attempts, client.timeout, client.cache, client.language), (1, 10, None, "fr-FR"))


class RealClient(SearchCase):
    """Le vrai client de media_library, seul le transport (`http.fetch`) est simulé : ce qui part vers TMDB et ce qui en revient passent par le même code qu'en vrai."""

    def setUp(self):
        super().setUp()
        self.hub.finder.make = request_search.client
        self.sent = []

    def answer(self, body=b'{"results": []}', error=None):
        def fetch(url, headers, timeout, attempts, fail, refuse, **more):
            self.sent.append((url, headers, timeout, attempts))
            if error is not None:
                raise error(url)
            return body, {}
        return mock.patch.object(tmdb_module.http, "fetch", fetch)


class TestTmdbDown(RealClient):
    def test_a_failing_tmdb_is_a_502_that_never_shows_the_key_nor_the_url(self):
        failures = [lambda url: TmdbError(f"<urlopen error timed out> {url}"), lambda url: TmdbAuthError(f"clé TMDB refusée (HTTP 401) {url}"),
                    lambda url: TimeoutError(f"délai dépassé {url}")]
        for n, failure in enumerate(failures):
            with self.answer(error=failure):
                handler, reply = call(self.hub, "GET", f"/api/requests/search?q=Recherche{n}", headers=self.headers(self.alice))
            self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))
            for secret in (KEY, "api.themoviedb.org"):
                self.assertNotIn(secret.encode(), reply.body)
                self.assertNotIn(secret, handler.stderr)
            self.assertNotIn(f"Recherche{n}", handler.stderr)
            self.assertNotIn("alice", handler.stderr)
        self.assertEqual(len(self.sent), 3)

    def test_an_unreadable_answer_is_a_502(self):
        with self.answer(b"pas du json"):
            reply = self.search(self.alice)
        self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))

    def test_tmdb_only_receives_the_text_once_and_nothing_of_the_account(self):
        with self.answer():
            self.found(self.alice, "Amélie", "movie")
        [(url, headers, timeout, attempts)] = self.sent
        self.assertTrue(url.startswith("https://api.themoviedb.org/3/search/movie?query=Am%C3%A9lie&"), url)
        self.assertNotIn("include_adult", url)
        self.assertEqual((timeout, attempts), (10, 1))
        for who in ("alice", self.alice["id"]):
            self.assertNotIn(who, url + repr(headers))

    def test_ten_searches_write_nothing_on_the_disk(self):
        before = sorted(p.relative_to(self.hub.cfg.data) for p in self.hub.cfg.data.rglob("*"))
        with self.answer(b'{"results": [{"id": 3, "title": "Trois"}]}'):
            for n in range(10):
                self.found(self.alice, f"recherche {n}")
        self.assertEqual(len(self.sent), 20)
        self.assertFalse((self.hub.cfg.data / "tmdb-cache").exists())
        self.assertEqual(sorted(p.relative_to(self.hub.cfg.data) for p in self.hub.cfg.data.rglob("*")), before)


class TestLimits(SearchCase):
    def test_thirty_searches_a_minute_per_account_then_a_429_that_spares_the_others(self):
        for n in range(30):
            self.assertEqual(self.search(self.alice, f"film {n}").status, 200, n)
        reply = self.search(self.alice, "film 0")              # même gardée en mémoire, une recherche compte
        self.assertEqual(reply.status, 429)
        self.assertRegex(reply.json()["error"], r"^trop de recherches : réessaie dans [0-9]+ s$")
        self.assertEqual(self.search(self.bob, "film 0").status, 200)

    def test_two_searches_running_turn_a_third_away_at_once(self):
        self.tmdb.hold = threading.Event()
        jobs_ = [("GET", f"/api/requests/search?q=lent{n}&kind=movie", None, self.headers(who)) for n, who in enumerate((self.alice, self.bob))]
        out = [None, None]
        threads = [threading.Thread(target=lambda i=i, job=job: out.__setitem__(i, call(self.hub, *job))) for i, job in enumerate(jobs_)]
        for t in threads:
            t.start()
        self.assertTrue(self.tmdb.entered.acquire(timeout=10) and self.tmdb.entered.acquire(timeout=10))
        started = time.monotonic()
        reply = self.search(self.boss, "troisième", "movie")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((reply.status, reply.json()), (503, {"error": "le serveur est occupé, réessaie dans un instant"}))
        self.tmdb.hold.set()
        for t in threads:
            t.join(10)
        self.assertEqual([o[1].status for o in out], [200, 200])
        self.assertEqual(self.search(self.boss, "troisième", "movie").status, 200)


class TestPace(unittest.TestCase):
    def test_the_window_slides_and_old_accounts_are_swept(self):
        now = [1000.0]
        pace = request_search.Pace(clock=lambda: now[0])
        for _ in range(30):
            pace.take("a")
        with self.assertRaises(hub_requests.RequestError) as caught:
            pace.take("a")
        self.assertEqual((caught.exception.status, caught.exception.message), (429, "trop de recherches : réessaie dans 61 s"))
        now[0] += 45
        with self.assertRaises(hub_requests.RequestError) as caught:
            pace.take("a")
        self.assertEqual(caught.exception.message, "trop de recherches : réessaie dans 16 s")
        now[0] += 15
        pace.take("a")
        for n in range(pace.SWEEP + 1):
            pace.take(f"compte {n}")
        now[0] += 61
        pace.take("dernier")
        self.assertEqual(list(pace._calls), ["dernier"])


class TestMemo(unittest.TestCase):
    def test_the_memory_keeps_two_hundred_answers_ten_minutes(self):
        now = [0.0]
        memo = request_search.Memo(clock=lambda: now[0])
        for n in range(201):
            memo.put(n, [n])
        self.assertIsNone(memo.get(0))
        self.assertEqual(memo.get(1), [1])
        now[0] += 599
        self.assertEqual(memo.get(200), [200])
        now[0] += 2
        self.assertIsNone(memo.get(200))
        self.assertEqual(len(memo._kept), 199)


class TestWhatIsThere(SearchCase):
    def test_a_film_already_there_gives_its_item_only_where_the_account_sees_it(self):
        self.tmdb.answers["search/movie"] = {"results": [movie(1, "Un"), movie(7, "Privé"), movie(2)]}
        alice = {r["tmdb"]: r.get("item") for r in self.found(self.alice, kind="movie")}
        self.assertEqual((alice[1], alice[7], alice[2]), ("m1", "p7", None))
        bob = {r["tmdb"]: r for r in self.found(self.bob, kind="movie")}
        self.assertEqual(bob[1]["item"], "m1")
        self.assertNotIn("item", bob[7])
        self.assertNotIn("item", bob[2])

    def test_a_show_already_there_gives_its_item(self):
        results = {r["tmdb"]: r for r in self.found(self.alice, "série", "show")}
        self.assertEqual(results[50]["item"], "s50")
        self.assertNotIn("item", results[77])

    def test_a_requested_result_says_only_whether_it_is_ours(self):
        self.tmdb.answers["tv/77"] = show(77)
        rid = self.ask(self.alice, 2).json()["id"]
        self.assertEqual(self.ask(self.alice, 77, "show").status, 201)
        alice = {(r["kind"], r["tmdb"]): r for r in self.found(self.alice)}
        self.assertEqual(alice[("movie", 2)]["request"], {"status": "pending", "mine": True, "id": rid})
        self.assertEqual(alice[("show", 77)]["request"]["mine"], True)
        self.assertNotIn("request", alice[("movie", 3)])
        bob = {(r["kind"], r["tmdb"]): r for r in self.found(self.bob)}
        self.assertEqual(bob[("movie", 2)]["request"], {"status": "pending", "mine": False})
        self.assertNotIn("alice", self.search(self.bob).body.decode())

    def test_one_read_of_the_requests_and_one_look_at_the_library_per_search(self):
        self.hub.cfg.tmdb_key = ""
        self.ask(self.alice, 2)
        self.ask(self.bob, 77, "show")
        self.hub.cfg.tmdb_key = KEY
        self.found(self.alice)                       # TMDB en mémoire : seules les requêtes de la base restent à compter
        self.db._derived.clear()
        statements = self.statements_of(self.alice, "/api/requests/search?q=film")
        self.assertEqual(sum(s.lstrip().upper().startswith("SELECT") and "request" in s for s in statements), 1, statements)
        self.assertEqual(sum("FROM items" in s and "UNION ALL" in s for s in statements), 1, statements)

    def test_a_search_without_results_reads_nothing(self):
        self.tmdb.answers = {"search/movie": {"results": []}, "search/tv": {"results": []}}
        statements = self.statements_of(self.alice, "/api/requests/search?q=rien")
        self.assertEqual([s for s in statements if "request" in s or "FROM items" in s], [])


class TestAskingFromAResult(SearchCase):
    def setUp(self):
        super().setUp()
        self.tmdb.answers.update({"tv/77": show(77, "Une série à venir", "2030-01-01", poster_path="/s77.jpg"), "movie/88": movie(88, "Quatre-vingt-huit", "1988-08-08", poster_path="/m88.jpg")})

    def test_title_and_year_come_from_tmdb_not_from_the_body(self):
        self.assertEqual(self.ask(self.alice, 77, "show", title="Faux", year="1900").status, 201)
        self.assertEqual(self.ask(self.alice, 88, title="Faux aussi").status, 201)
        found = {r["tmdb"]: (r["title"], r["year"]) for r in self.mine(self.alice)}
        self.assertEqual(found, {77: ("Une série à venir", "2030"), 88: ("Quatre-vingt-huit", "1988")})
        self.assertEqual(self.tmdb.calls, ["tv/77", "movie/88"])

    def test_a_film_the_server_knows_and_a_request_joined_ask_nothing(self):
        self.assertEqual(self.ask(self.alice, 3, title="Autre").status, 201)
        self.assertEqual(self.ask(self.alice, 77, "show").status, 201)
        self.assertEqual(self.ask(self.bob, 77, "show", title="Faux").status, 200)
        self.assertEqual(self.tmdb.calls, ["tv/77"])
        self.assertEqual({r["tmdb"]: r["title"] for r in self.mine(self.alice)}, {3: "Film 3", 77: "Une série à venir"})

    def test_tmdb_down_creates_nothing(self):
        self.tmdb.error = TmdbError(f"HTTP 500 https://api.themoviedb.org/3/tv/77?api_key={KEY}")
        reply = self.ask(self.alice, 77, "show", title="Une série")
        self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))
        self.assertNotIn(KEY.encode(), reply.body)
        self.assertEqual(self.stored(), [])
        self.hub.jobs.request_posters.assert_not_called()

    def test_an_id_tmdb_does_not_know_is_a_404_and_creates_nothing(self):
        reply = self.ask(self.alice, 999, "show", title="Inventée")
        self.assertEqual((reply.status, reply.json()), (404, {"error": "titre inconnu sur TMDB"}))
        self.tmdb.answers["movie/666"] = movie(666, "Adulte", adult=True)
        self.assertEqual(self.ask(self.alice, 666).status, 404)
        self.assertEqual(self.stored(), [])

    def test_without_a_key_the_body_is_kept_as_before(self):
        self.hub.cfg.tmdb_key = ""
        self.assertEqual(self.ask(self.alice, 77, "show", title="Ce que dit le client", year="2019").status, 201)
        self.assertEqual([(r["title"], r["year"]) for r in self.mine(self.alice)], [("Ce que dit le client", "2019")])
        self.assertEqual(self.tmdb.calls, [])
        self.hub.jobs.request_posters.assert_not_called()

    def test_the_posters_are_fetched_after_a_new_request_never_after_a_join(self):
        self.ask(self.alice, 77, "show")
        self.assertEqual(self.hub.jobs.request_posters.call_count, 1)
        self.ask(self.bob, 77, "show")
        self.ask(self.alice, 1)                                          # déjà là : 409
        self.assertEqual(self.hub.jobs.request_posters.call_count, 1)

    def test_a_failing_poster_job_never_fails_the_request(self):
        self.hub.jobs.request_posters.side_effect = RuntimeError("file des tâches en panne")
        reply = self.ask(self.alice, 77, "show")
        self.assertEqual(reply.status, 201)
        self.assertEqual(len(self.stored()), 1)


class TestPosters(SearchCase):
    def setUp(self):
        super().setUp()
        self.tmdb.answers.update({"movie/2": movie(2, poster_path="/m2.jpg"), "tv/77": show(77), "movie/88": movie(88, poster_path="/m88.jpg"), "movie/1": movie(1, poster_path="/m1.jpg"),
                                  "tv/78": show(78, poster_path="/s78.jpg"), "movie/5": movie(5, poster_path="/../../etc/passwd")})
        self.tmdb.failing = {"/m88.jpg"}
        self.posters = self.hub.cfg.data / "tmdb"
        self.hub.cfg.tmdb_key = ""                                       # les demandes se font sans TMDB : on ne compte ici que les appels de la tâche
        for who, kind, tmdb in ((self.alice, "movie", 2), (self.alice, "show", 77), (self.alice, "movie", 88), (self.alice, "show", 78), (self.boss, "movie", 1), (self.boss, "movie", 5)):
            self.ask(who, tmdb, kind, title=f"Titre {tmdb}")
        self.hub.cfg.tmdb_key = KEY
        self.log = []

    def fetch(self):
        return request_search.fetch_posters(self.hub.cfg, self.db, self.log.append, make=lambda cfg: self.tmdb)

    def test_the_posters_of_requests_are_fetched_once_and_a_failure_stops_nothing(self):
        result = self.fetch()
        self.assertEqual(result, {"saved": 2, "failed": 1, "without": 2})
        self.assertEqual(sorted(p.name for p in self.posters.iterdir()), ["78.jpg", "film-2.jpg"])
        self.assertEqual(sorted(self.tmdb.images), [("/m2.jpg", "w342"), ("/m88.jpg", "w342"), ("/s78.jpg", "w342")])
        self.assertNotIn("movie/1", self.tmdb.calls)                    # déjà là : rien n'est demandé
        self.assertNotIn(KEY, "\n".join(self.log))
        self.tmdb.calls.clear()
        self.fetch()
        self.assertNotIn("movie/2", self.tmdb.calls)
        self.assertNotIn("tv/78", self.tmdb.calls)

    def test_tmdb_down_fails_each_poster_and_a_refused_key_fails_the_job(self):
        self.tmdb.error = TmdbError("<urlopen error timed out>")
        self.assertEqual(self.fetch(), {"saved": 0, "failed": 5, "without": 0})
        self.tmdb.error = TmdbAuthError("clé TMDB refusée")
        with self.assertRaises(RuntimeError):
            self.fetch()
        self.assertFalse(any(p.suffix != ".jpg" for p in self.posters.iterdir()))

    def test_without_a_key_nothing_is_asked(self):
        self.hub.cfg.tmdb_key = ""
        self.assertEqual(self.fetch(), {"saved": 0, "failed": 0, "without": 0})
        self.assertEqual(self.tmdb.calls, [])

    def test_the_cover_and_the_image_of_a_request(self):
        self.fetch()
        self.hub.images.file.return_value = (b"jpeg", "image/jpeg", "cle")
        covers = {r["tmdb"]: (r["id"], r["cover"]) for r in self.mine(self.alice)}
        rid = covers[78][0]
        self.assertEqual({tmdb: cover for tmdb, (_, cover) in covers.items()}, {2: "/img/film/2", 78: f"/img/request/{rid}", 77: "", 88: ""})
        self.assertEqual({r["tmdb"]: r["cover"] for r in self.everything()}[78], f"/img/request/{rid}")
        self.assertEqual(self.get(self.alice, f"/img/request/{rid}").status, 200)
        self.assertEqual(self.get(self.boss, f"/img/request/{rid}").status, 200)
        self.assertEqual(self.get(self.bob, f"/img/request/{rid}").status, 404)            # pas sa demande : comme une demande inconnue
        self.assertEqual(self.get(self.alice, f"/img/request/{covers[77][0]}").status, 404)  # sans affiche
        self.assertEqual(self.get(self.alice, f"/img/request/{covers[2][0]}").status, 200)   # un film aussi
        self.assertEqual(self.get(self.alice, "/img/request/99999").status, 404)
        token = self.hub.media_token(accounts.by_id(self.db, self.alice["id"]))
        _, reply = call(self.hub, "GET", f"/img/request/{rid}?token={token}")
        self.assertEqual(reply.status, 200)


class TestPosterJob(JobsCase):
    def test_asking_again_while_it_waits_gives_the_same_job(self):
        blocker = Waiting()
        self.jobs._submit("Occupée", blocker)
        blocker.started.wait(5)
        with mock.patch.object(jobs.request_search, "fetch_posters", return_value={"saved": 0, "failed": 0, "without": 0}) as fetched:
            first = self.jobs.request_posters()
            self.assertIs(self.jobs.request_posters(), first)
            self.assertEqual(first.label, "Affiches des demandes")
            blocker.go.set()
            self.wait(first)
            self.assertEqual(fetched.call_count, 1)


if __name__ == "__main__":
    unittest.main()
