"""La boîte de réception : les fichiers que des comptes autorisés envoient au NAS, rangés sous HUB_DATA/inbox/<compte>/.

Le hub n'écrit jamais dans la médiathèque : un envoi n'entre dans aucune bibliothèque, l'administrateur le range lui-même. Un fichier déposé n'est jamais analysé (ni ffprobe ni scan) ; il n'est servi qu'à l'administrateur, en pièce jointe.

Ce module ne connaît pas HTTP : `receive` lit un flux (le corps de la requête) et tient les limites, `listing`, `path_of` et `remove` bornent tout chemin au dossier d'un compte. Le serveur (`server/app.py`) décide qui a le droit.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from pathlib import Path

CHUNK = 1 << 20                 # le corps est lu par morceaux de 1 Mo : jamais en mémoire d'un coup
PART = ".part"                  # un envoi en cours : renommé à la fin, jeté s'il échoue
STALE = 24 * 3600               # un .part plus vieux que cela est un envoi abandonné (arrêt brutal du serveur)
NAME_BYTES = 200                # longueur du nom, en octets UTF-8 : de la place pour « (2) » et « .part » sous la limite de 255 des systèmes de fichiers
SAME_NAME = 999                 # « Film (2).mkv », « Film (3).mkv »… jusque-là
ACCOUNT = re.compile(r"\w{1,64}", re.ASCII)       # un identifiant de compte (`default`, ou hexadécimal) : le seul nom de dossier que l'on construit à partir d'une donnée

# Ce que l'on accepte de ranger : vidéo, audio, livre, sous-titres, archive zip. Ni exécutable, ni script, ni page web (.exe, .sh, .bat, .js, .html, .svg…) : la liste est l'autorisation, tout le reste est refusé.
EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".m2ts", ".flv", ".ogv",
    ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".ape", ".wv",
    ".epub", ".cbz", ".cbr", ".cb7", ".pdf", ".mobi", ".azw3", ".fb2",
    ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup",
    ".zip",
})
RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))})      # noms que Windows n'ouvre pas comme des fichiers


class InboxError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _safe_char(char):
    """Ni séparateur, ni caractère que Windows refuse, ni caractère de contrôle, de mise en forme (inversion du sens d'écriture) ou de saut de ligne ou de paragraphe (U+2028, U+2029), ni caractère sans codage."""
    return char not in '<>:"/\\|?*' and unicodedata.category(char) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def clean_name(raw):
    """Le nom sous lequel ranger un fichier : seulement le dernier élément du chemin donné (`/` ou `\\`), les caractères douteux remplacés par `_`, ni point ni espace au début ou à la fin,
    un nom réservé de Windows préfixé par `_`, coupé à NAME_BYTES octets en gardant l'extension. 400 s'il ne reste rien ; 415 si l'extension n'est pas dans EXTENSIONS (la casse ne compte pas)."""
    if not isinstance(raw, str):
        raise InboxError(400, "nom du fichier attendu (?name=…)")
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c if _safe_char(c) else "_" for c in name).strip(" .")
    if not name:
        raise InboxError(400, "nom du fichier vide ou illisible")
    stem, ext = os.path.splitext(name)
    if ext.lower() not in EXTENSIONS:
        raise InboxError(415, "type de fichier refusé : " + (f"« {ext[:12]} »" if ext else "pas d'extension"))
    stem = stem.rstrip(" .")
    if stem.split(".")[0].rstrip().upper() in RESERVED:
        stem = "_" + stem
    stem = stem.encode("utf-8")[:NAME_BYTES - len(ext.encode("utf-8"))].decode("utf-8", "ignore").rstrip(" .")
    return stem + ext


