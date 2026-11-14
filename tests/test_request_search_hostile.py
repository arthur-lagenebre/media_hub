"""La recherche des demandes face à des entrées hostiles, sans réseau : texte aux caractères spéciaux, très long, de contrôle ou hors de l'alphabet latin (ce que TMDB reçoit : un seul paramètre `query`,
encodé, sur le bon chemin), deux comptes (ou plusieurs sessions d'un même compte) qui cherchent en même temps, et la clé TMDB, qui ne sort dans aucune réponse ni aucun journal.
Le vrai client de media_library tourne ; seul son transport est simulé (voir `RealClient`). Les fils partent ensemble sur un `Barrier`, sans `sleep`."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlsplit

from tests.rig import call
from tests.test_requests import PASSWORD
from tests.test_request_search import KEY, RealClient, SearchCase, UNREACHABLE, movie
from hub import accounts, request_search
from hub.server import jobs
from hub.server.app import HttpError
from libraries.video import tmdb as tmdb_module
from libraries.video.tmdb import TmdbAuthError, TmdbError

LAG = 10                     # secondes : un point de rendez-vous ou une tâche qui n'aboutit pas échoue le test au lieu de le bloquer


class Sent(RealClient):
    """Ce qui part vers TMDB, relu comme TMDB le lirait."""

    def go(self, who, text, kind="movie"):
        """(gestionnaire, réponse) de la recherche de `text`, tout encodé comme le fait un navigateur."""
        return call(self.hub, "GET", "/api/requests/search?q=" + quote(text, safe="") + f"&kind={kind}", headers=self.headers(who))

    def parsed(self, url):
        """Le texte que TMDB lit dans `url` ; échoue si l'adresse n'est pas sur le bon chemin, a un paramètre de plus ou de moins, ou n'est pas du ASCII imprimable."""
        split = urlsplit(url)
        self.assertEqual((split.scheme, split.netloc, split.path, split.fragment), ("https", "api.themoviedb.org", "/3/search/movie", ""), url)
        self.assertTrue(url.isascii() and url.isprintable() and " " not in url, ascii(url))
        pairs = parse_qsl(split.query, keep_blank_values=True, strict_parsing=True)
        self.assertEqual([key for key, _ in pairs], ["query", "api_key", "language"], url)
        return pairs[0][1]

    def only_sent(self):
        [(url, *_)] = self.sent
        return self.parsed(url)

    def forget(self):
        """Oublie ce qui est en mémoire et ce qui est parti : la recherche suivante ira jusqu'à TMDB, même pour un texte déjà cherché."""
        self.sent.clear()
        self.hub.finder.memo = request_search.Memo()


class TestSpecialCharacters(Sent):
    TEXTS = ("a&b=c", "x&kind=show", "x&api_key=autre", "où ?", "a#b", "a/b", "../../etc/passwd", "..\\..\\x", "100%", "%2e%2e%2f", "50%25", "a+b", '"deux mots"', "l'ami", "<b>x</b>", "a;b=c", "ab\tcd", "ab\r\ncd")

    def test_each_text_reaches_tmdb_as_one_encoded_parameter_on_the_same_path(self):
        for text in self.TEXTS:
            with self.subTest(text=text), self.answer():
                self.forget()
                handler, reply = self.go(self.alice, text)
                self.assertEqual((reply.status, reply.json()), (200, {"results": []}))
                self.assertEqual(self.only_sent(), text)               # un seul appel, pour les films : `&kind=show` du texte ne change pas la sorte
                self.assertEqual(handler.stderr, "")

    def test_a_raw_query_is_decoded_once_then_sent_encoded(self):
        raw = (("a+b", "a b"), ("a%2Bb", "a+b"), ("%2e%2e%2fx", "../x"), ("%0Aab%0A", "\nab\n"), ("ab%FFcd", "ab\ufffdcd"), ("ab%ED%A0%80", "ab\ufffd\ufffd\ufffd"),
               ("ab&q=cd", "cd"), ("ab&kind=show&kind=movie", "ab"), ("ab#cd", "ab"), ("a%252Fb", "a%2Fb"))
        for query, text in raw:
            with self.subTest(query=query), self.answer():
                self.forget()
                handler, reply = call(self.hub, "GET", "/api/requests/search?kind=movie&q=" + query, headers=self.headers(self.alice))
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(self.only_sent(), text.strip() if text.strip() else text)
                self.assertEqual(handler.stderr, "")

    def test_the_text_never_comes_back_in_the_answer(self):
        with self.answer():
            _, reply = self.go(self.alice, "<script>alert(1)</script>")
        self.assertEqual(reply.json(), {"results": []})
        self.assertNotIn(b"script", reply.body)


