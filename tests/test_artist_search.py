"""Demander un artiste en le cherchant dans MusicBrainz, avec repli sur le nom tapé : la route `GET /api/requests/artists` (droit « Peut demander », longueur du texte, une recherche à la fois, une requête par seconde,
un seul essai de 10 s, trente recherches par minute et par compte, mémoire de dix minutes) et ce qu'elle dit de chaque résultat (déjà dans une bibliothèque que le compte voit, déjà demandé). MusicBrainz est simulé : aucun appel réseau."""

from __future__ import annotations

import json
import threading
import unittest
import warnings
from unittest import mock
from urllib.parse import quote

from tests.rig import call
from tests.test_requests import FORBIDDEN, MB1, MB2, MB3, ArtistCase
from hub import artist_search, enrich, request_search
from libraries.music import musicbrainz as client_module

UNREACHABLE = {"error": "MusicBrainz ne répond pas : tu peux envoyer le nom tel quel"}


def found(mbid, name, **more):
    """Un artiste tel que la recherche de MusicBrainz le rend."""
    return {"id": mbid, "name": name, "type": "Group", "country": "FR", "score": 100, "life-span": {"begin": "1993-01-01", "end": "2021-02-22", "ended": True}, **more}


class FakeMusicBrainz:
    """Le client de MusicBrainz, sans réseau : `results` répond à `search_artists`, `error` est levée, `hold` (un Event) est attendu dans l'appel."""

    def __init__(self, results=(), error=None, hold=None):
        self.results, self.error, self.hold = list(results), error, hold
        self.asked = []
        self.entered = threading.Semaphore(0)

    def search_artists(self, name, limit=10):
        self.asked.append((name, limit))
        self.entered.release()
        if self.hold is not None:
            self.hold.wait(10)
        if self.error is not None:
            raise self.error
        return self.results