def is_stored_name(name):
    """Ce nom peut-il être celui d'un fichier fini de la boîte ? Le contraire de ce que `clean_name` ne produit jamais : séparateur, caractère douteux, point au début, point ou espace à la fin, `.part`."""
    try:
        size = len(name.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return False
    return 0 < size <= 255 and name == name.strip(" .") and not name.endswith(PART) and all(_safe_char(c) for c in name)


def _entries(folder):
    try:
        with os.scandir(folder) as found:
            return list(found)
    except OSError:
        return []


def _stat_file(entry):
    """Le `stat` d'un fichier régulier (pas un lien), ou None : ce n'est pas un fichier, ou il a disparu entre le parcours du dossier et ici (un DELETE, un envoi qui se termine)."""
    try:
        return entry.stat() if entry.is_file(follow_symlinks=False) else None
    except OSError:
        return None


def _discard(path):
    """Efface `path` s'il est là ; vrai s'il l'a été. Au mieux : un reste sera pris par `sweep`."""
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


class Inbox:
    """Les limites viennent de la configuration : espace libre à garder sous HUB_DATA (octets) et envois simultanés. Ni taille par fichier ni quota par compte, choix de l'utilisateur : seul le disque limite. Les attributs se lisent à chaque envoi."""

    def __init__(self, cfg):
        self.root = Path(cfg.data) / "inbox"
        self.keep_free, self.max_uploads = cfg.inbox_keep_free, cfg.inbox_max_uploads
        self._lock = threading.Lock()   # les compteurs ci-dessous
        self._names = threading.Lock()  # le choix d'un nom libre et sa publication : un nom pris par un `.part` ne se libère pas entre le contrôle et le renommage d'un autre envoi
        self._active = 0                # envois en cours
        self._pending = 0               # octets annoncés des envois en cours : deux envois qui arrivent ensemble ne dépassent pas l'espace à garder à eux deux

    # --- Chemins : tout nom de dossier ou de fichier passe par ici ---------------
    def _folder(self, account):
        """Le dossier d'un compte, ou None si `account` n'est pas un identifiant ou si ce dossier est un lien symbolique ou une jonction Windows (il mènerait hors de la boîte).
        Ni `resolve()` ni comparaison de chemins ici : sous Windows, `resolve()` échoue par moments quand le dossier est en pleine activité (envois simultanés) et rend alors un chemin d'une autre forme, d'où des refus à tort."""
        if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
            return None
        folder = self.root / account
        return None if folder.is_symlink() or folder.is_junction() else folder

    def _account_folders(self):
        return [folder for e in _entries(self.root) if e.is_dir() and (folder := self._folder(e.name)) is not None]

    def path_of(self, account, name):
        """Le fichier fini `name` du compte ; 404 pour tout le reste : un autre compte, un `.part`, un nom caché, un dossier, un lien, un chemin qui sort du dossier du compte.
        Le nom n'a ni séparateur ni point au début (`is_stored_name`) et le dossier n'est pas un lien (`_folder`) : le chemin ne peut pas sortir du dossier du compte."""
        folder = self._folder(account)
        if folder is None or not is_stored_name(name):
            raise InboxError(404, "fichier introuvable")
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise InboxError(404, "fichier introuvable")
        return path

    def remove(self, account, name):
        os.unlink(self.path_of(account, name))

    def listing(self, account=None):
        """Les fichiers finis, du plus récent au plus ancien : `{"account", "name", "size", "date"}`, ceux d'un compte ou de tous. Ni `.part`, ni dossier, ni lien."""
        folders = self._account_folders() if account is None else [f for f in [self._folder(account)] if f is not None]
        files = []
        for folder in folders:
            for entry in _entries(folder):
                info = _stat_file(entry) if is_stored_name(entry.name) else None
                if info is not None:
                    files.append({"account": folder.name, "name": entry.name, "size": info.st_size, "date": int(info.st_mtime)})
        return sorted(files, key=lambda f: (-f["date"], f["account"], f["name"]))

    def used(self, account):
        """Ce que les fichiers finis du compte occupent."""
        return sum(f["size"] for f in self.listing(account))

    # --- Réception ---------------------------------------------------------------
    @contextlib.contextmanager
    def admit(self, account, length):
        """Accepte (ou refuse) un envoi de `length` octets avant d'en lire un seul, et le compte comme en cours jusqu'à la sortie.
        Dans l'ordre : pas assez de place sur le disque (507), trop d'envois en même temps (429). Le dernier est le seul qui passe en réessayant."""
        if self._folder(account) is None:
            raise InboxError(400, "compte invalide")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if shutil.disk_usage(self.root).free - self._pending - length < self.keep_free:
                raise InboxError(507, "plus assez de place sur le serveur pour ce fichier")
            if self._active >= self.max_uploads:
                raise InboxError(429, "trop d'envois en cours : réessaie dans un moment")
            self._active += 1
            self._pending += length
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                self._pending -= length

    def _open_part(self, folder, name):
        """(nom retenu, fichier `.part` ouvert) : `name`, ou « stem (2).ext » s'il est pris, par un fichier ou par un envoi en cours. Le `.part` est créé sans écraser : s'il existe déjà, on passe au nom suivant."""
        stem, ext = os.path.splitext(name)
        with self._names:
            for number in range(1, SAME_NAME + 1):
                candidate = name if number == 1 else f"{stem} ({number}){ext}"
                if os.path.lexists(folder / candidate):
                    continue
                try:
                    return candidate, open(folder / (candidate + PART), "xb")
                except FileExistsError:
                    continue
        raise InboxError(409, "trop de fichiers du même nom")

    def _publish(self, folder, final):
        """Renomme `<final>.part` en `<final>` sans rien écraser. Sous le même verrou que `_open_part` : un autre envoi du même nom ne peut pas choisir « final » entre le contrôle et le renommage. 409 si un fichier de ce nom est apparu autrement (déposé à la main)."""
        with self._names:
            if os.path.lexists(folder / final):
                raise InboxError(409, "un fichier de ce nom est apparu pendant l'envoi : recommence")
            os.rename(folder / (final + PART), folder / final)

    @staticmethod
    def _copy(source, out, length):
        """Copie `length` octets de `source` par morceaux de CHUNK ; rend ce qui a été reçu (moins si le flux s'arrête). Une erreur de lecture (coupure, délai dépassé) sort telle quelle ; une erreur d'écriture est un 507."""
        got = 0
        while got < length:
            chunk = source.read(min(CHUNK, length - got))
            if not chunk:
                break
            try:
                out.write(chunk)
            except OSError:
                raise InboxError(507, "écriture impossible : le disque du serveur est peut-être plein")
            got += len(chunk)
        return got

    def receive(self, account, raw_name, source, length):
        """Range dans le dossier du compte les `length` octets de `source` sous un nom nettoyé, sans jamais écraser. Rend `{"name", "size"}`.
        Le fichier s'écrit dans `<nom>.part`, renommé quand tout est là ; si l'envoi échoue (flux trop court, coupure, disque plein) le `.part` est effacé. InboxError avant toute lecture quand l'espace manque ou que trop d'envois sont en cours."""
        name = clean_name(raw_name)
        with self.admit(account, length):
            folder = self._folder(account)
            folder.mkdir(exist_ok=True)
            final, out = self._open_part(folder, name)
            part, stored = folder / (final + PART), False
            try:
                with out:
                    got = self._copy(source, out, length)
                if got != length:
                    raise InboxError(400, f"envoi interrompu : {got} octets reçus sur {length}")
                self._publish(folder, final)
                stored = True
            finally:
                if not stored:
                    _discard(part)
        return {"name": final, "size": length}

    # --- Nettoyage ---------------------------------------------------------------
    def sweep(self, now=None):
        """Efface les `.part` vieux de plus de STALE secondes (un envoi abandonné par un arrêt brutal) ; rend leur nombre. Un envoi en cours écrit sans cesse : son `.part` est toujours récent."""
        limit = (time.time() if now is None else now) - STALE
        removed = 0
        for folder in self._account_folders():
            for entry in _entries(folder):
                info = _stat_file(entry) if entry.name.endswith(PART) else None
                if info is not None and info.st_mtime < limit:
                    removed += _discard(entry.path)
        return removed

    def _sweep_quietly(self):
        try:
            self.sweep()
        except OSError as e:
            print(f"boîte de réception : nettoyage impossible ({e})", file=sys.stderr)

    def keep_clean(self):
        """Nettoie tout de suite (ce qu'un arrêt brutal a laissé), puis une fois par jour, dans un fil."""
        self._sweep_quietly()

        def loop():
            while True:
                time.sleep(STALE)
                self._sweep_quietly()
        threading.Thread(target=loop, name="inbox-sweep", daemon=True).start()
