"""Ce que le hub fait en ce moment pour qui (lectures, téléchargements, envois, sauvegarde), et la trace des dernières décisions de lecture : de quoi savoir si on peut relancer le serveur sans couper quelqu'un.

Tout est en mémoire, sous un verrou (un fil par connexion) : ni table ni migration, et rien ne survit au redémarrage. L'horloge s'injecte pour les tests.

Un transfert est une ligne par (compte, élément transféré, nature, adresse du client) : la lecture directe d'un film se fait en nombreuses requêtes Range courtes, qui ne font qu'une ligne.
L'adresse sert à séparer deux appareils d'un même compte ; elle n'est jamais rendue (`origin` en dit seulement `local` ou `remote`).
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial

ACTIVE_SECONDS = 60             # une ligne reste tant qu'une connexion est ouverte, puis ce temps après sa dernière activité (le navigateur lit un moment depuis sa mémoire tampon sans rien demander)
DECISIONS_KEPT = 100            # les décisions de lecture gardées, les plus récentes
KINDS = ("direct", "audio", "remux", "transcode", "download", "upload", "backup")
PLAYING = ("direct", "audio", "remux", "transcode")
DEVICES = ("pc", "phone", "tablet", "tv")
TITLE_MAX = 200
AGENT_MAX = 60
CAPS_ITEMS = 12                 # des listes de `caps` : au plus ce nombre de mots, de ce nombre de caractères
CAPS_WORD = 24
CAPS_LISTS = ("video", "audio", "containers")           # ce que lit `probe.plan`, et rien d'autre
CAPS_NUMBERS = {"channels": 32, "height": 8640}
WORD_RE = re.compile(r"[\w.+:-]{1,%d}" % CAPS_WORD)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
BROWSERS = (("Edg", "Edge"), ("OPR", "Opera"), ("FxiOS", "Firefox"), ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Version", "Safari"))
SYSTEMS = (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("CrKey", "Chromecast"), ("Tizen", "Tizen"), ("Web0S", "webOS"), ("Windows", "Windows"), ("Macintosh", "macOS"), ("Linux", "Linux"))


def clean_text(value, limit):
    """Le texte sans caractère de contrôle, au plus `limit` caractères ; vide quand ce n'est pas un texte."""
    return CONTROL_RE.sub("", value).strip()[:limit] if isinstance(value, str) else ""


def short_agent(text):
    """L'agent utilisateur en quelques mots (« Firefox 120 · Windows »), borné ; un client qui n'est pas un navigateur reste tel quel, tronqué."""
    text = clean_text(text, 400)
    browser = next((f"{name} {found.group(1)}" for token, name in BROWSERS if (found := re.search(rf"\b{token}/(\d+)", text))), "")
    system = next((name for token, name in SYSTEMS if token in text), "")
    return " · ".join(part for part in (browser, system) if part)[:AGENT_MAX] or text[:AGENT_MAX]


def clean_device(value):
    """`pc`, `phone`, `tablet` ou `tv` ; vide pour toute autre valeur : le client la déclare, on ne garde que ce qu'on connaît."""
    return value if isinstance(value, str) and value in DEVICES else ""


def clean_caps(caps):
    """Seulement les clés que lit la décision de lecture : `video`, `audio`, `containers` (des mots courts, en nombre borné), `channels`, `height` (des entiers bornés) et `hdr` (un booléen).
    Ce que le client envoie est écarté dès qu'il n'a pas le bon type ; jamais recopié tel quel."""
    caps = caps if isinstance(caps, dict) else {}
    out = {}
    for key in CAPS_LISTS:
        if isinstance(caps.get(key), list):
            words = [word for word in caps[key] if isinstance(word, str) and WORD_RE.fullmatch(word)][:CAPS_ITEMS]
            if words:
                out[key] = words
    for key, top in CAPS_NUMBERS.items():
        number = caps.get(key)
        if isinstance(number, float) and number.is_integer():
            number = int(number)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= top:
            out[key] = number
    if isinstance(caps.get("hdr"), bool):
        out["hdr"] = caps["hdr"]
    return out


def origin_of(address):
    """`local` pour la boucle locale et les adresses privées (réseau de la maison, IPv6 locale), `remote` pour le reste, adresse illisible comprise. `address` est celui de `client_ip` : une adresse, ou, pour l'IPv6 derrière le proxy, un réseau /64.
    Une adresse Tailscale en 100.64.0.0/10 n'est pas privée pour Python : `remote` ; en `fd7a:115c:a1e0::/48` (un réseau local unique) : `local`."""
    try:
        found = ipaddress.ip_network(str(address).partition("%")[0], strict=False).network_address
    except ValueError:
        return "remote"
    found = getattr(found, "ipv4_mapped", None) or found
    return "local" if found.is_loopback or found.is_private else "remote"