class TestControlCharacters(Sent):
    def setUp(self):
        super().setUp()
        self.hub.finder.pace.LIMIT = 10 ** 6                  # 34 recherches de suite : la cadence n'est pas ce qu'on regarde ici

    def test_a_null_byte_is_encoded_never_sent_raw(self):
        with self.answer():
            handler, reply = call(self.hub, "GET", "/api/requests/search?kind=movie&q=ab%00cd", headers=self.headers(self.alice))
        self.assertEqual(reply.status, 200)
        self.assertEqual(self.only_sent(), "ab\x00cd")
        self.assertIn("ab%00cd", self.sent[0][0])
        self.assertEqual(handler.stderr, "")

    def test_every_control_character_inside_a_text_is_encoded(self):
        with self.answer():
            for code in [*range(0x20), 0x7F, 0x85, 0x2028]:
                text = f"a{chr(code)}b"
                with self.subTest(code=hex(code)):
                    self.forget()
                    reply = self.go(self.alice, text)[1]
                    self.assertEqual(reply.status, 200, hex(code))
                    self.assertEqual(self.only_sent(), text)

    def test_only_control_characters_never_make_a_broken_request(self):
        with self.answer():
            for text in ("\x00\x00", "\x01\x02\x03", "\x7f\x7f"):
                with self.subTest(text=ascii(text)):
                    self.forget()
                    handler, reply = self.go(self.alice, text)
                    self.assertEqual((reply.status, reply.json()), (400, {"error": "texte à chercher : de 2 à 100 caractères"}))      # aucun caractère utile
                    self.assertEqual(self.sent, [])
                    self.assertEqual(handler.stderr, "")
        with self.answer():
            self.forget()
            self.assertEqual(self.go(self.alice, "a\x00\u200bb")[1].status, 200)               # deux caractères utiles suffisent, les autres partent encodés
            self.assertEqual(self.only_sent(), "a\x00\u200bb")


class TestTextLength(Sent):
    def test_a_hundred_characters_pass_and_a_hundred_and_one_do_not(self):
        with self.answer():
            for count, status in ((99, 200), (100, 200), (101, 400)):
                with self.subTest(count=count):
                    self.forget()
                    reply = self.go(self.alice, "x" * count)[1]
                    self.assertEqual(reply.status, status)
                    self.assertEqual(len(self.sent), 1 if status == 200 else 0)

    def test_the_blanks_at_the_edges_do_not_count_but_the_ones_inside_do(self):
        inside = "a" + " " * 98 + "b"                                    # 100 caractères, 98 blancs au milieu
        cases = (("  " + "x" * 100 + "  ", 200, "x" * 100), ("\t\n " + "x" * 100 + "\u00a0", 200, "x" * 100), (inside, 200, inside),
                 (" " + inside + " ", 200, inside), ("a" + " " * 99 + "b", 400, None), ("a" + " " * 100, 400, None), (" " * 200, 400, None))
        with self.answer():
            for text, status, sent in cases:
                with self.subTest(text=ascii(text)[:30], status=status):
                    self.forget()
                    handler, reply = self.go(self.alice, text)
                    self.assertEqual(reply.status, status, reply.body)
                    if status == 200:
                        self.assertEqual(self.only_sent(), sent)
                    else:
                        self.assertEqual((reply.json(), self.sent), ({"error": "texte à chercher : de 2 à 100 caractères"}, []))
                    self.assertEqual(handler.stderr, "")

    def test_a_huge_text_is_a_400_and_tmdb_is_not_asked(self):
        with self.answer():
            reply = self.go(self.alice, "x" * 200_000)[1]
            reply_blank = self.go(self.alice, " " * 200_000)[1]
        self.assertEqual((reply.status, reply_blank.status, self.sent), (400, 400, []))

    def test_two_characters_are_the_least(self):
        with self.answer():
            statuses = [self.go(self.alice, text)[1].status for text in ("é", "日", "🎬", " ab", "日本", "🎬🎬")]
        self.assertEqual(statuses, [400, 400, 400, 200, 200, 200])


