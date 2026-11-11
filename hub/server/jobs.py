"""Tâches de fond : scan d'une bibliothèque, enrichissement TMDB, nettoyage. Une seule à la fois, dans l'ordre d'arrivée.

Rien ne se lance tout seul (sauf le premier scan d'une bibliothèque vide, et le rescan périodique si HUB_AUTOSCAN_HOURS le demande) : le bouton « Mettre à jour » de l'interface est le déclencheur habituel.
"""

from __future__ import annotations

import errno
import os
import queue
import re
import stat
import threading
import time
from collections import deque
from pathlib import Path

from libraries.music import coverfile, wanted

from .. import enrich, labels, scan
from ..scan.common import is_album_cover

LOG_LINES = 400


class Job:
    def __init__(self, ident, label, func, key=None):
        self.id, self.label, self.func = ident, label, func
        self.key = label if key is None else key          # ce qui reconnaît la même demande (voir Jobs._submit) ; l'intitulé, lui, n'est que ce qu'on affiche
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

    def _submit(self, label, func, key=None):
        """Met la tâche en file, ou rend celle qui attend ou tourne déjà pour la même demande. La demande est reconnue à `key` (sans lui, à l'intitulé) : l'intitulé porte le libellé affiché
        d'une bibliothèque, que l'administrateur peut changer ou fixer à « tout » ; il ne doit jamais décider si deux demandes sont la même."""
        key = label if key is None else key
        with self.lock:
            same = next((j for j in self.history if j.key == key and j.status in ("waiting", "running")), None)
            if same:
                return same
            self.counter += 1
            job = Job(self.counter, label, func, key)
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
        return self._submit(f"Mise à jour · {labels.shown(self.db, lib)}", lambda log: scan.scan_library(self.db, lib, log), ("scan", lib.id))

    def scan_all(self):
        def run(log):
            return [scan.scan_library(self.db, lib, log) for lib in self.cfg.libraries]
        return self._submit("Mise à jour · tout", run, ("scan-all",))

    def enrich(self, lib, force=False):
        return self._submit(f"Enrichissement TMDB · {labels.shown(self.db, lib)}", self._then_ratings(enrich.enrich_shows, lib, force), ("enrich", lib.id))

    def enrich_sagas(self, lib, force=False):
        return self._submit(f"Sagas TMDB · {labels.shown(self.db, lib)}", self._then_ratings(enrich.enrich_sagas, lib, force), ("sagas", lib.id))

    def _then_ratings(self, first, lib, force):
        """La tâche qui fait `first` (fiches des séries, sagas des films) puis la passe des notes TMDB de la même bibliothèque : un seul bilan, les échecs des deux additionnés."""
        def run(log):
            started = int(time.time())
            counts = first(self.cfg, self.db, lib, log, force)
            ratings = enrich.enrich_ratings(self.cfg, self.db, lib, log, force, since=started)          # forcé : ce que `first` vient de noter (les séries) n'est pas redemandé
            return {**counts, "rated": ratings["rated"], "failed": counts["failed"] + ratings["failed"]}
        return run

    def enrich_artists(self, lib, force=False):
        return self._submit(f"Fiches artistes · {labels.shown(self.db, lib)}", lambda log: enrich.enrich_artists(self.cfg, self.db, lib, log, force), ("artists", lib.id))

    def enrich_person(self, name, person_id, force=False):
        """Les films que TMDB attribue à une personne (`person_id` : son identifiant TMDB ; `name` : pour l'intitulé de la tâche)."""
        return self._submit(f"Films de {name}", lambda log: enrich.enrich_person(self.cfg, self.db, person_id, log, force))

    def enrich_people(self, libs, label="bibliothèques de films", force=False):
        """Les films des réalisateurs et des acteurs de premier plan de ces bibliothèques `libs` : voir enrich.enrich_people. `label` : « bibliothèques de films » ou le nom de la catégorie (« Séries »), pour l'intitulé de la tâche : une tâche par catégorie."""
        return self._submit(f"Films manquants · {label}", lambda log: enrich.enrich_people(self.cfg, self.db, libs, log, force), ("people", tuple(lib.id for lib in libs)))

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


