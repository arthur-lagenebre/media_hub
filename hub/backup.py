"""Sauvegarde et restauration des données propres au hub, sans les médias ni ce qui se refait.

L'archive (un .zip) contient ce qui ne se refait pas :

    hub.db         comptes (mots de passe hachés), clé des sessions, droits, progressions, avis, playlists, demandes, suggestions, réglages, enrichissement
    sagas.json     les sagas composées à la main
    posters/       les affiches choisies à la main
    artists/       les photos d'artistes gardées hors de la Musique

et `manifest.json` : format, version du hub, version du schéma de la base, et chaque fichier avec sa taille et son empreinte SHA-256.
Les caches (img/, subs/, tmdb/, tmdb-cache/, musicbrainz-cache/), les outils (bin/, pylib/) et la boîte de réception n'y sont pas : ils se refont,
ou se sauvegardent par instantané de leur dataset.

**L'archive n'est pas chiffrée** (bibliothèque standard seulement) : elle contient les hachés des mots de passe et la clé des sessions, qui est gardée
à la restauration (les appareils restent connectés). Elle se garde comme un mot de passe.

La base est copiée par l'API de sauvegarde de SQLite, cohérente pendant que le serveur écrit (mode WAL) ; jamais par copie du fichier, qui perdrait
ce qui est encore dans le journal.

La restauration se fait serveur arrêté (`python -m hub restore`) : elle prend le verrou de HUB_DATA (runlock.py), que le serveur garde tant qu'il tourne, et refuse
s'il est déjà pris. Elle vérifie toute l'archive avant de toucher à quoi que ce soit : format connu,
schéma pas plus récent que ce hub, chemins attendus seulement, tailles et empreintes, base SQLite intègre. Une archive refusée laisse l'installation
telle qu'elle était. Sur une installation qui a déjà une base, il faut `replace=True` : ce qui est remplacé est mis de côté dans
`avant-restauration-<date>/`, rien n'est effacé. Une base d'un schéma plus ancien est migrée au démarrage suivant (db.Database).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__, runlock
from .db import SCHEMA_VERSION

FORMAT = 1                                   # monte si la forme de l'archive change au point qu'un hub d'avant ne saurait plus la lire
BASE = "hub.db"
MANIFEST = "manifest.json"
FILES = ("sagas.json",)                      # fichiers facultatifs, à la racine de HUB_DATA
FOLDERS = ("posters", "artists")             # dossiers facultatifs de HUB_DATA, à plat (un fichier par affiche ou photo)
BACKUPS = "sauvegardes"                      # HUB_DATA/sauvegardes : où `python -m hub backup` écrit sans chemin donné
ASIDE = "avant-restauration"                 # HUB_DATA/avant-restauration-<date> : ce qu'une restauration remplace
TEMPORARY = (".tmp", ".part")                # une écriture en cours (images.write_atomic, enrich) : jamais sauvegardée
CHUNK = 1 << 20


class BackupError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def archive_name(now=None):
    """Le nom proposé pour une archive : `media_hub-<version>-<AAAAMMJJ-HHMMSS>.zip`."""
    return f"media_hub-{__version__}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}.zip"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_of(path):
    """Le numéro de schéma gardé dans la base (`kv` « schema ») ; None s'il n'y en a pas."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='schema'").fetchone()
    finally:
        conn.close()
    return int(json.loads(row[0])) if row else None


def _copy_base(db_path, out):
    """Copie cohérente de la base, journal WAL compris, par l'API de sauvegarde de SQLite."""
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(out)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")          # une seule pièce : pas de -wal à côté de la copie
    finally:
        target.close()
        source.close()


def _sources(data):
    """[(chemin dans l'archive, fichier)] de ce qui accompagne la base."""
    found = [(name, data / name) for name in FILES if (data / name).is_file()]
    for folder in FOLDERS:
        if (data / folder).is_dir():
            found += [(f"{folder}/{path.name}", path) for path in sorted((data / folder).iterdir())
                      if path.is_file() and not path.is_symlink() and not path.name.startswith(".") and not path.name.endswith(TEMPORARY)]
    return found


