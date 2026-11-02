"""Le gestionnaire HTTP, sans réseau : réponses, fichiers par morceaux, aiguillage, erreurs, corps des requêtes, site, connexion."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from tests.rig import MiniHub, Rig, call
from hub import accounts
from hub.server import app
from hub.server.app import HttpError

PASSWORD = "un-mot-de-passe-ok"


class RootCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        (self.root / "web" / "index.html").write_text("<html>accueil</html>", encoding="utf-8")
        self.hub = MiniHub(self.root)

    def rig(self, method="GET", target="/", body=b"", headers=None):
        return Rig(self.hub, method, target, body, headers)


class TestSend(RootCase):
    def test_json_keeps_the_accents_and_is_not_cached(self):
        rig = self.rig()
        rig.send_json({"titre": "Éléphant"}, 201, {"X-Test": "1"})
        reply = rig.reply()
        self.assertEqual((reply.status, reply.body, reply.headers["x-test"], reply.headers["cache-control"]), (201, '{"titre":"Éléphant"}'.encode("utf-8"), "1", "no-store"))
        self.assertEqual(reply.headers["content-length"], str(len(reply.body)))
        self.assertEqual(reply.headers["content-type"], "application/json; charset=utf-8")

    def test_a_head_request_gets_the_headers_and_no_body(self):
        rig = self.rig("HEAD")
        rig.send_json({"a": 1})
        reply = rig.reply()
        self.assertEqual((reply.body, reply.headers["content-length"]), (b"", "7"))

    def test_a_body_is_sent_with_its_type_and_size(self):
        rig = self.rig()
        rig.send_body(b"abc", "text/plain", 202, {"X-Un": "deux"})
        reply = rig.reply()
        self.assertEqual((reply.status, reply.body, reply.headers["content-type"], reply.headers["content-length"], reply.headers["x-un"]), (202, b"abc", "text/plain", "3", "deux"))


class TestSendFile(RootCase):
    def setUp(self):
        super().setUp()
        self.data = b"0123456789"

    def send(self, name="clip.txt", range_=None, method="GET", **kw):
        path = self.root / name
        path.write_bytes(self.data)
        rig = self.rig(method, headers={"Range": range_} if range_ else None)
        rig.send_file(path, **kw)
        return rig, rig.reply()

    def test_the_whole_file_is_sent_with_its_type(self):
        rig, reply = self.send()
        self.assertEqual((reply.status, reply.body, reply.headers["content-type"], reply.headers["content-length"], reply.headers["accept-ranges"]), (200, self.data, "text/plain", "10", "bytes"))
        self.assertNotIn("content-range", reply.headers)

    def test_the_type_is_guessed_from_the_name_and_an_unknown_one_is_bytes(self):
        self.assertEqual(self.send("a.mp4")[1].headers["content-type"], "video/mp4")
        self.assertEqual(self.send("a.inconnu")[1].headers["content-type"], "application/octet-stream")
        self.assertEqual(self.send("a.tar.gz")[1].headers["content-type"], "application/x-tar")

    def test_a_given_type_wins(self):
        self.assertEqual(self.send(mime="video/x-matroska")[1].headers["content-type"], "video/x-matroska")

    def test_extra_headers_are_added(self):
        self.assertEqual(self.send(headers={"Cache-Control": "private"})[1].headers["cache-control"], "private")
        self.assertNotIn("cache-control", self.send(headers=None)[1].headers)

    def test_a_part_is_sent_with_exactly_the_bytes_asked(self):
        _, reply = self.send(range_="bytes=2-5")
        self.assertEqual((reply.status, reply.body, reply.headers["content-length"], reply.headers["content-range"]), (206, b"2345", "4", "bytes 2-5/10"))
        _, reply = self.send(range_="bytes=3-3")
        self.assertEqual((reply.body, reply.headers["content-length"]), (b"3", "1"))
        _, reply = self.send(range_="bytes=-3")
        self.assertEqual((reply.body, reply.headers["content-range"]), (b"789", "bytes 7-9/10"))

    def test_the_file_goes_out_by_pieces(self):
        with mock.patch.object(app, "CHUNK", 3):
            _, reply = self.send(range_="bytes=1-8")
            self.assertEqual(reply.body, b"12345678")
            _, reply = self.send()
            self.assertEqual(reply.body, self.data)

    def test_the_connection_waits_a_long_time_during_the_send_then_goes_back_to_normal(self):
        rig, _ = self.send()
        self.assertEqual(rig.connection.settimeout.call_args_list, [mock.call(3600), mock.call(rig.timeout)])

    def test_a_head_request_sends_no_byte_of_the_file(self):
        _, reply = self.send(method="HEAD", range_="bytes=2-5")
        self.assertEqual((reply.status, reply.body, reply.headers["content-length"]), (206, b"", "4"))

    def test_a_part_outside_the_file_is_a_416(self):
        _, reply = self.send(range_="bytes=10-")
        self.assertEqual((reply.status, reply.body, reply.headers["content-range"]), (416, b"", "bytes */10"))

    def test_a_missing_file_is_a_404(self):
        with self.assertRaises(HttpError) as caught:
            self.rig().send_file(self.root / "absent.mkv")
        self.assertEqual((caught.exception.status, caught.exception.message), (404, "fichier introuvable"))

    def test_a_file_shorter_than_announced_ends_the_send(self):
        path = self.root / "court.bin"
        path.write_bytes(b"abc")
        rig = self.rig()
        rig.copy_range(open(path, "rb"), 0, 100)
        self.assertEqual(rig.wfile.getvalue(), b"abc")


class TestDispatch(RootCase):
    def test_an_unknown_api_path_is_a_404_and_an_unknown_page_is_the_home_page(self):
        _, reply = call(self.hub, "GET", "/api/nope")
        self.assertEqual((reply.status, reply.json()), (404, {"error": "introuvable"}))
        _, reply = call(self.hub, "GET", "/une/page")
        self.assertEqual((reply.status, reply.body), (200, b"<html>accueil</html>"))
        for path in ("/media/inconnu", "/img/inconnu", "/book/inconnu"):
            self.assertEqual(call(self.hub, "GET", path)[1].status, 404, path)

    def test_only_get_and_head_serve_the_site(self):
        self.assertEqual(call(self.hub, "POST", "/une/page", {})[1].status, 404)
        self.assertEqual(call(self.hub, "HEAD", "/une/page")[1].body, b"")

    def test_head_answers_the_get_routes(self):
        _, reply = call(self.hub, "HEAD", "/api/state")
        self.assertEqual((reply.status, reply.body), (200, b""))

    def test_the_query_is_read_with_the_last_value_winning(self):
        _, reply = call(self.hub, "GET", "/api/search?q=aa&q=zz")
        self.assertEqual(reply.json(), {"results": []})

    def test_a_failing_route_is_a_500_that_does_not_stop_the_server(self):
        def boom(h, m, q):
            raise ValueError("oups")

        rig, reply = call(self.hub, "GET", "/boom", rig=type("R", (Rig,), {"__init__": lambda self, *a: (Rig.__init__(self, *a), setattr(self, "routes", [("GET", re.compile("/boom"), boom)]))[0]}))
        self.assertEqual((reply.status, reply.json()), (500, {"error": "erreur interne : oups"}))
        self.assertIn("ValueError: oups", rig.stderr)
        self.assertIn("500 GET /boom : erreur interne : oups", rig.stderr)

    def test_a_reader_who_left_is_not_an_error(self):
        def gone(h, m, q):
            raise BrokenPipeError()

        rig = self.rig("GET", "/gone")
        rig.routes = [("GET", re.compile("/gone"), gone)]
        rig.dispatch()
        self.assertTrue(rig.close_connection)
        self.assertIsNone(rig.reply())

    def test_the_request_closes_its_body_even_when_the_route_does_not_read_it(self):
        rig, reply = call(self.hub, "POST", "/api/nope", b"x" * 20)
        self.assertEqual(reply.status, 404)
        self.assertEqual(rig.rfile.tell(), 20)


class TestAccess(RootCase):
    def setUp(self):
        super().setUp()
        self.alice = accounts.create(self.hub.db, "alice", PASSWORD)
        self.cookie = self.hub.cookie(self.alice)

    def test_without_a_session_the_api_is_a_401_and_a_page_goes_to_the_login(self):
        _, reply = call(self.hub, "GET", "/api/state")
        self.assertEqual((reply.status, reply.json()), (401, {"error": "connexion requise"}))
        _, reply = call(self.hub, "GET", "/")
        self.assertEqual((reply.status, reply.headers["location"]), (302, "/login"))
        _, reply = call(self.hub, "GET", "/img/item/x")
        self.assertEqual((reply.status, reply.headers["location"]), (302, "/login"))

    def test_the_login_pages_stay_open(self):
        for path in ("/login", "/favicon.svg", "/icon.svg"):
            self.assertNotEqual(call(self.hub, "GET", path)[1].status, 401, path)

    def test_a_session_cookie_or_a_bearer_token_opens_the_door(self):
        self.assertEqual(call(self.hub, "GET", "/api/state", headers=self.cookie)[1].status, 200)
        token = self.hub.session_token(self.alice)
        self.assertEqual(call(self.hub, "GET", "/api/state", headers={"Authorization": f"Bearer {token}"})[1].status, 200)
        self.assertEqual(call(self.hub, "GET", "/api/state", headers={"Authorization": f"bearer  {token}"})[1].status, 200)
        self.assertEqual(call(self.hub, "GET", "/api/state", headers={"Cookie": f"autre=1; hub={token}; x=2"})[1].status, 200)

    def test_a_forged_session_is_refused(self):
        token = self.hub.session_token(self.alice)
        for headers in ({"Cookie": f"hub={token}junk"}, {"Cookie": f"hub={token[:-1]}0"}, {"Authorization": f"Basic {token}"}, {"Authorization": "Bearer"}, {"Cookie": f"xhub={token}"}):
            self.assertEqual(call(self.hub, "GET", "/api/state", headers=headers)[1].status, 401, headers)

    def test_the_media_token_only_opens_the_files(self):
        self.hub.images.get.return_value = None
        token = self.hub.media_token()
        for path in ("/img/item/x", "/media/x/file", "/book/x/page/0"):
            self.assertEqual(call(self.hub, "GET", f"{path}?token={token}")[1].status, 404, path)
            self.assertEqual(call(self.hub, "GET", f"{path}?token=faux")[1].status, 302, path)
        self.assertEqual(call(self.hub, "GET", f"/api/state?token={token}")[1].status, 401)
        self.assertEqual(call(self.hub, "GET", f"/api/subs/x/e0.vtt?token={token}")[1].status, 404)

    def test_the_user_of_the_request_is_the_account_of_the_session(self):
        rig, _ = call(self.hub, "GET", "/api/state", headers=self.cookie)
        self.assertEqual(rig.user, accounts.public(self.alice))


class TestFail(RootCase):
    def fail(self, status, target="/x", method="GET", **attrs):
        rig = self.rig(method, target, body=b"abc" if method == "POST" else b"")
        for key, value in attrs.items():
            setattr(rig, key, value)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rig._fail(status, "message")
        return rig, err.getvalue()

    def test_a_server_error_is_written_to_the_journal_and_a_client_error_is_not(self):
        self.assertEqual(self.fail(500)[1], "500 GET /x : message\n")
        self.assertEqual(self.fail(502)[1], "502 GET /x : message\n")
        for status in (400, 404, 501):
            self.assertEqual(self.fail(status)[1], "", status)

    def test_the_token_is_never_written_in_the_journal(self):
        for target in ("/media/a/file?token=SECRET&n=1", "/media/a/file?%74oken=SECRET&n=1", "/media/a/file?n=1&token=SECRET"):
            line = self.fail(500, target)[1]
            self.assertNotIn("SECRET", line, target)
            self.assertIn("token=***", line)
            self.assertIn("n=1", line)

    def test_an_empty_value_stays_in_the_journal(self):
        self.assertEqual(self.fail(500, "/x?a=&b=1")[1], "500 GET /x?a=&b=1 : message\n")

    def test_the_message_is_sent_without_the_paths_of_the_server(self):
        rig = self.rig()
        with contextlib.redirect_stderr(io.StringIO()):
            rig._fail(404, f"pas de {self.hub.cfg.data}/x")
        self.assertEqual(rig.reply().json(), {"error": "pas de …/x"})

    def test_a_connection_that_cannot_be_written_is_closed(self):
        rig, _ = self.fail(500, send_json=mock.Mock(side_effect=OSError))
        self.assertTrue(rig.close_connection)
        self.assertFalse(self.fail(500)[0].close_connection)

    def test_the_body_of_a_refused_post_is_thrown_away_and_a_failure_to_do_so_closes_the_connection(self):
        rig, _ = self.fail(404, method="POST")
        self.assertEqual(rig.rfile.tell(), 3)
        rig, _ = self.fail(404, method="POST", body_taken=True)
        self.assertEqual(rig.rfile.tell(), 0)
        rig, _ = self.fail(404, method="POST", discard_body=mock.Mock(side_effect=OSError))
        self.assertTrue(rig.close_connection)
        self.assertFalse(self.fail(404, method="POST")[0].close_connection)


class TestBodies(RootCase):
    def rig_with(self, body, length=None):
        return self.rig("POST", "/x", body, {"Content-Length": str(len(body) if length is None else length)})

    def test_a_json_object_is_read_and_anything_else_is_an_empty_one(self):
        self.assertEqual(self.rig_with(b'{"a": "\xc3\xa9"}').body_json(), {"a": "é"})
        for body in (b"[1, 2]", b"12", b"du bruit", b"", b'"texte"', b"\xff\xfe"):
            self.assertEqual(self.rig_with(body).body_json(), {}, body)

    def test_the_body_is_marked_as_read(self):
        rig = self.rig_with(b"{}")
        rig.body_json()
        self.assertTrue(rig.body_taken)

    def test_a_body_of_exactly_one_megabyte_is_read_and_a_byte_more_is_refused(self):
        limit = 1 << 20
        text = b'{"a": "' + b"x" * (limit - 9) + b'"}'
        self.assertEqual(len(text), limit)
        rig = self.rig_with(text)
        self.assertEqual(len(rig.body_json()["a"]), limit - 9)
        self.assertFalse(rig.close_connection)
        rig = self.rig_with(b"x", length=limit + 1)
        with self.assertRaises(HttpError) as caught:
            rig.body_json()
        self.assertEqual((caught.exception.status, caught.exception.message), (413, "corps trop gros"))
        self.assertTrue(rig.close_connection)
        self.assertTrue(rig.body_taken)

    def test_a_refused_body_is_read_and_thrown_away_up_to_one_megabyte(self):
        limit = 1 << 20
        rig = self.rig_with(b"x" * limit)
        rig.discard_body()
        self.assertEqual((rig.rfile.tell(), rig.close_connection, rig.body_taken), (limit, False, True))
        rig = self.rig_with(b"x" * (limit + 1))
        rig.discard_body()
        self.assertEqual((rig.rfile.tell(), rig.close_connection), (0, True))

    def test_a_body_that_is_not_announced_is_left_alone(self):
        rig = self.rig("POST", "/x", b"x", {"Content-Length": "0"})
        rig.discard_body()
        self.assertEqual((rig.rfile.tell(), rig.close_connection), (0, False))
        rig = self.rig("POST", "/x", b"x")
        del rig.headers["Content-Length"]
        rig.discard_body()
        self.assertEqual((rig.rfile.tell(), rig.close_connection), (0, False))

    def test_an_unreadable_or_negative_length_cuts_the_connection(self):
        for value in ("abc", "-1"):
            rig = self.rig("POST", "/x", b"x", {"Content-Length": value})
            rig.discard_body()
            self.assertEqual((rig.rfile.tell(), rig.close_connection), (0, True), value)

    def test_a_file_is_read_up_to_its_limit(self):
        rig = self.rig_with(b"x" * 10)
        self.assertEqual(rig.body_bytes(10), b"x" * 10)
        self.assertFalse(rig.close_connection)
        self.assertTrue(rig.body_taken)
        rig = self.rig_with(b"x" * 11)
        with self.assertRaises(HttpError) as caught:
            rig.body_bytes(10)
        self.assertEqual(caught.exception.status, 413)
        self.assertTrue(rig.close_connection)
        self.assertTrue(rig.body_taken)


class TestHandle(RootCase):
    def test_a_reader_who_leaves_in_the_middle_closes_the_connection_quietly(self):
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            rig = self.rig()
            with mock.patch.object(BaseHTTPRequestHandler, "handle", side_effect=error):
                rig.handle()
            self.assertTrue(rig.close_connection, error)

    def test_any_other_error_goes_up(self):
        with mock.patch.object(BaseHTTPRequestHandler, "handle", side_effect=ValueError), self.assertRaises(ValueError):
            self.rig().handle()


class TestStatic(RootCase):
    def setUp(self):
        super().setUp()
        web = self.root / "web"
        for name, data in (("app.js", b"x=1"), ("data.json", b"{}"), ("logo.svg", b"<svg/>"), ("pic.png", b"png"), ("raw.zzz", b"zz"), ("style.css", b"a{}")):
            (web / name).write_bytes(data)

    def get(self, path, headers=None, method="GET"):
        rig = self.rig(method, path, headers=headers)
        rig.static(path)
        return rig.reply()

    def test_the_text_files_say_their_encoding_and_the_others_do_not(self):
        types_ = {name: self.get("/" + name).headers["content-type"] for name in ("index.html", "app.js", "data.json", "logo.svg", "style.css", "pic.png", "raw.zzz")}
        self.assertEqual(types_["index.html"], "text/html; charset=utf-8")
        self.assertEqual(types_["style.css"], "text/css; charset=utf-8")
        self.assertEqual(types_["data.json"], "application/json; charset=utf-8")
        self.assertEqual(types_["logo.svg"], "image/svg+xml; charset=utf-8")
        self.assertTrue(types_["app.js"].endswith("; charset=utf-8"))
        self.assertEqual(types_["pic.png"], "image/png")
        self.assertEqual(types_["raw.zzz"], "application/octet-stream")

    def test_the_file_is_revalidated_by_its_stamp(self):
        first = self.get("/app.js")
        stamp = first.headers["etag"]
        self.assertEqual((first.status, first.body, first.headers["cache-control"]), (200, b"x=1", "no-cache"))
        again = self.get("/app.js", headers={"If-None-Match": stamp})
        self.assertEqual((again.status, again.body, again.headers["etag"]), (304, b"", stamp))
        self.assertEqual(self.get("/app.js", headers={"If-None-Match": '"autre"'}).status, 200)

    def test_a_head_request_gets_no_body(self):
        self.assertEqual(self.get("/app.js", method="HEAD").body, b"")


class TestLogin(RootCase):
    def setUp(self):
        super().setUp()
        self.alice = accounts.create(self.hub.db, "alice", PASSWORD)
        patcher = mock.patch.object(app.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def form(self, user, password):
        return call(self.hub, "POST", "/api/login", f"user={user}&password={password}".encode(), {"Content-Type": "application/x-www-form-urlencoded"})[1]

    def test_the_right_password_gives_a_session_cookie_and_goes_home(self):
        reply = self.form("alice", PASSWORD)
        self.assertEqual((reply.status, reply.headers["location"]), (303, "/"))
        self.assertEqual(reply.headers["set-cookie"], f"hub={self.hub.session_token(self.alice)}; Path=/; Max-Age={90 * 86400}; HttpOnly; SameSite=Lax")
        self.sleep.assert_called_once_with(accounts.Throttle.BASE)

    def test_the_wrong_password_goes_back_to_the_login_and_slows_the_next_try(self):
        reply = self.form("alice", "faux")
        self.assertEqual((reply.status, reply.headers["location"]), (303, "/login?error=1"))
        self.assertNotIn("set-cookie", reply.headers)
        for _ in range(4):
            self.form("alice", "faux")
        self.assertGreater(self.sleep.call_args.args[0], accounts.Throttle.BASE)

    def test_a_good_try_starts_the_count_again(self):
        for _ in range(4):
            self.form("alice", "faux")
        self.form("alice", PASSWORD)
        self.form("alice", "faux")
        self.assertEqual(self.sleep.call_args.args[0], accounts.Throttle.BASE)

    def test_too_many_wrong_tries_stop_everything(self):
        for _ in range(accounts.Throttle.LIMIT):
            self.form("alice", "faux")
        calls = self.sleep.call_count
        reply = self.form("alice", PASSWORD)
        self.assertEqual((reply.status, reply.headers["location"]), (303, "/login?error=2"))
        self.assertEqual(self.sleep.call_count, calls)

    def test_without_any_account_nobody_waits(self):
        hub = MiniHub(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, hub.cfg.data.parent, True)
        reply = call(hub, "POST", "/api/login", b"user=x&password=y", {"Content-Type": "application/x-www-form-urlencoded"})[1]
        self.assertEqual(reply.status, 303)
        self.sleep.assert_called_once_with(0)


class TestToken(RootCase):
    def setUp(self):
        super().setUp()
        self.alice = accounts.create(self.hub.db, "alice", PASSWORD)
        patcher = mock.patch.object(app.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, body, headers=None, hub=None):
        return call(hub or self.hub, "POST", "/api/token", body, headers)

    def test_the_right_password_gives_the_two_tokens(self):
        _, reply = self.ask({"user": "alice", "password": PASSWORD})
        out = reply.json()
        self.assertEqual((reply.status, out["token"], out["media_token"], out["user"]), (200, self.hub.session_token(self.alice), self.hub.media_token(), accounts.public(self.alice)))

    def test_a_password_that_is_not_a_text_or_a_name_that_is_not_a_text_counts_as_a_failure(self):
        for body in ({"user": "alice", "password": 1234}, {"user": 5, "password": PASSWORD}, {"user": ["alice"], "password": PASSWORD}, {"user": "alice"}, [1], {}):
            with mock.patch.object(self.hub.throttle, "failed") as failed:
                _, reply = self.ask(body)
            self.assertEqual((reply.status, reply.json()), (401, {"error": "mot de passe incorrect"}), body)
            failed.assert_called_once()

    def test_a_body_without_a_usable_password_gets_the_same_answer_whoever_is_named(self):
        accounts.create(self.hub.db, "bob", PASSWORD)
        for body in ({"password": 1234}, {}, {"user": 5, "password": PASSWORD}):
            _, reply = self.ask(body)
            self.assertEqual((reply.status, reply.json()), (401, {"error": "mot de passe incorrect"}), body)

    def test_the_name_may_be_left_out(self):
        self.assertEqual(self.ask({"password": PASSWORD})[1].status, 200)

    def test_a_wrong_password_is_refused_and_counted(self):
        with mock.patch.object(self.hub.throttle, "failed") as failed:
            _, reply = self.ask({"user": "alice", "password": "faux"})
        self.assertEqual(reply.status, 401)
        failed.assert_called_once()

    def test_too_many_tries_stop_everything(self):
        for _ in range(accounts.Throttle.LIMIT):
            self.ask({"user": "alice", "password": "faux"})
        self.assertEqual(self.ask({"user": "alice", "password": PASSWORD})[1].status, 429)

    def test_a_body_of_four_kilobytes_is_read_and_a_byte_more_is_refused(self):
        head = json.dumps({"user": "alice", "password": PASSWORD}).encode()
        exact = head + b" " * (4096 - len(head))
        rig, reply = self.ask(exact)
        self.assertEqual((reply.status, rig.close_connection), (200, False))
        rig, reply = self.ask(exact + b" ")
        self.assertEqual((reply.status, reply.json(), rig.close_connection), (413, {"error": "corps trop gros"}, True))

    def test_without_account_no_token_is_asked(self):
        hub = MiniHub(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, hub.cfg.data.parent, True)
        rig, reply = self.ask({"password": "x"}, hub=hub)
        self.assertEqual((reply.json(), rig.rfile.tell()), ({"token": "", "media_token": ""}, len(json.dumps({"password": "x"}))))


class TestCheckCurrent(RootCase):
    def setUp(self):
        super().setUp()
        self.alice = accounts.create(self.hub.db, "alice", PASSWORD)
        patcher = mock.patch.object(app.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, current, user_id=None):
        rig = self.rig()
        rig.user = {"id": user_id or self.alice["id"]}
        return app.check_current(rig, current)

    def test_the_right_password_gives_the_account_after_a_pause(self):
        self.assertEqual(self.check(PASSWORD)["id"], self.alice["id"])
        self.sleep.assert_called_once_with(0.4)

    def test_a_wrong_or_unreadable_password_or_a_vanished_account_is_a_400(self):
        for current, user in (("faux", None), (1234, None), (None, None), (PASSWORD, "disparu")):
            with self.assertRaises(HttpError) as caught:
                self.check(current, user)
            self.assertEqual((caught.exception.status, caught.exception.message), (400, "mot de passe actuel incorrect"), (current, user))


if __name__ == "__main__":
    unittest.main()