class TestOtherAlphabets(Sent):
    TEXTS = ("千と千尋の神隠し", "الجوكر", "Война и мир", "🎬🍿", "Amélie 日本 🎬 ✓")

    def test_the_text_goes_to_tmdb_in_utf8_percent_encoding(self):
        for text in self.TEXTS:
            with self.subTest(text=text), self.answer():
                self.forget()
                handler, reply = self.go(self.alice, text)
                self.assertEqual(reply.status, 200)
                self.assertEqual(self.only_sent(), text)
                self.assertIn("query=" + quote(text, safe="") + "&", self.sent[0][0])
                self.assertEqual(handler.stderr, "")

    def test_characters_are_counted_not_bytes_the_same_for_every_script(self):
        for char in ("日", "ب", "🎬", "é", "ы"):
            for count, status in ((100, 200), (101, 400)):
                with self.subTest(char=char, count=count), self.answer():
                    self.forget()
                    reply = self.go(self.alice, char * count)[1]
                    self.assertEqual(reply.status, status)
                    if status == 200:
                        self.assertEqual(self.only_sent(), char * count)
                    else:
                        self.assertEqual(self.sent, [])

    def test_a_title_in_another_script_comes_back_whole(self):
        body = json.dumps({"results": [{"id": 129, "title": "千と千尋の神隠し", "release_date": "2001-07-20", "overview": "عنوان 🎬"}]}, ensure_ascii=False).encode()
        with self.answer(body):
            reply = self.go(self.alice, "千と千尋")[1]
        [result] = reply.json()["results"]
        self.assertEqual((result["title"], result["overview"], result["year"]), ("千と千尋の神隠し", "عنوان 🎬", "2001"))

    def test_the_memory_ignores_case_in_every_script(self):
        with self.answer():
            self.go(self.alice, "ВОЙНА И МИР")
            self.go(self.bob, "война и мир")
            self.go(self.alice, "ÉCOLE")
            self.go(self.bob, "école")
        self.assertEqual(len(self.sent), 2)