@dataclass
class Line:
    user: str                   # l'identifiant du compte
    name: str                   # son nom, tel qu'il était au dernier transfert
    ref: str                    # ce qui est transféré : un fichier de la médiathèque, ou de quoi reconnaître un envoi, un fichier de la boîte, une sauvegarde
    address: str                # jamais rendue
    kind: str
    title: str
    library: str
    origin: str
    agent: str
    started: float
    last: float
    bytes: int = 0
    connections: int = 0

    def public(self):
        return {"user": self.name, "title": self.title, "library": self.library, "kind": self.kind, "origin": self.origin, "agent": self.agent,
                "started": int(self.started), "last": int(self.last), "bytes": self.bytes, "connections": self.connections}


@dataclass
class Decision:
    at: float
    user: str
    name: str
    address: str                # jamais rendue : elle ne sert qu'à rapprocher la décision d'un transfert
    agent: str
    device: str
    entry: str
    title: str
    caps: dict
    mode: str
    why: str

    def public(self):
        return {"at": int(self.at), "user": self.name, "device": self.device, "agent": self.agent, "entry": self.entry, "title": self.title, "caps": self.caps, "mode": self.mode, "why": self.why}


class Activity:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.lines = {}                                         # (compte, ref, nature, adresse) -> Line
        self.decisions = deque(maxlen=DECISIONS_KEPT)

    # --- Les transferts ---------------------------------------------------
    @contextmanager
    def transfer(self, user, name, ref, kind, address, title="", library="", agent=""):
        """Un transfert ouvert le temps du bloc, qui rend la fonction où compter les octets envoyés ; il se referme sur toute sortie, une coupure du client comprise.
        Un transfert qui échoue avant le moindre octet ne laisse pas de ligne (un envoi refusé n'a rien envoyé)."""
        key = (user, ref, kind, address)
        with self.lock:
            now = self.clock()
            self._prune(now)
            line = self.lines.get(key)
            if line is None:
                line = self.lines[key] = Line(user, name, ref, address, kind, clean_text(title, TITLE_MAX), clean_text(library, TITLE_MAX), origin_of(address), short_agent(agent), now, now)
            line.name, line.connections, line.last = name, line.connections + 1, now
        failed = False
        try:
            yield partial(self._count, line)
        except BaseException:
            failed = True
            raise
        finally:
            self._close(key, line, failed)

    def _count(self, line, size):
        with self.lock:
            line.bytes += size
            line.last = self.clock()

    def _close(self, key, line, failed):
        with self.lock:
            now = self.clock()
            line.connections -= 1
            line.last = now
            if failed and not line.bytes and not line.connections and self.lines.get(key) is line:
                del self.lines[key]
            self._prune(now)

    def touch(self, user, ref):
        """Le lecteur du compte a donné sa position : les lignes de ce compte et de ce fichier sont toujours regardées. Ne crée jamais de ligne."""
        with self.lock:
            now = self.clock()
            self._prune(now)
            for line in self.lines.values():
                if line.user == user and line.ref == ref:
                    line.last = now

    def _prune(self, now):
        """Retire les lignes sans connexion ouverte dont la dernière activité est trop ancienne (verrou pris) : le registre ne grandit pas."""
        for key in [key for key, line in self.lines.items() if not line.connections and now - line.last >= ACTIVE_SECONDS]:
            del self.lines[key]

    # --- Les décisions ----------------------------------------------------
    def decide(self, user, name, address, agent, device, entry, title, caps, mode, why):
        """Garde une décision de lecture. Ce que le client a dit (`device`, `caps`, agent) est nettoyé ici, jamais gardé tel quel."""
        with self.lock:
            self.decisions.append(Decision(self.clock(), user, name, address, short_agent(agent), clean_device(device), entry, clean_text(title, TITLE_MAX), clean_caps(caps),
                                           clean_text(mode, 20), clean_text(why, 300)))

    # --- La lecture -------------------------------------------------------
    def report(self):
        """{"counts", "transfers", "decisions"} : les lignes actives (avec `device` quand une décision du même compte, de la même entrée et du même client le donne), les décisions de la plus récente à la plus ancienne."""
        with self.lock:
            self._prune(self.clock())
            lines, decisions = list(self.lines.values()), list(reversed(self.decisions))
            return {"counts": counts(lines), "transfers": [self._shown(line, decisions) for line in lines], "decisions": [d.public() for d in decisions]}

    @staticmethod
    def _shown(line, decisions):
        out = line.public()
        device = next((d.device for d in decisions if (d.user, d.entry, d.address) == (line.user, line.ref, line.address)), "")
        if device:
            out["device"] = device
        return out


def counts(lines):
    kinds = [line.kind for line in lines]
    return {"playing": sum(kind in PLAYING for kind in kinds), "downloads": kinds.count("download"), "uploads": kinds.count("upload"), "remux": kinds.count("remux"),
            "transcodes": kinds.count("transcode"), "backups": kinds.count("backup")}