class Clock:
    """Une horloge à la main : `sleep` l'avance et note ce qu'on a attendu."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


class SearchCase(ArtistCase):
    def setUp(self):
        # Ces tests lisent stderr (`handler.stderr`) : une base d'un test précédent que le ramasse-miettes détruit pendant la requête y écrirait « unclosed database » (Python 3.14).
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", ResourceWarning)
        super().setUp()
        self.addCleanup(self.db.connection().close)
        self.original = enrich.musicbrainz
        self.clock = Clock()
        self.hub.artist_finder = artist_search.Finder(self.hub.cfg, clock=self.clock, sleep=self.clock.sleep)
        self.mb = FakeMusicBrainz([found(MB1, "Daft Punk", disambiguation="duo électronique"), found(MB2, "Daft Punk", type="Person", country="CA", disambiguation="homonyme", **{"life-span": {"begin": "1980"}})])
        self.make = self.enterContext(mock.patch.object(enrich, "musicbrainz", return_value=self.mb))

    def search(self, who, text="Daft Punk"):
        return self.get(who, "/api/requests/artists?q=" + quote(text))

    def results(self, who, text="Daft Punk"):
        reply = self.search(who, text)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json()["results"]


class TestSearching(SearchCase):
    def test_a_result_gives_what_tells_namesakes_apart_and_the_page_of_the_artist(self):
        results = self.results(self.alice)
        self.assertEqual(results[0], {"mbid": MB1, "name": "Daft Punk", "type": "Group", "country": "FR", "disambiguation": "duo électronique", "begin": "1993", "end": "2021", "url": f"https://musicbrainz.org/artist/{MB1}"})
        self.assertEqual(results[1], {"mbid": MB2, "name": "Daft Punk", "type": "Person", "country": "CA", "disambiguation": "homonyme", "begin": "1980", "end": "", "url": f"https://musicbrainz.org/artist/{MB2}"})
        self.assertEqual(self.mb.asked, [("Daft Punk", artist_search.RESULTS)])

    def test_what_musicbrainz_gives_badly_is_left_out(self):
        self.mb.results = [found("pas-un-identifiant", "Mauvais"), found(MB1, ""), found(MB1, "Dai\x00ft"), found(MB1, "x" * 201), found(MB1, "Various Artists"), found(MB1, "Artiste inconnu"), "texte", None,
                           {"name": "Sans identifiant"}, found(MB3.upper(), "  Air  ", type=7, country=None, disambiguation=["x"], **{"life-span": "pas un objet"})]
        self.assertEqual(self.results(self.alice, "air"), [{"mbid": MB3, "name": "Air", "type": "", "country": "", "disambiguation": "", "begin": "", "end": "", "url": f"https://musicbrainz.org/artist/{MB3}"}])

    def test_at_most_ten_results_come_back_whatever_musicbrainz_sends(self):
        self.mb.results = [found(f"{n:08d}-0000-0000-0000-000000000000", f"Artiste {n}") for n in range(30)]
        self.assertEqual(len(self.results(self.alice)), 10)

    def test_a_result_says_when_it_is_already_in_a_library_the_account_sees(self):
        self.scanned("musique", self.album("musique", "a1", "Daft  PUNK", "ar9"))
        self.scanned("musprive", self.album("musprive", "p1", "Air", "ar2"))
        self.mb.results = [found(MB1, "Daft Punk"), found(MB3, "Air")]
        mine, bobs, boss = self.results(self.alice), self.results(self.bob), self.results(self.boss)
        self.assertEqual(mine[0]["artist"], {"lib": "musique", "id": "ar9", "name": "Daft  PUNK"})
        self.assertEqual(mine[1]["artist"], {"lib": "musprive", "id": "ar2", "name": "Air"})
        self.assertEqual((bobs[0]["artist"]["lib"], "artist" in bobs[1]), ("musique", False))          # « Air » n'est que dans une bibliothèque que Bob ne voit pas
        self.assertEqual(boss[1]["artist"], {"lib": "musprive", "id": "ar2", "name": "Air"})

    def test_a_result_is_known_by_its_mbid_when_the_library_knows_it(self):
        self.scanned("musique", self.album("musique", "a1", "Les Robots", "ar9", mbid=MB1))
        self.mb.results = [found(MB1, "Daft Punk"), found(MB2, "Daft Punk")]
        got = self.results(self.alice)
        self.assertEqual(got[0]["artist"], {"lib": "musique", "id": "ar9", "name": "Les Robots"})          # l'artiste est là, sous un autre nom
        self.assertNotIn("artist", got[1])

    def test_a_result_says_whether_it_is_already_asked_and_by_whom_only_for_oneself(self):
        rid = self.ask_artist(self.alice, "daft punk").json()["id"]
        self.assertEqual([r["request"] for r in self.results(self.alice)], [{"status": "pending", "mine": True, "id": rid}] * 2)
        self.assertEqual([r["request"] for r in self.results(self.bob)], [{"status": "pending", "mine": False}] * 2)
        self.mb.results = [found(MB3, "Air")]
        self.assertNotIn("request", self.results(self.alice, "air")[0])

    def test_a_request_that_the_library_answers_is_added_when_the_search_reads_it(self):
        self.ask_artist(self.alice, "Daft Punk")
        self.scanned("musique", self.album("musique", "a1", "Daft Punk"))
        self.assertEqual(self.results(self.alice)[0]["request"]["status"], "added")
        self.assertEqual(self.stored_artists(), [("artist", "added")])

    def test_the_text_goes_from_one_to_two_hundred_characters(self):
        for text in ("", "   ", "x" * 201):
            reply = self.search(self.alice, text)
            self.assertEqual((reply.status, reply.json()), (400, {"error": "texte à chercher : de 1 à 200 caractères"}), repr(text))
        self.assertEqual(self.get(self.alice, "/api/requests/artists").status, 400)
        self.assertEqual(self.mb.asked, [])
        self.assertEqual(self.search(self.alice, "x").status, 200)
        self.assertEqual(self.search(self.alice, "  " + "y" * 200 + "  ").status, 200)

    def test_only_an_account_that_may_ask_searches(self):
        self.post(self.boss, f"/api/user/{self.alice['id']}/request", {"request": False})
        reply = self.search(self.alice)
        self.assertEqual((reply.status, reply.json()), (403, {"error": FORBIDDEN}))
        _, anonymous = call(self.hub, "GET", "/api/requests/artists?q=air")
        self.assertEqual(anonymous.status, 401)
        self.assertEqual(self.mb.asked, [])
        self.assertEqual(self.search(self.boss).status, 200)

    def test_the_route_does_not_swallow_the_one_of_the_tmdb_search(self):
        self.assertEqual(self.get(self.alice, "/api/requests/search?q=film").status, 503)           # sans clé TMDB : sa route à lui
        self.assertEqual(self.mb.asked, [])


class TestMusicBrainzDown(SearchCase):
    def test_a_failing_musicbrainz_is_a_502_in_french_with_nothing_technical(self):
        failures = [enrich.MusicBrainzError("HTTP 503 https://musicbrainz.org/ws/2/artist/?query=Secret", 503), enrich.MusicBrainzError("délai dépassé"), OSError("réseau coupé Secret"), ValueError("illisible"),
                    AttributeError("'list' object has no attribute 'get'"), RecursionError("trop profond")]
        for n, failure in enumerate(failures):
            self.mb.error = failure
            handler, reply = call(self.hub, "GET", f"/api/requests/artists?q=Secret{n}", headers=self.headers(self.alice))
            self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE), repr(failure))
            self.assertNotIn(b"musicbrainz.org", reply.body)
            self.assertNotIn(f"Secret{n}", handler.stderr)                    # le texte cherché ne va pas au journal
            self.assertNotIn("alice", handler.stderr)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 0)

    def test_a_failure_is_not_kept_in_memory(self):
        self.mb.error = enrich.MusicBrainzError("délai dépassé")
        self.assertEqual(self.search(self.alice).status, 502)
        self.mb.error = None
        self.assertEqual(len(self.results(self.alice)), 2)

    def test_the_name_can_still_be_sent_as_typed_after_a_failure(self):
        self.mb.error = enrich.MusicBrainzError("délai dépassé")
        self.assertEqual(self.search(self.alice, "Air").status, 502)
        reply = self.ask_artist(self.alice, "Air")
        self.assertEqual((reply.status, reply.json()["status"]), (201, "pending"))

    def test_a_search_that_wipes_out_the_database_is_not_possible_through_the_text(self):
        for text in ('Daft" OR artist:"x', "Daft\\Punk", "'; DROP TABLE requests; --"):
            self.assertEqual(self.search(self.alice, text).status, 200, text)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM requests")[0], 0)


class TestTheClient(SearchCase):
    """Ce qui part vers MusicBrainz : le vrai client de media_library, seul le transport (`http.fetch`) est simulé."""

    def setUp(self):
        super().setUp()
        self.sent = []

    def answer(self, body, error=None):
        def fetch(url, headers, timeout, attempts, fail, refuse, **more):
            self.sent.append((url, headers, timeout, attempts))
            if error is not None:
                raise error
            return body, {}
        return mock.patch.object(client_module.http, "fetch", fetch)

    def original_client(self):
        """Le vrai `enrich.musicbrainz`, le temps d'un bloc (SearchCase le remplace par un double)."""
        return mock.patch.object(enrich, "musicbrainz", self.original)

    def test_one_try_of_ten_seconds_with_the_hub_user_agent_and_nothing_of_the_account(self):
        body = json.dumps({"artists": [found(MB1, "Daft Punk")]}).encode()
        with self.original_client(), self.answer(body):
            results = self.results(self.alice, "Daft Punk")
        self.assertEqual([r["mbid"] for r in results], [MB1])
        [(url, headers, timeout, attempts)] = self.sent
        self.assertTrue(url.startswith("https://musicbrainz.org/ws/2/artist/?query="), url)
        self.assertIn("Daft%20Punk", url)
        self.assertEqual((timeout, attempts), (artist_search.TIMEOUT, artist_search.ATTEMPTS))
        self.assertEqual((artist_search.TIMEOUT, artist_search.ATTEMPTS), (10, 1))
        self.assertEqual(headers["User-Agent"], enrich.MB_USER_AGENT)
        for who in ("alice", self.alice["id"], self.hub.secret):
            self.assertNotIn(who, url + repr(headers))

    def test_musicbrainz_down_at_the_transport_is_the_same_502(self):
        with self.original_client(), self.answer(b"", error=client_module.MusicBrainzError("HTTP 503 Service Unavailable", 503)):
            reply = self.search(self.alice, "Air")
        self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))

    def test_an_unreadable_answer_is_a_502(self):
        with self.original_client(), self.answer(b"pas du json"):
            self.assertEqual(self.search(self.alice, "Air").status, 502)

    def test_an_answer_that_is_not_a_list_of_artists_is_a_502_or_empty_never_a_500(self):
        for body in (b'{"artists": "x"}', b"[]", b'{"artists": [1, null, "a"]}', b"null"):
            with self.original_client(), self.answer(body):
                reply = self.search(self.alice, f"Air{body[:6]!r}")
            self.assertIn(reply.status, (200, 502), body)
            if reply.status == 200:
                self.assertEqual(reply.json(), {"results": []}, body)