def create(db_path, data, out):
    """Écrit l'archive `out` (remplacée si elle existe) et rend son manifeste. BackupError si la base manque. L'archive s'écrit à côté puis prend son nom
    d'un bloc : une sauvegarde coupée ne laisse pas une archive tronquée sous le nom attendu."""
    db_path, data, out = Path(db_path), Path(data), Path(out)
    if not db_path.is_file():
        raise BackupError(f"pas de base à sauvegarder : {db_path} n'existe pas")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".sauvegarde-", dir=out.parent))
    try:
        base = work / BASE
        _copy_base(db_path, base)
        members = [(BASE, base)] + _sources(data)
        manifest = {"format": FORMAT, "hub": __version__, "schema": _schema_of(base), "created": int(time.time()),
                    "files": [{"path": name, "size": path.stat().st_size, "sha256": _sha256(path)} for name, path in members]}
        partial = work / "archive.zip"
        with zipfile.ZipFile(partial, "w") as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))
            for name, path in members:
                z.write(path, name, zipfile.ZIP_DEFLATED if name.endswith((".db", ".json")) else zipfile.ZIP_STORED)
        os.replace(partial, out)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _allowed(name):
    """`name` est-il un chemin que l'archive peut contenir : la base, un fichier de FILES, ou un fichier à plat d'un dossier de FOLDERS ? Rien d'autre,
    et surtout rien qui sorte de HUB_DATA (`..`, chemin absolu, lecteur Windows, antislash)."""
    if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if name in (BASE, *FILES):
        return True
    return len(parts) == 2 and parts[0] in FOLDERS and parts[1] not in ("", ".", "..") and not parts[1].startswith(".")


def _open(archive):
    try:
        return zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise BackupError(f"{archive} n'est pas une archive zip lisible")


def _load_manifest(z):
    try:
        manifest = json.loads(z.read(MANIFEST))
    except KeyError:
        raise BackupError("archive sans manifest.json : ce n'est pas une sauvegarde de media_hub")
    except ValueError:
        raise BackupError("manifest.json illisible : archive abîmée")
    if not isinstance(manifest, dict):
        raise BackupError("manifest.json illisible : archive abîmée")
    return manifest


def _check_manifest(manifest):
    """Le format, la version de la base et la liste des fichiers que dit le manifeste."""
    if manifest.get("format") != FORMAT:
        raise BackupError(f"format d'archive inconnu ({manifest.get('format')}) : ce hub lit le format {FORMAT}")
    schema = manifest.get("schema")
    if isinstance(schema, int) and schema > SCHEMA_VERSION:
        raise BackupError(f"archive d'un hub plus récent (base en version {schema}, ce hub n'en connaît que {SCHEMA_VERSION}) : mets le hub à jour avant de restaurer")
    files = manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise BackupError("manifest.json sans liste de fichiers : archive abîmée")
    return manifest


def _check_paths(listed):
    """Chaque chemin du manifeste est un de ceux qu'une sauvegarde contient, et la base y est."""
    refused = [name for name in listed if not _allowed(name)]
    if refused:
        raise BackupError(f"chemin refusé dans l'archive : {refused[0]!r}")
    if BASE not in listed:
        raise BackupError("archive sans hub.db")


def _check_members(z, listed):
    """L'archive contient exactement ce que liste le manifeste : ni intrus, ni manquant."""
    present = {info.filename for info in z.infolist() if not info.is_dir()} - {MANIFEST}
    extra = sorted(present - set(listed))
    if extra:
        raise BackupError(f"fichier que le manifeste ne liste pas : {extra[0]}")
    missing = sorted(set(listed) - present)
    if missing:
        raise BackupError(f"fichier listé mais absent de l'archive : {missing[0]}")


