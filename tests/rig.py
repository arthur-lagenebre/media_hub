"""De quoi appeler le gestionnaire HTTP sans réseau : une requête en mémoire, la réponse relue telle qu'elle serait écrite sur la connexion."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
import types
from unittest import mock

from tests import fixtures
from hub import accounts, config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.db import Database  # noqa: E402
from hub.sagas import Sagas  # noqa: E402
from hub.server import app  # noqa: E402


class Reply:
    """Ce que le gestionnaire a écrit : statut, en-têtes (clés en minuscules) et corps."""

    def __init__(self, raw):
        head, _, self.body = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.pairs = [(key.lower(), value) for key, _, value in (line.partition(": ") for line in lines[1:])]
        self.headers = dict(self.pairs)

    def json(self):
        return json.loads(self.body)


class MiniHub(app.Hub):
    """Le Hub sans ses fils ni ses images : une vraie base, les vraies méthodes, rien d'autre."""

    def __init__(self, root, libraries=(), **cfg):
        self.db = Database(root / "hub.db")
        data = root / "data"
        data.mkdir(exist_ok=True)
        self.cfg = types.SimpleNamespace(data=data, libraries=list(libraries), web=root / "web", max_streams=2, ffmpeg="", ffprobe="", tmdb_key="", repo=None, admin="", **cfg)
        self.cfg.library = lambda ident: next((lib for lib in self.cfg.libraries if lib.id == ident), None)
        self.images, self.jobs = mock.MagicMock(), mock.MagicMock()
        self.jobs.busy.return_value = False
        self.images.has_poster.return_value = False
        self.sagas = Sagas(data / "sagas.json")
        self.throttle = accounts.Throttle()
        self.streams = threading.BoundedSemaphore(2)
        self.active = 0
        self.secret = "ab" * 32
        self.started = 0.0

    def cookie(self, row):
        return {"Cookie": f"hub={self.session_token(row)}"}


class Rig(app.Handler):
    """Un gestionnaire sans connexion : la requête donnée, la réponse dans `wfile`."""

    def __init__(self, hub, method="GET", target="/", body=b"", headers=None):
        self.hub = hub
        self.command, self.path = method, target
        self.request_version, self.requestline = "HTTP/1.1", f"{method} {target} HTTP/1.1"
        self.client_address = ("127.0.0.1", 50000)
        message = http.client.HTTPMessage()
        sent = {"Content-Length": str(len(body))} if body or method == "POST" else {}
        for key, value in {**sent, **(headers or {})}.items():
            message[key] = value
        self.headers = message
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self.connection = mock.MagicMock()
        self.close_connection = False
        self.routes = app.ROUTES
        self.user = None
        self.body_taken = False
        self.stderr = ""

    def reply(self):
        raw = self.wfile.getvalue()
        return Reply(raw) if raw else None


def call(hub, method, target, body=None, headers=None, rig=Rig):
    """(le gestionnaire, la réponse) d'une requête passée par `dispatch`, avec le journal d'erreurs relevé."""
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    handler = rig(hub, method, target, data, headers)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        handler.dispatch()
    handler.stderr = err.getvalue()
    return handler, handler.reply()