class Together(SearchCase):
    """Des fils qui cherchent ensemble : TMDB simulé, la mémoire déjà garnie des textes cherchés (un fil qui perdrait la course aux deux places d'appel compterait un 503 que ces tests ne regardent pas)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, sys, "stderr", sys.stderr)          # `call` détourne stderr : des fils qui s'y croisent peuvent le laisser détourné

    def together(self, plans, kind="movie"):
        """`plans` : [(en-têtes, nombre de recherches)] ; un fil par plan, tous partent en même temps. Rend les statuts reçus par plan, dans l'ordre."""
        for n in range(max(count for _, count in plans)):
            self.hub.finder.search(f"film {n}", (kind,))
        barrier, out = threading.Barrier(len(plans)), [[] for _ in plans]

        def run(slot, headers, count):
            barrier.wait(LAG)
            for n in range(count):
                out[slot].append(call(self.hub, "GET", f"/api/requests/search?q=film%20{n}&kind={kind}", headers=headers)[1].status)

        threads = [threading.Thread(target=run, args=(slot, *plan)) for slot, plan in enumerate(plans)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(LAG * 3)
        self.assertFalse(any(t.is_alive() for t in threads))
        return out


class TestTwoAccountsAtOnce(Together):
    def test_each_account_counts_its_own_thirty_while_both_search(self):
        out = self.together([(self.headers(self.alice), 31), (self.headers(self.bob), 31)])
        self.assertEqual(out, [[200] * 30 + [429]] * 2)
        self.assertEqual({key: len(times) for key, times in self.hub.finder.pace._calls.items()}, {self.alice["id"]: 30, self.bob["id"]: 30})

    def test_one_account_at_its_limit_does_not_slow_the_other(self):
        out = self.together([(self.headers(self.alice), 40), (self.headers(self.bob), 10)])
        self.assertEqual(out[1], [200] * 10)
        self.assertEqual((out[0].count(200), out[0].count(429)), (30, 10))

    def test_each_window_slides_on_its_own(self):
        now = [1000.0]
        self.hub.finder = request_search.Finder(self.hub.cfg, clock=lambda: now[0])
        self.hub.finder.make = lambda cfg: self.tmdb
        statuses = lambda who, first, last: [self.search(who, f"film {n}").status for n in range(first, last)]
        self.assertEqual(statuses(self.alice, 0, 30), [200] * 30)
        self.assertEqual(statuses(self.bob, 0, 15), [200] * 15)
        now[0] = 1040
        self.assertEqual(statuses(self.bob, 15, 30), [200] * 15)
        self.assertEqual((statuses(self.alice, 0, 1), statuses(self.bob, 0, 1)), ([429], [429]))
        now[0] = 1061                                            # la fenêtre d'Alice est vide ; celle de Bob garde ses 15 recherches de 1040
        self.assertEqual(statuses(self.alice, 0, 31), [200] * 30 + [429])
        self.assertEqual(statuses(self.bob, 0, 16), [200] * 15 + [429])

    def test_the_two_places_are_shared_by_every_account_and_a_memorised_search_needs_none(self):
        self.found(self.alice, "mémorisé", "movie")
        self.tmdb.hold = threading.Event()
        threads = [threading.Thread(target=call, args=(self.hub, "GET", f"/api/requests/search?q=lent{n}&kind=movie"), kwargs={"headers": self.headers(who)})
                   for n, who in enumerate((self.alice, self.bob))]
        self.addCleanup(self.tmdb.hold.set)
        for t in threads:
            t.start()
        self.assertTrue(self.tmdb.entered.acquire(timeout=LAG) and self.tmdb.entered.acquire(timeout=LAG))
        self.assertEqual(self.search(self.boss, "mémorisé", "movie").status, 200)
        self.assertEqual(self.search(self.boss, "pas en mémoire", "movie").status, 503)
        self.tmdb.hold.set()
        for t in threads:
            t.join(LAG)

    def test_the_places_come_back_after_a_failure_even_an_unexpected_one(self):
        for error in (TmdbError("coupé"), RecursionError("trop profond")):
            self.tmdb.error = error
            reply = self.search(self.alice, f"panne {type(error).__name__}", "movie")
            self.assertIn(reply.status, (500, 502))
        slots = self.hub.finder.slots
        self.assertTrue(slots.acquire(blocking=False) and slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))


class TestSeveralSessions(Together):
    def token_of(self, who):
        """Une session de plus : le jeton que `POST /api/token` rend à un client natif."""
        _, reply = call(self.hub, "POST", "/api/token", {"user": who["name"], "password": PASSWORD})
        self.assertEqual(reply.status, 200, reply.body)
        return {"Authorization": "Bearer " + reply.json()["token"]}

    def test_the_cookie_the_bearer_and_a_new_login_share_the_same_thirty(self):
        cookie, bearer = self.headers(self.alice), self.token_of(self.alice)
        asked = lambda headers, first: [call(self.hub, "GET", f"/api/requests/search?q=film%20{n}&kind=movie", headers=headers)[1].status for n in range(first, first + 10)]
        self.assertEqual(asked(cookie, 0), [200] * 10)
        self.assertEqual(asked(bearer, 10), [200] * 10)
        self.assertEqual(asked(self.token_of(self.alice), 20), [200] * 10)
        self.assertEqual((asked(cookie, 0)[0], asked(bearer, 0)[0]), (429, 429))
        self.assertEqual(self.search(self.bob, "film 0", "movie").status, 200)

    def test_signing_in_again_does_not_start_a_new_count(self):
        for n in range(30):
            self.assertEqual(self.search(self.alice, f"film {n}", "movie").status, 200)
        old = self.headers(self.alice)
        accounts.revoke_sessions(self.db, self.alice["id"])           # « déconnecter les autres appareils » : un nouveau jeton, le même compte
        _, again = call(self.hub, "GET", "/api/requests/search?q=film%200&kind=movie", headers=old)
        self.assertEqual(again.status, 401)
        for headers in (self.headers(self.alice), self.token_of(self.alice)):
            self.assertEqual(call(self.hub, "GET", "/api/requests/search?q=film%200&kind=movie", headers=headers)[1].status, 429)

    def test_three_sessions_of_one_account_searching_together_share_the_thirty(self):
        plans = [(self.headers(self.alice), 14), (self.token_of(self.alice), 14), (self.headers(self.alice), 14)]
        out = [status for statuses in self.together(plans) for status in statuses]
        self.assertEqual((out.count(200), out.count(429)), (30, 12))
        self.assertEqual(self.search(self.bob, "film 0", "movie").status, 200)


