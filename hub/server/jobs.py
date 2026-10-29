"""Tâches de fond : scan d'une bibliothèque, enrichissement TMDB, nettoyage. Une seule à la fois, dans l'ordre d'arrivée.

Rien ne se lance tout seul (sauf le premier scan d'une bibliothèque vide, et le rescan périodique si HUB_AUTOSCAN_HOURS le demande) : le bouton « Mettre à jour » de l'interface est le déclencheur habituel.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from collections import deque

from libraries.music import coverfile, wanted

from .. import enrich, scan
from ..scan.common import is_album_cover

LOG_LINES = 400


class Job:
    def __init__(self, ident, label, func):
        self.id, self.label, self.func = ident, label, func
        self.status = "waiting"           # waiting | running | done | failed
        self.log = deque(maxlen=LOG_LINES)
        self.queued = time.time()
        self.started = self.ended = None
        self.result = None

    def line(self, text):
        self.log.append(text)

    def public(self, with_log=False):
        out = {"id": self.id, "label": self.label, "status": self.status, "queued": int(self.queued), "started": int(self.started) if self.started else None,
               "ended": int(self.ended) if self.ended else None, "result": self.result}
        if with_log:
            out["log"] = list(self.log)
        return out


class Jobs:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.queue = queue.Queue()
        self.history = deque(maxlen=30)
        self.counter = 0
        self.lock = threading.Lock()
        self.worker = threading.Thread(target=self._run, name="jobs", daemon=True)
        self.worker.start()

    def _submit(self, label, func):
        with self.lock:
            if any(j.label == label and j.status in ("waiting", "running") for j in self.history):
                return next(j for j in self.history if j.label == label and j.status in ("waiting", "running"))
            self.counter += 1
            job = Job(self.counter, label, func)
            self.history.appendleft(job)
        self.queue.put(job)
        return job

    def _run(self):
        while True:
            job = self.queue.get()
            job.status, job.started = "running", time.time()
            try:
                job.result = job.func(job.line)
                job.status = "done"
            except Exception as e:           # une tâche qui échoue ne doit pas arrêter les suivantes
                job.line(f"ERREUR : {e}")
                job.status = "failed"
            job.ended = time.time()

    # --- Les tâches --------------------------------------------------------
    def scan(self, lib):
        return self._submit(f"Mise à jour · {lib.label}", lambda log: scan.scan_library(self.db, lib, log))

    def scan_all(self):
        def run(log):
            return [scan.scan_library(self.db, lib, log) for lib in self.cfg.libraries]
        return self._submit("Mise à jour · tout", run)

    def enrich(self, lib, force=False):
        return self._submit(f"Enrichissement TMDB · {lib.label}", lambda log: enrich.enrich_shows(self.cfg, self.db, lib, log, force))

    def enrich_artists(self, lib, force=False):
        return self._submit(f"Fiches artistes · {lib.label}", lambda log: enrich.enrich_artists(self.cfg, self.db, lib, log, force))

    def cleanup(self, apply):
        def run(log):
            return clean_junk(self.cfg.libraries, apply, log)
        return self._submit("Nettoyage · fichiers parasites" + ("" if apply else " (simulation)"), run)

    def snapshot(self, with_log=False):
        return [j.public(with_log) for j in list(self.history)]

    def busy(self):
        return any(j.status in ("waiting", "running") for j in self.history)


JUNK = ("thumbs.db", "desktop.ini", ".ds_store", "albumartsmall.jpg")
# Lecteur Windows Media : AlbumArt_{identifiant}_Large.jpg et AlbumArt_{identifiant}_Small.jpg, semés dans chaque dossier d'album
ALBUM_ART = re.compile(r"^albumart_\{[0-9a-f-]{36}\}_(large|small)\.jpg$", re.IGNORECASE)
SKIP_DIRS = {".zfs"}        # instantanés ZFS : lecture seule, et copies de tout
CANDIDATES_DIRS = {coverfile.CANDIDATES_DIR.casefold()}      # les pochettes entre lesquelles Cover.py --candidates laisse choisir : elles n'ont pas encore été choisies


def is_junk(name, music=False):
    """Ce nom de fichier est-il un déchet ? Ceux que Windows ou macOS sèment (Thumbs.db, AlbumArt_{...}_Large.jpg...) ; et, en musique, toute image qui n'est pas cover.{ext}, la seule pochette d'un album, ni la pochette d'un album manquant ("1997 - OK Computer.jpg")."""
    if name.casefold() in JUNK or ALBUM_ART.match(name):
        return True
    return music and os.path.splitext(name.casefold())[1] in coverfile.EXTS and not is_album_cover(name) and wanted.parse(name) is None


def walk_junk(root, music=False):
    """Les chemins des fichiers parasites sous `root`, sans suivre les liens symboliques ni entrer dans .zfs."""
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (music and d.casefold() in CANDIDATES_DIRS)]
        for name in files:
            path = os.path.join(folder, name)
            if is_junk(name, music) and not os.path.islink(path):
                yield path


def clean_junk(libraries, apply, log):
    """Les fichiers parasites : ceux que Windows et macOS sèment dans les dossiers, et les images de musique autres que cover.{ext} : comptés, et effacés si `apply`."""
    found = removed = 0
    for lib in libraries:
        for path in walk_junk(lib.path, lib.kind == "music"):
            found += 1
            if apply:
                try:
                    os.unlink(path)
                    removed += 1
                except OSError as e:
                    log(f"  impossible d'effacer {path} : {e}")
    log(f"{found} fichier(s) de ce genre" + (f", {removed} effacé(s)." if apply else " (simulation : rien n'a été effacé)."))
    return {"found": found, "removed": removed}
