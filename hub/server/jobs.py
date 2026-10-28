"""Tâches de fond : scan d'une bibliothèque, enrichissement TMDB, nettoyage. Une seule à la fois, dans l'ordre d'arrivée.

Rien ne se lance tout seul (sauf le premier scan d'une bibliothèque vide, et le rescan périodique si HUB_AUTOSCAN_HOURS le demande) : le bouton « Mettre à jour » de l'interface est le déclencheur habituel.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

from .. import enrich, scan

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

    def cleanup(self, apply):
        def run(log):
            return clean_junk(self.cfg.libraries, apply, log)
        return self._submit("Nettoyage · Thumbs.db" + ("" if apply else " (simulation)"), run)

    def snapshot(self, with_log=False):
        return [j.public(with_log) for j in list(self.history)]

    def busy(self):
        return any(j.status in ("waiting", "running") for j in self.history)


JUNK = ("thumbs.db", "desktop.ini", ".ds_store")


def clean_junk(libraries, apply, log):
    """Les fichiers que Windows et macOS sèment dans les dossiers (Thumbs.db...) : comptés, et effacés si `apply`."""
    found = removed = 0
    for lib in libraries:
        for path in lib.path.rglob("*"):
            if path.is_file() and path.name.casefold() in JUNK:
                found += 1
                if apply:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError as e:
                        log(f"  impossible d'effacer {path} : {e}")
    log(f"{found} fichier(s) de ce genre" + (f", {removed} effacé(s)." if apply else " (simulation : rien n'a été effacé)."))
    return {"found": found, "removed": removed}