class Silent(RealClient):
    """Un TMDB qui échoue de toutes les façons que le client sait : chaque message porte l'adresse demandée, donc la clé."""
    FAILURES = {"TmdbError": lambda url: TmdbError(f"<urlopen error timed out> {url}"), "TmdbAuthError": lambda url: TmdbAuthError(f"clé TMDB refusée (HTTP 401) {url}"),
                "TimeoutError": lambda url: TimeoutError(f"délai dépassé {url}"), "OSError": lambda url: OSError(url), "ValueError": lambda url: ValueError(f"adresse invalide {url}"),
                "HTTPError": lambda url: HTTPError(url, 500, "Erreur", {}, None)}

    def clean(self, handler, reply, what=""):
        for secret in (KEY, "api_key"):
            self.assertNotIn(secret.encode(), reply.body, what)
            self.assertNotIn(secret, handler.stderr, what)

    def call(self, who, method, target, body=None):
        handler, reply = call(self.hub, method, target, body, self.headers(who))
        self.clean(handler, reply, f"{method} {target}")
        return handler, reply


class TestTheKeyStaysSecret(Silent):
    def test_no_answer_of_the_search_shows_it_whatever_its_status(self):
        for name, failure in self.FAILURES.items():
            with self.subTest(name), self.answer(error=failure):
                handler, reply = self.call(self.alice, "GET", f"/api/requests/search?q=panne%20{name}")
                self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))
        self.assertEqual(self.call(self.alice, "GET", "/api/requests/search?q=a")[1].status, 400)
        self.hub.finder.pace.LIMIT = 1
        self.assertEqual(self.call(self.alice, "GET", "/api/requests/search?q=un&kind=movie")[1].status, 429)       # la cadence est déjà entamée par les pannes
        self.hub.cfg.tmdb_key = ""
        self.assertEqual(self.call(self.bob, "GET", "/api/requests/search?q=sans%20cle")[1].status, 503)
        self.hub.cfg.tmdb_key = KEY
        self.post(self.boss, f"/api/user/{self.bob['id']}/request", {"request": False})
        self.assertEqual(self.call(self.bob, "GET", "/api/requests/search?q=interdit")[1].status, 403)

    def test_a_search_that_works_shows_nothing_of_it_either(self):
        with self.answer(b'{"results": [{"id": 3, "title": "Trois"}]}'):
            handler, reply = self.call(self.alice, "GET", "/api/requests/search?q=trois")
        self.assertEqual(reply.status, 200)
        self.assertEqual(handler.stderr, "")

    def test_no_answer_of_a_request_shows_it(self):
        for n, (name, failure) in enumerate(self.FAILURES.items()):
            with self.subTest(name), self.answer(error=failure):
                _, reply = self.call(self.alice, "POST", "/api/requests", {"kind": "show", "tmdb": 800 + n})
                self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))
        self.assertEqual(self.stored(), [])
        with self.answer(b'{"title": "Quatre-vingt-huit", "release_date": "1988-08-08"}'):
            handler, reply = self.call(self.alice, "POST", "/api/requests", {"kind": "movie", "tmdb": 88})
        self.assertEqual((reply.status, handler.stderr), (201, ""))
        for who, target in ((self.alice, "/api/requests?mine=1"), (self.boss, "/api/requests")):
            self.assertEqual(self.call(who, "GET", target)[1].status, 200)

    def test_the_state_the_lists_the_users_and_the_images_never_carry_it(self):
        self.hub.images.file.return_value = (b"jpeg", "image/jpeg", "cle")
        self.hub.cfg.tmdb_key = ""
        rid = self.ask(self.alice, 78, "show", title="Une série").json()["id"]
        self.hub.cfg.tmdb_key = KEY
        (self.hub.cfg.data / "tmdb").mkdir(exist_ok=True)
        (self.hub.cfg.data / "tmdb" / "78.jpg").write_bytes(b"\xff\xd8\xff")
        targets = ["/api/state", "/api/requests?mine=1", "/api/requests", "/api/home", "/api/users", f"/img/request/{rid}", "/img/request/999", "/api/requests/search?q=film",
                   "/api/requests/search?q=a", "/api/requests/search"]
        with self.answer():
            for who in (self.alice, self.boss, self.bob):
                for target in targets:
                    with self.subTest(who=who["name"], target=target):
                        self.call(who, "GET", target)
        state = self.get(self.alice, "/api/state").json()
        self.assertIs(state["tmdb"], True)                       # que le serveur en a une, jamais laquelle
        self.assertNotIn("api_key", json.dumps(state))