class TestPaceAndTurns(SearchCase):
    def test_a_search_is_kept_in_memory_whatever_the_case_and_asks_musicbrainz_once(self):
        self.results(self.alice, "Daft Punk")
        self.results(self.bob, "daft punk")
        self.assertEqual(len(self.mb.asked), 1)
        self.assertEqual(self.clock.slept, [])
        self.results(self.alice, "Air")
        self.assertEqual(len(self.mb.asked), 2)

    def test_no_pause_at_all_once_the_interval_has_passed_exactly(self):
        self.results(self.alice, "un")
        self.clock.now += artist_search.INTERVAL
        self.results(self.alice, "deux")
        self.assertEqual((len(self.mb.asked), self.clock.slept), (2, []))                 # pas même une attente de zéro seconde

    def test_musicbrainz_is_asked_at_most_once_per_interval(self):
        self.results(self.alice, "un")
        self.results(self.alice, "deux")
        self.results(self.bob, "trois")
        self.assertEqual(self.clock.slept, [artist_search.INTERVAL, artist_search.INTERVAL])
        self.assertGreaterEqual(artist_search.INTERVAL, 1.0)
        self.clock.now += 5
        self.results(self.alice, "quatre")
        self.assertEqual(len(self.clock.slept), 2)                           # cinq secondes après : pas d'attente
        self.clock.now += 0.4
        self.results(self.alice, "cinq")
        self.assertEqual(self.clock.slept[2:], [0.7])                        # seul ce qui manque à l'intervalle s'attend

    def test_a_second_search_waits_its_turn_then_is_refused_busy_in_french(self):
        hold = threading.Event()
        self.mb.hold = hold
        self.addCleanup(hold.set)
        first = []
        with mock.patch.object(artist_search, "WAIT", 0.3):
            thread = threading.Thread(target=lambda: first.append(call(self.hub, "GET", "/api/requests/artists?q=un", headers=self.headers(self.alice))))
            thread.start()
            self.assertTrue(self.mb.entered.acquire(timeout=10))
            reply = self.search(self.bob, "deux")
            hold.set()
            thread.join(10)
        self.assertEqual((reply.status, reply.json()), (503, {"error": "la recherche d'artistes est occupée : tu peux envoyer le nom tel quel"}))
        self.assertEqual(first[0][1].status, 200)
        self.assertEqual(len(self.mb.asked), 1)                              # la seconde n'a rien demandé à MusicBrainz

    def test_thirty_searches_a_minute_per_account_and_the_others_are_not_touched(self):
        for n in range(request_search.Pace.LIMIT):
            self.assertEqual(self.search(self.alice, "Daft Punk").status, 200, n)
        reply = self.search(self.alice, "Daft Punk")
        self.assertEqual(reply.status, 429)
        self.assertIn("trop de recherches : réessaie dans", reply.json()["error"])
        self.assertEqual(self.search(self.bob, "Daft Punk").status, 200)


class TestTheLogNeverKeepsTheText(SearchCase):
    def test_a_server_error_names_the_route_without_the_text_searched(self):
        self.mb.error = enrich.MusicBrainzError("délai dépassé")
        handler, reply = call(self.hub, "GET", "/api/requests/artists?q=Texte+Secret&token=abc", headers=self.headers(self.alice))
        self.assertEqual(reply.status, 502)
        self.assertIn("/api/requests/artists?q=***&token=***", handler.stderr)
        self.assertNotIn("Secret", handler.stderr)


if __name__ == "__main__":
    unittest.main()