ARTIST_PHOTO = f"{enrich.PHOTO_STEM}.webp"        # la photo d'un artiste, dans son dossier


def is_junk(name, music=False):
    """Ce nom de fichier est-il un déchet ? Ceux que Windows ou macOS sèment (Thumbs.db, AlbumArt_{...}_Large.jpg...) ; et, en musique, toute image qui n'est pas cover.{ext}, la seule pochette d'un album, ni la pochette d'un album manquant ("1997 - OK Computer.jpg"), ni artist.webp, la photo d'un artiste."""
    if name.casefold() in JUNK or ALBUM_ART.match(name):
        return True
    return music and os.path.splitext(name.casefold())[1] in coverfile.EXTS and not is_album_cover(name) and name.casefold() != ARTIST_PHOTO and wanted.parse(name) is None


def walk_junk(root, music=False):
    """Les chemins des fichiers parasites sous `root`, sans suivre les liens symboliques ni entrer dans .zfs."""
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (music and d.casefold() in CANDIDATES_DIRS)]
        for name in files:
            path = os.path.join(folder, name)
            if is_junk(name, music) and not os.path.islink(path):
                yield path


def force_unlink(path):
    """Efface `path`, comme `rm -f` : si les droits l'interdisent (fichier en lecture seule, dossier non modifiable), les rend le temps d'effacer, puis remet ceux du dossier. Un système de fichiers monté en lecture seule (EROFS) reste, lui, infranchissable."""
    try:
        os.unlink(path)
        return
    except PermissionError:
        pass
    folder = os.path.dirname(path)
    mode = os.stat(folder).st_mode
    try:
        os.chmod(folder, mode | stat.S_IWUSR | stat.S_IXUSR)
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
        os.unlink(path)
    finally:
        os.chmod(folder, mode)


SAMPLE = 20           # les premiers fichiers que la simulation nomme : de quoi voir ce qui partirait avant de confirmer


def erase(path, lib, read_only, log):
    """Efface `path` (de force) ; rend (effacé ?, fichiers vus sur un volume en lecture seule, celui-ci compris). Une erreur est dite pour son fichier ; un volume en lecture seule, une seule fois."""
    try:
        force_unlink(path)
        return True, read_only
    except OSError as e:
        if e.errno != errno.EROFS:
            log(f"  impossible d'effacer {path} : {e}")
        elif not read_only:         # le volume entier est en lecture seule : une seule ligne par bibliothèque suffit
            log(f"  impossible d'effacer {path} : volume en lecture seule (retire « :ro » du volume {lib.path} dans docker-compose.yml)")
        return False, read_only + (e.errno == errno.EROFS)


def shown_name(lib, path):
    """Le nom d'un fichier dans la liste : depuis le dossier de sa bibliothèque (« Musique/Daft Punk/Thumbs.db »), avec des « / »."""
    return f"{Path(lib.path).name}/{os.path.relpath(path, lib.path).replace(os.sep, '/')}"


def list_sample(sample, found, log):
    """Dans le journal d'une simulation : les premiers fichiers, puis combien d'autres."""
    for name in sample:
        log(f"  {name}")
    if found > len(sample):
        log(f"  … et {found - len(sample)} autre(s).")


def clean_junk(libraries, apply, log):
    """Les fichiers parasites : ceux que Windows et macOS sèment dans les dossiers, et les images de musique autres que cover.{ext} : comptés (les premiers nommés), et effacés si `apply` (de force : voir force_unlink)."""
    found = removed = 0
    sample = []
    for lib in libraries:
        read_only = 0
        for path in walk_junk(lib.path, lib.kind == "music"):
            found += 1
            if len(sample) < SAMPLE:
                sample.append(shown_name(lib, path))
            if apply:
                erased, read_only = erase(path, lib, read_only, log)
                removed += erased
        if read_only > 1:
            log(f"  … et {read_only - 1} autre(s) fichier(s) de {lib.path} sur ce volume en lecture seule.")
    sample.sort()
    if not apply:
        list_sample(sample, found, log)
    log(f"{found} fichier(s) de ce genre" + (f", {removed} effacé(s)." if apply else " (simulation : rien n'a été effacé)."))
    return {"found": found, "removed": removed, "sample": sample}