class TestTheJournalNeverHasTheKeyNorTheText(Silent):
    def test_the_text_is_hidden_under_every_spelling_of_the_parameter(self):
        targets = ("/api/requests/search?q=secretaire", "/api/requests/search?%71=secretaire", "/api/requests/search?q=un&q=secretaire", "/api/requests/search?kind=movie&q=secretaire&token=x",
                   "/api/requests/search?q=" + quote("secrétaire/ça&va"))
        for target in targets:
            with self.subTest(target=target), self.answer(error=self.FAILURES["TmdbError"]):
                handler, reply = self.call(self.alice, "GET", target)
                self.assertEqual(reply.status, 502)
                self.assertIn("502 GET /api/requests/search?", handler.stderr)
                for text in ("secretaire", "secr", "va"):
                    self.assertNotIn(text, handler.stderr)

    def test_the_text_is_hidden_when_the_path_is_percent_encoded(self):
        # Défaut corrigé : `dispatch` route d'après `unquote(split.path)`, mais `log_failure` compare `split.path` brut à « /api/requests/search » (app.py, log_failure) :
        # `/api/requests/%73earch?q=…` atteint la route et son échec écrit le texte cherché au journal.
        with self.answer(error=self.FAILURES["TmdbError"]):
            handler, reply = self.call(self.alice, "GET", "/api/requests/%73earch?q=secretaire")
        self.assertEqual(reply.status, 502)
        self.assertNotIn("secretaire", handler.stderr)

    def test_a_failure_of_the_client_logs_the_kind_of_failure_and_nothing_else(self):
        for name, failure in self.FAILURES.items():
            with self.subTest(name), self.answer(error=failure):
                handler, _ = self.call(self.alice, "GET", f"/api/requests/search?q=journal%20{name}&kind=movie")
                self.assertRegex(handler.stderr, r"^TMDB \(recherche des demandes\) : (pas de réponse|clé refusée)\n502 GET /api/requests/search\?q=\*\*\*&kind=movie : TMDB ne répond pas, réessaie plus tard\n$")