def _extract(z, name, entry, stage):
    """Extrait un fichier dans `stage` en vérifiant sa taille et son empreinte."""
    info = z.getinfo(name)
    if info.file_size != entry.get("size"):
        raise BackupError(f"{name} : taille différente de celle du manifeste (archive abîmée)")
    target = stage / name
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with z.open(info) as src, open(target, "wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            digest.update(chunk)
            dst.write(chunk)
    if digest.hexdigest() != entry.get("sha256"):
        raise BackupError(f"{name} : empreinte différente de celle du manifeste (archive abîmée)")


def _check_and_extract(archive, stage):
    """Vérifie toute l'archive et l'extrait dans `stage` ; rend le manifeste. BackupError au premier défaut."""
    with _open(archive) as z:
        manifest = _check_manifest(_load_manifest(z))
        listed = {f.get("path"): f for f in manifest["files"]}
        _check_paths(listed)
        _check_members(z, listed)
        for name, entry in listed.items():
            _extract(z, name, entry, stage)
    _check_base(stage / BASE)
    return manifest


def _check_base(path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        schema = _schema_of(path)
    except (sqlite3.DatabaseError, ValueError, TypeError):
        raise BackupError("hub.db n'est pas une base SQLite lisible (archive abîmée)")
    if not ok:
        raise BackupError("hub.db ne passe pas la vérification d'intégrité de SQLite (archive abîmée)")
    if schema is not None and schema > SCHEMA_VERSION:
        raise BackupError(f"hub.db est en version {schema}, ce hub n'en connaît que {SCHEMA_VERSION} : mets le hub à jour avant de restaurer")


def _targets(db_path, data):
    """[(chemin dans l'archive, emplacement)] de tout ce qu'une restauration remplace, journaux de la base compris."""
    return ([(BASE, db_path), (None, Path(f"{db_path}-wal")), (None, Path(f"{db_path}-shm"))]
            + [(name, data / name) for name in FILES] + [(folder, data / folder) for folder in FOLDERS])


def restore(archive, db_path, data, replace=False):
    """Remet l'archive en place, serveur arrêté. Rend {"schema", "files", "aside"} : `aside`, le dossier où l'ancienne installation a été mise de côté (None
    s'il n'y avait rien). BackupError, sans rien modifier, si l'archive est refusée ou si une base existe déjà sans `replace`."""
    db_path, data = Path(db_path), Path(data)
    if not Path(archive).is_file():
        raise BackupError(f"archive introuvable : {archive}")
    lock = runlock.hold(data)
    if lock is None:
        raise BackupError(f"le serveur tourne sur ces données ({data}) : arrête-le avant de restaurer (rien n'a été modifié)")
    try:
        return _restore_locked(Path(archive), db_path, data, replace)
    finally:
        runlock.release(lock)


def _restore_locked(archive, db_path, data, replace):
    """La restauration elle-même, verrou de HUB_DATA tenu : aucun serveur ne démarre pendant ce temps (il le dirait)."""
    if db_path.exists() and not replace:
        raise BackupError(f"une base existe déjà ({db_path}) : arrête le serveur, puis relance avec --replace (l'installation actuelle sera mise de côté dans {ASIDE}-<date>/, rien n'est effacé)")
    stage = Path(tempfile.mkdtemp(prefix=".restauration-", dir=data))
    try:
        manifest = _check_and_extract(archive, stage)
        aside = _set_aside(db_path, data)
        _put_in_place(stage, db_path, data)
        return {"schema": manifest.get("schema"), "files": len(manifest["files"]), "aside": aside}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _set_aside(db_path, data):
    """Déplace ce qu'une restauration remplace dans `avant-restauration-<date>/` ; rend ce dossier (texte), ou None s'il n'y avait rien."""
    existing = [path for _, path in _targets(db_path, data) if path.exists()]
    if not existing:
        return None
    aside = data / f"{ASIDE}-{time.strftime('%Y%m%d-%H%M%S')}"
    aside.mkdir()
    for path in existing:
        os.replace(path, aside / path.name)
    return str(aside)


def _put_in_place(stage, db_path, data):
    """Déplace ce que `stage` a reçu de l'archive à sa place dans HUB_DATA."""
    for name, path in _targets(db_path, data):
        if name and (stage / name).exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / name, path)