class TestPosterJobIsSilent(Silent):
    """La tâche « Affiches des demandes » : son journal est lu par l'administrateur (`GET /api/jobs?log=1`), son échec écrit sur stderr."""

    def setUp(self):
        super().setUp()
        self.hub.cfg.tmdb_key = ""                                # les demandes se font sans TMDB : seule la tâche appelle TMDB ici
        for n in range(3):
            self.ask(self.alice, 90 + n, "movie", title=f"Titre {n}")
        self.hub.cfg.tmdb_key = KEY
        self.hub.cfg.ffprobe = ""
        self.hub.jobs = jobs.Jobs(self.hub.cfg, self.db)

    def run_job(self):
        job = self.hub.jobs.request_posters()
        deadline = time.monotonic() + LAG
        while job.status in ("waiting", "running") and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertIn(job.status, ("done", "failed"))
        return job

    def seen(self, job):
        """Tout ce qu'un compte peut lire de la tâche : sa description, son journal, la route des tâches."""
        reply = self.get(self.boss, "/api/jobs?log=1")
        self.assertEqual(reply.status, 200)
        return json.dumps(job.public(with_log=True)) + reply.body.decode()

    def test_a_failing_tmdb_leaves_the_key_out_of_the_log_the_result_and_the_route(self):
        for name, failure in self.FAILURES.items():
            with self.subTest(name), self.answer(error=failure):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    job = self.run_job()
                    seen = self.seen(job)
                self.assertNotIn(KEY, seen)
                self.assertNotIn("api_key", seen)
                self.assertNotIn(KEY, err.getvalue())
                if name == "TmdbAuthError":
                    self.assertEqual((job.status, list(job.log)[-1]), ("failed", "ERREUR : clé TMDB refusée par l'API"))
                else:
                    self.assertEqual((job.status, job.result), ("done", {"saved": 0, "failed": 3, "without": 0}))

    def test_a_poster_that_arrives_logs_no_address(self):
        body = json.dumps(movie(90, poster_path="/m90.jpg")).encode()
        with self.answer(body), mock.patch.object(tmdb_module.Tmdb, "save_image", lambda self_, path, size, dest: dest.write_bytes(b"\xff\xd8\xff")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                job = self.run_job()
                seen = self.seen(job)
        self.assertEqual(job.status, "done")
        self.assertNotIn(KEY, seen + err.getvalue())
        self.assertNotIn("themoviedb", seen + err.getvalue())


class TestUnexpectedFailures(Silent):
    """Ce que TMDB ou le client ne devraient pas faire, mais que le serveur doit tenir : une réponse hostile n'est pas une panne du serveur, et rien ne fuit."""

    def test_an_answer_nested_too_deep_is_a_502_not_a_500(self):
        # Défaut corrigé : `json.loads` d'une réponse imbriquée sur des dizaines de milliers de niveaux lève RecursionError dans Tmdb.get (media_library) ; `request_search._asked` ne rattrape que
        # TmdbError, TmdbAuthError, OSError et ValueError (request_search.py, _asked) : la recherche rend 500 « erreur interne » et une trace au journal au lieu du 502 du contrat.
        with self.answer(b"[" * 100_000):
            handler, reply = self.call(self.alice, "GET", "/api/requests/search?q=profond&kind=movie")
        self.assertEqual((reply.status, reply.json()), (502, UNREACHABLE))

    def test_an_answer_nested_too_deep_does_not_stop_the_poster_job(self):
        # Défaut corrigé : même cause dans `fetch_posters` (request_search.py) : la RecursionError n'est pas rattrapée par `except (TmdbError, OSError, ValueError)`, la tâche entière échoue
        # au lieu de compter un échec pour cette affiche seulement (« un échec n'arrête que son affiche »).
        self.hub.cfg.tmdb_key = ""
        self.ask(self.alice, 90, "movie", title="Titre")
        self.hub.cfg.tmdb_key = KEY
        log = []
        with self.answer(b"[" * 100_000):
            result = request_search.fetch_posters(self.hub.cfg, self.db, log.append)
        self.assertEqual(result, {"saved": 0, "failed": 1, "without": 0})

    def test_an_unforeseen_exception_carrying_the_address_does_not_leak_the_key(self):
        # Défaut corrigé : `_asked` est une liste blanche d'exceptions (request_search.py) ; toute autre est une erreur 500 dont le message (« erreur interne : … ») part dans la réponse
        # et au journal tel quel. Ici le message porte l'adresse demandée, donc `api_key=<clé>` : la clé sort dans la réponse et dans stderr.
        def boom(url, *rest, **more):
            raise RuntimeError(f"incident sur {url}")
        with mock.patch.object(tmdb_module.http, "fetch", boom):
            handler, reply = call(self.hub, "GET", "/api/requests/search?q=inattendu&kind=movie", headers=self.headers(self.alice))
        self.assertNotIn(KEY.encode(), reply.body)
        self.assertNotIn(KEY, handler.stderr)

    def test_an_unforeseen_exception_carrying_the_address_does_not_leak_the_key_in_the_job_log(self):
        # Défaut corrigé : même cause dans la tâche : `Jobs._run` écrit « ERREUR : {e} » dans le journal que l'administrateur lit (jobs.py, _run) ; une exception hors de la liste de
        # `fetch_posters` y porte l'adresse, donc la clé.
        self.hub.cfg.tmdb_key = ""
        self.ask(self.alice, 90, "movie", title="Titre")
        self.hub.cfg.tmdb_key = KEY
        self.hub.cfg.ffprobe = ""

        def boom(url, *rest, **more):
            raise RuntimeError(f"incident sur {url}")
        queue = jobs.Jobs(self.hub.cfg, self.db)
        with mock.patch.object(tmdb_module.http, "fetch", boom):
            job = queue.request_posters()
            deadline = time.monotonic() + LAG
            while job.status in ("waiting", "running") and time.monotonic() < deadline:
                threading.Event().wait(0.01)
        self.assertNotIn(KEY, "\n".join(job.log))


class TestTheLastResortsHideTheKey(Silent):
    """Les trois ceintures qui retirent la clé TMDB d'un message qui n'a rien à voir avec la recherche (`Jobs._run`, `Hub.scrub`, `log_failure`) : ni la route de recherche ni la tâche des affiches
    n'y mènent (elles rattrapent tout avant), donc on y arrive avec une tâche et une route qui échouent à leur façon."""

    def setUp(self):
        super().setUp()
        self.hub.cfg.ffprobe = ""

    def lines(self, handler):
        return handler.stderr.splitlines()

    def test_a_job_that_fails_with_the_address_leaves_the_key_out_of_its_log(self):
        def boom(log):
            raise RuntimeError(f"incident sur https://api.themoviedb.org/3/search/movie?api_key={KEY}")
        self.hub.jobs = queue = jobs.Jobs(self.hub.cfg, self.db)
        job = queue._submit("Tâche en panne", boom)
        deadline = time.monotonic() + LAG
        while job.status in ("waiting", "running") and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual((job.status, list(job.log)[-1].startswith("ERREUR : incident sur https://api.themoviedb.org/")), ("failed", True))
        reply = self.get(self.boss, "/api/jobs?log=1")
        for seen in ("\n".join(job.log), reply.body.decode()):
            self.assertNotIn(KEY, seen)

    def test_a_refusal_with_the_address_hides_the_key_in_the_answer_and_in_the_journal(self):
        # Une 502 levée par une route : `_fail` passe le message à `Hub.scrub` (la réponse), puis `log_failure` l'écrit sur stderr ; chacune des deux ceintures retire la clé.
        self.hub.jobs.snapshot.side_effect = HttpError(502, f"TMDB a répondu sur https://api.themoviedb.org/3/movie/1?api_key={KEY}")
        handler, reply = call(self.hub, "GET", "/api/jobs", headers=self.headers(self.boss))
        self.assertEqual(reply.status, 502)
        self.assertIn("https://api.themoviedb.org/3/movie/1?api_key=", reply.json()["error"])
        self.assertNotIn(KEY.encode(), reply.body)
        self.assertRegex(self.lines(handler)[-1], r"^502 GET /api/jobs : TMDB a répondu sur ")
        self.assertNotIn(KEY, handler.stderr)

    def test_an_internal_error_with_the_address_hides_the_key_in_the_answer_and_in_the_log_line(self):
        self.hub.jobs.snapshot.side_effect = RuntimeError(f"incident sur https://api.themoviedb.org/3/movie/1?api_key={KEY}")
        handler, reply = call(self.hub, "GET", "/api/jobs", headers=self.headers(self.boss))
        self.assertEqual(reply.status, 500)
        self.assertIn("erreur interne : incident sur", reply.json()["error"])
        self.assertNotIn(KEY.encode(), reply.body)
        self.assertRegex(self.lines(handler)[-1], r"^500 GET /api/jobs : erreur interne : incident sur ")
        self.assertNotIn(KEY, self.lines(handler)[-1])


if __name__ == "__main__":
    unittest.main()
