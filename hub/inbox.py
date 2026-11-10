"""La boîte de réception : les fichiers que des comptes autorisés envoient au NAS, rangés sous HUB_INBOX (par défaut HUB_DATA/inbox) dans un dossier par compte.

Le hub n'écrit jamais dans la médiathèque : un envoi n'entre dans aucune bibliothèque, l'administrateur le range lui-même. Un fichier déposé n'est jamais analysé (ni ffprobe ni scan) ; il n'est servi qu'à l'administrateur, en pièce jointe.

Le dossier d'un compte porte le NOM du compte, nettoyé (`clean_folder`) : c'est ce que l'administrateur lit sur un partage réseau. Le lien compte -> dossier est gardé dans la base (`users.inbox_dir`), fixé à la première utilisation et jamais recalculé :
un renommage, une collision de noms ou un compte supprimé ne donnent pas le dossier à un autre. Un compte supprimé laisse son dossier et ses fichiers en place. Les dossiers d'avant (nommés d'après l'identifiant du compte : `default`, ou hexadécimal) sont renommés au démarrage (`migrate`).
Le choix d'un nom libre se fait sous un verrou (`_names`), en comparant sans tenir compte de la casse (NTFS et SMB ne distinguent pas « Léa » de « léa ») et sans jamais écraser un dossier existant.

Un envoi peut aller dans un sous-dossier du dossier du compte (jusqu'à DEPTH niveaux, `clean_dir`) : le hub le crée quand un fichier y arrive, jamais vide, jamais en cas de refus, et le reprend si l'envoi échoue. Deux écritures qui ne diffèrent que par la casse (ou la forme Unicode) sont le MÊME dossier, jamais « (2) » : envoyer deux fois un dossier le fusionne, seuls les fichiers de même nom prennent un numéro.
`listing`, `used`, `sweep`, `path_of` et `remove` parcourent ces sous-dossiers sans jamais suivre un lien symbolique ni une jonction, et sans sortir du dossier du compte ; supprimer le dernier fichier d'un sous-dossier retire le dossier.

Ce module ne connaît pas HTTP : `receive` lit un flux (le corps de la requête) et tient les limites, `listing`, `path_of` et `remove` bornent tout chemin à la boîte puis au dossier du compte (le chemin est résolu puis vérifié, en plus des contrôles de nom). Le serveur (`server/app.py`) décide qui a le droit.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

CHUNK = 1 << 20                 # le corps est lu par morceaux de 1 Mo : jamais en mémoire d'un coup
PART = ".part"                  # un envoi en cours : renommé à la fin, jeté s'il échoue
STALE = 24 * 3600               # un .part plus vieux que cela est un envoi abandonné (arrêt brutal du serveur)
NAME_BYTES = 200                # longueur du nom, en octets UTF-8 : de la place pour « (2) » et « .part » sous la limite de 255 des systèmes de fichiers
SAME_NAME = 999                 # « Film (2).mkv », « Film (3).mkv »… jusque-là
FOLDER_BYTES = 120              # un nom de dossier de compte, en octets UTF-8 : de la place pour « (999) » sous la limite de 255 des systèmes de fichiers
FOLDER_FALLBACK = "compte"      # le nom d'un dossier dont le nom de compte ne laisse rien après nettoyage
DEPTH = 4                       # niveaux de sous-dossiers sous le dossier du compte : de quoi garder « Série/Saison 1/Disque 2/Bonus », pas de quoi bâtir une arborescence sans fond (chaque parcours la descend, et un chemin trop long gênerait les partages Windows)
MAX_SUBFOLDERS = 1000           # sous-dossiers d'un compte, tous niveaux : un envoi qui en créerait au-delà est refusé. Chaque dossier est une entrée de plus à lire à chaque liste, calcul d'espace et nettoyage ; sans plafond, un client (ou un bogue) en sèmerait sans fin avec des fichiers d'un octet.

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
    if _is_reserved(stem):
        stem = "_" + stem
    stem = stem.encode("utf-8")[:NAME_BYTES - len(ext.encode("utf-8"))].decode("utf-8", "ignore").rstrip(" .")
    return stem + ext


def is_stored_name(name):
    """Ce nom peut-il être celui d'un fichier fini de la boîte ? Le contraire de ce que `clean_name` ne produit jamais : séparateur, caractère douteux, point au début, point ou espace à la fin, `.part` (en toute casse : NTFS et SMB ouvrent `.PART` comme `.part`)."""
    try:
        size = len(name.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return False
    return 0 < size <= 255 and name == name.strip(" .") and not name.lower().endswith(PART) and all(_safe_char(c) for c in name)



def _is_reserved(name):
    return name.split(".")[0].rstrip().upper() in RESERVED


def clean_folder(raw):
    """Le nom du dossier d'un compte, d'après son nom : les mêmes règles de fond que `clean_name` (ni séparateur, ni caractère que Windows refuse, ni contrôle ; ni point ni espace au début ou à la fin, donc jamais caché ; un nom réservé de Windows
    préfixé par `_` ; normalisation NFC), mais sans extension à exiger, coupé à FOLDER_BYTES octets, et jamais vide : FOLDER_FALLBACK quand rien ne reste."""
    name = unicodedata.normalize("NFC", raw) if isinstance(raw, str) else ""
    name = "".join(c if _safe_char(c) else "_" for c in name).strip(" .")
    if name and _is_reserved(name):
        name = "_" + name
    name = name.encode("utf-8")[:FOLDER_BYTES].decode("utf-8", "ignore").rstrip(" .")
    return name or FOLDER_FALLBACK


def _clean_segment(raw):
    """Un segment de `clean_dir` : les règles de `clean_folder`, mais un segment qui ne laisse rien (vide, `.`, `..`, points et espaces) est refusé (400), jamais remplacé par un nom neutre."""
    name = unicodedata.normalize("NFC", raw)
    if not name.strip(" ."):
        raise InboxError(400, "segment de dossier vide, « . » ou « .. » refusé")
    return clean_folder(name)


def clean_dir(raw):
    """Le chemin relatif d'un sous-dossier (`?dir=`), en segments nettoyés : des noms séparés par `/`, chacun passé par `clean_folder`. Rien ou un texte vide : `()` (le dossier du compte). 400 en français pour un segment vide, `.` ou `..` (jamais ignoré),
    une barre oblique inverse (séparateur de Windows : refusée plutôt que changée en `_`, le client croirait envoyer deux niveaux), plus de DEPTH niveaux, ou autre chose qu'un texte. Le texte n'est pas décodé ici : `%2e%2e` est un nom de dossier ordinaire."""
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, str):
        raise InboxError(400, "chemin de dossier attendu sous forme de texte (?dir=…)")
    if "\\" in raw:
        raise InboxError(400, "« \\ » n'est pas un séparateur de dossiers : sépare-les par « / » (?dir=…)")
    segments = raw.split("/", DEPTH)                   # au plus DEPTH + 1 morceaux : un chemin énorme n'est pas découpé en entier
    if len(segments) > DEPTH:
        raise InboxError(400, f"dossiers trop profonds : {DEPTH} niveaux au plus")
    return tuple(_clean_segment(segment) for segment in segments)


def is_folder_name(name):
    """Ce nom peut-il être celui d'un dossier de la boîte ? Un seul élément de chemin, sans caractère douteux, sans point ni espace au début ou à la fin (donc ni `.`, ni `..`, ni nom caché), pas un nom réservé de Windows, 255 octets au plus.
    Les noms que `clean_folder` produit, ceux d'avant (identifiants) et un dossier posé à la main : jamais un chemin."""
    try:
        size = len(name.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return False
    return 0 < size <= 255 and name == name.strip(" .") and all(_safe_char(c) for c in name) and not _is_reserved(name)


def _same(name):
    """La clé qui compare des noms de dossier comme NTFS et SMB le font : sans tenir compte de la casse ni de la forme Unicode."""
    return unicodedata.normalize("NFC", name).casefold()


def _overlap(box, other):
    """Comment la boîte `box` se place par rapport à `other`, chemins résolus et casse de la plate-forme : "same" (le même dossier), "inside_it" (`other` est dans la boîte), "inside" (la boîte est dans `other`), ou "" (rien en commun).
    Un échec de résolution ou deux volumes distincts : "" (le chemin ne peut pas recouper)."""
    try:
        one, two = os.path.normcase(str(box.resolve())), os.path.normcase(str(other.resolve()))
        common = os.path.commonpath([one, two])
    except (OSError, RuntimeError, ValueError):
        return ""
    if one == two:
        return "same"
    return "inside_it" if common == one else "inside" if common == two else ""


def _is_mount(path):
    """Vrai si `path` est un point de montage, faux si c'est un simple dossier ; None quand la plate-forme ne permet pas de le savoir (Windows : seule la racine d'un lecteur en est un) ou que le système échoue."""
    if os.name == "nt":
        return None
    try:
        return os.path.ismount(path)
    except OSError:
        return None


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
    """Les limites viennent de la configuration : espace libre à garder sur le volume de la boîte (octets) et envois simultanés. Ni taille par fichier ni quota par compte, choix de l'utilisateur : seul le disque limite. Les attributs se lisent à chaque envoi.
    `db` est la base du hub : le lien compte -> dossier y est gardé (`users.inbox_dir`)."""

    def __init__(self, cfg, db):
        self.data, self.libraries = getattr(cfg, "data", None), getattr(cfg, "libraries", None) or ()      # HUB_DATA et les bibliothèques : `check` y compare la boîte
        self.root = Path(cfg.inbox)     # HUB_INBOX : peut être un autre volume que HUB_DATA ; l'espace libre se mesure et les `.part` se balaient ici
        self.db = db
        self.keep_free, self.max_uploads = cfg.inbox_keep_free, cfg.inbox_max_uploads
        self._lock = threading.Lock()   # les compteurs ci-dessous
        self._names = threading.RLock() # le choix d'un nom libre (de dossier ou de fichier) et sa publication : un nom pris par un `.part` ne se libère pas entre le contrôle et le renommage d'un autre envoi, deux comptes ne choisissent pas le même dossier.
                                        # Aussi la création et le retrait des sous-dossiers : un dossier retrouvé vide n'est pas retiré pendant qu'un envoi s'apprête à y poser son `.part`. Réentrant : `_stage` tient le verrou autour de `_open_part`, qui le prend aussi
        self._active = 0                # envois en cours
        self._pending = 0               # octets annoncés des envois en cours : deux envois qui arrivent ensemble ne dépassent pas l'espace à garder à eux deux

    # --- Le dossier d'un compte : son nom, fixé une fois dans la base --------------
    def folder_of(self, uid):
        """Le nom du dossier du compte, ou None s'il n'en a pas encore (rien envoyé) ou si le compte n'existe pas."""
        row = self.db.one("SELECT inbox_dir FROM users WHERE id=?", (uid,))
        return (row["inbox_dir"] or None) if row else None

    def owners(self):
        """{nom de dossier: nom du compte} des comptes qui existent. Un dossier absent de cette table est celui d'un compte supprimé."""
        return {r["inbox_dir"]: r["name"] for r in self.db.query("SELECT name, inbox_dir FROM users WHERE inbox_dir IS NOT NULL AND inbox_dir <> ''")}

    def _taken(self):
        """Les noms de dossier pris, sous la forme de `_same` : ceux qui existent sur le disque (d'un compte, d'un compte supprimé, posés à la main) et ceux que la base attribue."""
        taken = {_same(e.name) for e in _entries(self.root)}
        taken.update(_same(r["inbox_dir"]) for r in self.db.query("SELECT inbox_dir FROM users WHERE inbox_dir IS NOT NULL AND inbox_dir <> ''"))
        return taken

    @staticmethod
    def _numbered(base):
        """`base`, puis « base (2) », « base (3) »… jusqu'à SAME_NAME."""
        yield base
        for number in range(2, SAME_NAME + 1):
            yield f"{base} ({number})"

    def _unusable(self, what, error):
        """Le journal dit quoi (avec le chemin), le client lit un message sans chemin : 503."""
        print(f"boîte de réception : {what} ({error})", file=sys.stderr)
        return InboxError(503, "la boîte de réception du serveur est inutilisable : préviens l'administrateur")

    def _usable(self):
        """Crée la boîte si elle manque ; InboxError 503 si elle n'existe pas et ne peut pas être créée."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise self._unusable(f"{self.root} introuvable et impossible à créer", e)

    def folder_for(self, uid):
        """Le nom du dossier du compte, créé à la première utilisation. Sous le verrou des noms : deux envois d'un même compte, ou de deux comptes, ne se partagent jamais un dossier.
        Un dossier nommé d'après l'identifiant (celui d'avant ce changement, que `migrate` n'a pas encore renommé) est repris tel quel. Sinon : le nom du compte nettoyé, avec « (2) »… s'il est pris à la casse près. 400 pour un compte inconnu."""
        with self._names:
            row = self.db.one("SELECT name, inbox_dir FROM users WHERE id=?", (uid,))
            if row is None:
                raise InboxError(400, "compte invalide")
            self._usable()
            current = row["inbox_dir"]
            if current and is_folder_name(current):
                self._mkdir(current)
                return current
            stale = current or ""                  # une valeur qui n'est pas un nom de dossier (base touchée à la main) : le compte est traité comme neuf, et la valeur est réécrite avant tout dossier
            if self._old_folder_is_his(uid):
                return self._record(uid, uid, stale)
            return self._new_folder(uid, row["name"], stale)

    def _old_folder_is_his(self, uid):
        """Existe-t-il un dossier nommé d'après l'identifiant `uid` (celui d'avant), et aucun autre compte ne le tient-il dans la base ?"""
        return is_folder_name(uid) and self._is_real_folder(self.root / uid) and not self._held_by_other(uid, uid)

    def _new_folder(self, uid, account_name, stale):
        """Le premier nom libre (« nom », « nom (2) »…) devient le dossier du compte : créé, puis fixé dans la base. 409 si SAME_NAME noms sont pris."""
        taken = self._taken()
        for candidate in self._numbered(clean_folder(account_name)):
            if _same(candidate) not in taken and self._created(candidate):
                return self._keep(uid, candidate, stale)
        raise InboxError(409, "trop de dossiers de même nom dans la boîte")

    def _created(self, name):
        """Crée le dossier `name` de la boîte : vrai s'il est à nous, faux s'il existait (un autre l'a pris entre-temps) ; 503 si le système refuse."""
        try:
            (self.root / name).mkdir()
        except FileExistsError:
            return False
        except OSError as e:
            raise self._unusable(f"dossier « {name} » impossible à créer", e)
        return True

    def _keep(self, uid, candidate, stale):
        """Fixe `candidate` dans la base ; rend le nom retenu. Si la base en garde un autre, le dossier qu'on vient de faire ne sert à personne : il est retiré (vide), et l'autre est repris."""
        try:
            kept = self._record(uid, candidate, stale)
        except Exception:
            self._remove_empty(candidate)
            raise
        if kept != candidate:
            self._remove_empty(candidate)
            if not is_folder_name(kept):
                raise self._unusable(f"le dossier « {kept} » de la base n'est pas un nom de dossier", "base modifiée pendant l'envoi")
            self._mkdir(kept)
        return kept

    def _remove_empty(self, name):
        """Supprime le dossier `name` de la boîte s'il est vide (celui qu'on vient de créer) ; jamais un dossier qui contient quelque chose."""
        with contextlib.suppress(OSError):
            (self.root / name).rmdir()

    def _held_by_other(self, uid, name):
        """Un autre compte que `uid` tient-il déjà le dossier `name` dans la base (à la casse et à la forme Unicode près) ? Alors ce dossier n'est pas à `uid`, même s'il porte son identifiant."""
        rows = self.db.query("SELECT inbox_dir FROM users WHERE id<>? AND inbox_dir IS NOT NULL AND inbox_dir<>''", (uid,))
        return any(_same(r["inbox_dir"]) == _same(name) for r in rows)

    def _mkdir(self, name):
        """Le dossier d'un compte qui en a déjà un nom : il est recréé s'il a disparu (effacé à la main)."""
        try:
            (self.root / name).mkdir(exist_ok=True)
        except OSError as e:
            raise self._unusable(f"dossier « {name} » impossible à créer", e)

    def _record(self, uid, name, stale=""):
        """Fixe le dossier du compte dans la base, sauf s'il en a déjà un (`stale`, une valeur qui n'était pas un nom de dossier, est remplacée) ; rend le nom retenu."""
        self.db.execute("UPDATE users SET inbox_dir=? WHERE id=? AND (inbox_dir IS NULL OR inbox_dir='' OR inbox_dir=?)", (name, uid, stale))
        return self.folder_of(uid) or name

    @staticmethod
    def _is_real_folder(path):
        return path.is_dir() and not path.is_symlink() and not path.is_junction()

    # --- Chemins : tout nom de dossier ou de fichier passe par ici ---------------
    @staticmethod
    def _inside(path, parent):
        """`path` résolu (liens et jonctions suivis) reste-t-il sous `parent` résolu ? Un lien, une jonction ou un `..` caché qui mènerait hors de la boîte est refusé ici, en plus des contrôles de nom. La comparaison est celle de la plate-forme (casse ignorée sous Windows) ; une résolution qui échoue refuse."""
        try:
            real, wanted = os.path.normcase(str(path.resolve())), os.path.normcase(str(parent.resolve()))
            return os.path.commonpath([real, wanted]) == wanted
        except (OSError, RuntimeError, ValueError):
            return False

    def _folder(self, name):
        """Le dossier `name` de la boîte, ou None : ce n'est pas un nom de dossier (séparateur, `..`, caché, réservé, octet nul…), le dossier n'existe pas, c'est un lien symbolique ou une jonction Windows, ou il sort de la boîte une fois résolu."""
        if not isinstance(name, str) or not is_folder_name(name):
            return None
        folder = self.root / name
        try:
            if not self._is_real_folder(folder) or not self._inside(folder, self.root):
                return None
        except (OSError, ValueError):
            return None
        return folder

    def _account_folders(self):
        return [folder for e in _entries(self.root) if e.is_dir() and (folder := self._folder(e.name)) is not None]

    def _confined(self, path, account):
        """`path` résolu reste-t-il sous la boîte ET sous le dossier du compte `account` ? Le second contrôle compte : un lien ou une jonction que les contrôles de nom ne voient pas pourrait mener au dossier d'un AUTRE compte, qui est bien sous la boîte."""
        return self._inside(path, self.root) and self._inside(path, account)

    def _is_subfolder(self, entry, account):
        """Une entrée du dossier d'un compte que l'on descend : un vrai dossier (ni lien ni jonction) au nom de dossier valable (donc ni caché ni réservé), qui reste sous le dossier du compte."""
        path = Path(entry.path)
        return is_folder_name(entry.name) and self._is_real_folder(path) and self._confined(path, account)

    def _tree(self, account):
        """`(dossier, chemin relatif en segments, entrées)` du dossier du compte puis de chacun de ses sous-dossiers, jusqu'à DEPTH niveaux. Aucun lien ni jonction n'est descendu : le parcours ne sort jamais du dossier du compte, et ne boucle pas."""
        pending = [(account, ())]
        while pending:
            folder, relative = pending.pop()
            entries = _entries(folder)
            yield folder, relative, entries
            if len(relative) < DEPTH:
                pending.extend((Path(e.path), relative + (e.name,)) for e in entries if self._is_subfolder(e, account))

    def _read_dir(self, sub):
        """Les segments d'un `?dir=` pour LIRE : chacun doit déjà être un nom de dossier tel que la liste le donne (pas de nettoyage : on ne devine pas un dossier), DEPTH au plus ; 404 sinon, comme pour tout chemin qui ne mène à rien."""
        if sub is None or sub == "":
            return ()
        segments = sub.split("/", DEPTH) if isinstance(sub, str) else None
        if segments is None or len(segments) > DEPTH or not all(is_folder_name(segment) for segment in segments):
            raise InboxError(404, "fichier introuvable")
        return tuple(segments)

    def _locate(self, folder_name, sub):
        """`(dossier du compte, dossier du fichier)` ; 404 pour un compte inconnu, un chemin invalide, un dossier absent ou que la liste ne donne pas sous ce nom (un nom court 8.3), un lien, une jonction ou un chemin qui sort du dossier du compte."""
        account = self._folder(folder_name)
        parts = self._read_dir(sub)
        if account is None:
            raise InboxError(404, "fichier introuvable")
        folder = account
        for segment in parts:
            listed = any(_same(e.name) == _same(segment) for e in _entries(folder))       # un nom que la liste donne (à la casse près), pas un alias que le système ouvre aussi : le nom court 8.3
            folder = folder / segment
            if not listed or not self._is_real_folder(folder) or not self._confined(folder, account):
                raise InboxError(404, "fichier introuvable")
        return account, folder

    def path_of(self, folder_name, name, sub=None):
        """Le fichier fini `name` du dossier `folder_name` (et de son sous-dossier `sub`, des segments séparés par `/`) ; 404 pour tout le reste : un autre dossier, un `.part`, un nom caché, un dossier, un lien, un chemin qui sort de la boîte ou du dossier du compte.
        Les noms n'ont ni séparateur ni point au début (`is_folder_name`, `is_stored_name`), aucun dossier n'est un lien (`_locate`), le nom doit être celui d'une entrée de la liste du dossier (comme `_locate` pour les dossiers : pas d'alias 8.3), et le fichier résolu doit rester sous le dossier du compte résolu."""
        account, folder = self._locate(folder_name, sub)
        if not is_stored_name(name):
            raise InboxError(404, "fichier introuvable")
        path = folder / name
        listed = any(_same(e.name) == _same(name) and is_stored_name(e.name) and _stat_file(e) is not None for e in _entries(folder))       # un nom que la liste donne, pas un alias que le système ouvre aussi (le nom court 8.3 d'un `.part` ou d'un fichier fini)
        if not listed or path.is_symlink() or not path.is_file() or not self._confined(path, account):
            raise InboxError(404, "fichier introuvable")
        return path

    def remove(self, folder_name, name, sub=None):
        """Supprime le fichier ; le sous-dossier qu'il laisse vide part avec lui (et ses parents devenus vides), jamais le dossier du compte ni un dossier qui contient encore quelque chose (même un `.part`)."""
        path = self.path_of(folder_name, name, sub)
        try:
            os.unlink(path)
        except FileNotFoundError:
            raise InboxError(404, "fichier introuvable")
        self._prune([path.parent], self._folder(folder_name), climb=True)

    def _prune(self, folders, account, climb):
        """Retire de `folders` ceux qui sont vides, du plus profond au plus haut, sous le verrou des noms ; avec `climb`, remonte aussi aux parents devenus vides. S'arrête avant le dossier du compte, qui reste toujours."""
        if account is None:
            return
        with self._names:
            for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
                while folder != account and account in folder.parents:
                    try:
                        folder.rmdir()
                    except OSError:                       # il n'est pas vide (ou a disparu) : on le laisse
                        break
                    folder = folder.parent if climb else account

    def _files_of(self, account):
        for folder, relative, entries in self._tree(account):
            for entry in entries:
                info = _stat_file(entry) if is_stored_name(entry.name) else None
                if info is not None:
                    yield {"account": account.name, "dir": "/".join(relative), "name": entry.name, "size": info.st_size, "date": int(info.st_mtime)}

    def listing(self, uid=None):
        """Les fichiers finis, du plus récent au plus ancien : `{"account", "dir", "name", "size", "date"}` (`account` : le nom du dossier du compte ; `dir` : le chemin relatif du sous-dossier, `""` à la racine), ceux du compte `uid` ou de tous,
        sous-dossiers compris. Ni `.part`, ni dossier, ni lien."""
        if uid is None:
            accounts = self._account_folders()
        else:
            name = self.folder_of(uid)
            accounts = [account for account in [self._folder(name)] if account is not None] if name else []
        files = [f for account in accounts for f in self._files_of(account)]
        return sorted(files, key=lambda f: (-f["date"], f["account"], f["dir"], f["name"]))

    def used(self, uid):
        """Ce que les fichiers finis du compte occupent, sous-dossiers compris."""
        return sum(f["size"] for f in self.listing(uid))

    # --- Sous-dossiers à la réception ---------------------------------------------
    def _child(self, account, parent, segment):
        """Le sous-dossier de `parent` qui se confond avec `segment` (casse et forme Unicode ignorées), ou None s'il n'y en a pas. 409 si ce nom est pris par un fichier, un lien ou une jonction (jamais écrit à travers) ; 503 si le dossier mène hors du dossier du compte."""
        key = _same(segment)
        for entry in _entries(parent):
            if _same(entry.name) != key:
                continue
            path = Path(entry.path)
            if not (is_folder_name(entry.name) and self._is_real_folder(path)):
                print(f"boîte de réception : « {path} » n'est pas un dossier ordinaire (fichier, lien ou jonction)", file=sys.stderr)
                raise InboxError(409, f"« {segment} » : un fichier ou un lien porte déjà ce nom")
            if not self._confined(path, account):
                raise self._unusable(f"le dossier « {path} » sort du dossier du compte", "lien ou jonction non reconnu")
            return path
        return None

    def _existing(self, account, parts):
        """`(le dossier existant le plus profond du chemin, les segments qui restent à créer)`."""
        folder = account
        for index, segment in enumerate(parts):
            found = self._child(account, folder, segment)
            if found is None:
                return folder, parts[index:]
            folder = found
        return folder, ()

    def _room_for(self, account, new):
        """409 si créer `new` sous-dossiers de plus porterait le compte au-delà de MAX_SUBFOLDERS. Un dossier déjà là n'est pas concerné (on ne passe ici que pour en créer)."""
        have = sum(1 for _ in self._tree(account)) - 1          # le dossier du compte lui-même ne compte pas
        if have + new > MAX_SUBFOLDERS:
            raise InboxError(409, f"trop de sous-dossiers dans ton dossier ({MAX_SUBFOLDERS} au plus)")

    def _twin(self, account, path):
        """Le dossier ordinaire de la boîte que `path` désigne déjà sans le même nom que dans la liste (nom court 8.3 de Windows : `_child`, qui compare les noms, ne le voit pas), ou None. Même contrôle que `_child` : ni fichier, ni lien, ni jonction, et sous le dossier du compte."""
        for entry in _entries(path.parent):
            found = Path(entry.path)
            try:
                if os.path.samefile(found, path):
                    return found if is_folder_name(entry.name) and self._is_real_folder(found) and self._confined(found, account) else None
            except OSError:
                continue
        return None

    def _make_folders(self, account, parent, names, touched):
        """Crée `names` l'un dans l'autre sous `parent`, chacun consigné dans `touched` pour être repris si l'envoi échoue. Un nom qui existe déjà sans que `_child` l'ait vu est l'alias court d'un vrai dossier : on y entre (sous son vrai nom, rien de créé) ; 409 si c'est autre chose."""
        for name in names:
            parent = parent / name
            try:
                parent.mkdir()
            except FileExistsError:
                twin = self._twin(account, parent)
                if twin is None:
                    raise InboxError(409, f"« {name} » existe déjà sous un autre nom ou n'est pas un dossier ordinaire : choisis un autre nom")
                parent = twin
            except OSError as e:
                raise self._unusable(f"dossier « {parent} » impossible à créer", e)
            else:
                touched.append(parent)
        return parent

    def _stage(self, account, parts, name, touched):
        """`(dossier, nom retenu, fichier .part ouvert)` : le dossier du chemin `parts` sous celui du compte, créé s'il manque (plafond tenu avant d'en créer un), et le `.part` ouvert dedans. Tout sous le verrou des noms : ni deux envois qui créent le même dossier,
        ni un dossier retiré (vide) entre sa création et le `.part`. Le dossier final est vérifié sous la boîte et sous le dossier du compte. `touched` reçoit les dossiers créés et le dossier cible, même déjà là : si l'envoi échoue, `receive` les retire s'ils sont vides, car l'envoi qui a créé le dossier peut avoir échoué avant celui-ci."""
        with self._names:
            folder, missing = self._existing(account, parts)
            if missing:
                self._room_for(account, len(missing))
                folder = self._make_folders(account, folder, missing, touched)
            if not self._confined(folder, account):
                raise self._unusable(f"le dossier « {folder} » sort du dossier du compte", "lien ou jonction non reconnu")
            touched.append(folder)
            final, out = self._open_part(folder, name)
        return folder, final, out

    # --- Démarrage : vérifier la boîte, migrer les anciens dossiers --------------
    def check(self):
        """Au démarrage : la boîte existe (créée au besoin), le serveur peut y écrire, elle ne recoupe ni HUB_DATA ni une bibliothèque, et, donnée hors de HUB_DATA, c'est bien un volume monté. None si tout va bien, sinon le message à montrer à l'administrateur (`announce` y met « ATTENTION : ») : un chemin non inscriptible ou mal choisi se dit au démarrage, pas au premier envoi."""
        found = self._overlaps()
        if self._in_library():
            return " ".join(found)          # le hub n'écrit jamais dans la médiathèque : ni dossier ni fichier d'essai tant que la boîte la recoupe
        problems = []
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self.root):
                pass
        except OSError as e:
            problems.append(f"la boîte de réception ({self.root}) n'est pas utilisable : {e.strerror or e} ; les envois seront refusés (vérifie HUB_INBOX et les droits d'écriture du conteneur).")
        if self._unmounted():
            problems.append(f"HUB_INBOX ({self.root}) est donné hors de HUB_DATA mais n'est pas un point de montage : si le dataset n'est pas monté sur ce chemin dans le yaml, les envois s'écrivent dans le conteneur lui-même et seront perdus au redéploiement. "
                            "(Un sous-dossier d'un volume monté est normal : ignore ce message.)")
        problems.extend(found)
        return " ".join(problems) or None

    def _unmounted(self):
        """Vrai si la boîte est hors de HUB_DATA (donc donnée par HUB_INBOX : le défaut y est toujours) et n'est pas un point de montage. Faux quand on ne sait pas (pas de HUB_DATA connu, Windows)."""
        if self.data is None or _overlap(self.root, Path(self.data)):
            return False
        return _is_mount(self.root) is False

    def _in_library(self):
        """Vrai si la boîte est une bibliothèque, en contient une ou est dans l'une d'elles."""
        return any(_overlap(self.root, Path(lib.path)) for lib in self.libraries)

    def _overlaps(self):
        """Les messages d'une boîte qui est (ou contient, ou est dans) HUB_DATA ou une bibliothèque. Le défaut `HUB_DATA/inbox` (la boîte DANS les données) est normal et ne dit rien."""
        found = []
        if self.data is not None and _overlap(self.root, Path(self.data)) in ("same", "inside_it"):
            found.append(f"HUB_INBOX ({self.root}) est le dossier de données (HUB_DATA) ou en contient : hub.db, img/ et posters/ y seraient pris pour des dossiers de compte et leur contenu pour des envois, "
                         "listés et supprimables par l'administrateur. Choisis un dossier dédié (HUB_DATA/inbox par défaut, ou un dossier à côté).")
        for lib in self.libraries:
            if _overlap(self.root, Path(lib.path)):
                label = getattr(lib, "label", None) or Path(lib.path).name
                found.append(f"HUB_INBOX ({self.root}) recoupe la bibliothèque « {label} » ({lib.path}) : les envois se mêleraient à la médiathèque (ou la boîte la contiendrait), que le hub ne doit jamais modifier. Choisis un dossier hors des bibliothèques.")
        return found

    def migrate(self):
        """Au démarrage : le dossier d'un compte nommé d'après son identifiant (`default`, ou hexadécimal : celui d'avant) est renommé d'après le nom du compte. Rend le nombre de dossiers renommés.
        Rien n'est écrasé ni perdu (le nom libre est choisi comme pour un nouveau dossier), les `.part` en cours partent avec leur dossier, et relancer ne change rien. Un dossier qui ne peut pas être renommé (ouvert ailleurs, droits) reste en place sous son ancien nom,
        gardé comme tel dans la base, et le démarrage continue : la prochaine fois sera la bonne. Un dossier dont le compte n'existe plus n'est pas touché."""
        try:
            users = self.db.query("SELECT id, name FROM users")
        except Exception as e:
            print(f"boîte de réception : migration impossible ({e})", file=sys.stderr)
            return 0
        done = 0
        for row in users:
            try:
                done += self._migrate_one(row["id"], row["name"])
            except Exception as e:           # un dossier qui résiste ne doit pas empêcher le démarrage, ni les autres comptes
                print(f"boîte de réception : le dossier du compte « {row['name']} » garde son ancien nom ({e})", file=sys.stderr)
        return done

    def _migrate_one(self, uid, account_name):
        with self._names:
            if not self._old_folder_to_rename(uid):
                return 0                              # déjà renommé, rien d'ancien à renommer, ou ce dossier est celui d'un autre compte
            if self.folder_of(uid) is None:
                self._record(uid, uid)               # l'ancien dossier est déjà celui du compte, tel quel : si la suite échoue, la base le dit
            target = self._target_name(uid, account_name)
            if target == uid:
                return 0
            self._rename_folder(uid, target)
            return 1

    def _old_folder_to_rename(self, uid):
        """Le dossier nommé d'après l'identifiant `uid` est-il à renommer ? Non s'il l'a déjà été (la base a un autre nom), s'il n'existe pas (ou est un lien, une jonction), ou si un autre compte, nommé comme cet identifiant, le tient."""
        current = self.folder_of(uid)
        if (current is not None and current != uid) or not is_folder_name(uid) or not self._is_real_folder(self.root / uid):
            return False
        return not self._held_by_other(uid, uid)

    def _target_name(self, uid, account_name):
        """Le nom libre du compte (choisi comme pour un nouveau dossier), l'ancien dossier `uid` ne comptant pas comme pris. 409 si SAME_NAME noms sont pris."""
        taken = self._taken() - {_same(uid)}
        target = next((c for c in self._numbered(clean_folder(account_name)) if _same(c) not in taken), None)
        if target is None:
            raise InboxError(409, "trop de dossiers de même nom dans la boîte")
        return target

    def _rename_folder(self, uid, target):
        """Renomme le dossier de l'ancien nom (l'identifiant `uid`) en `target`, puis la base ; si la base échoue, le dossier reprend son ancien nom (la base garde l'ancien nom : le dossier aussi)."""
        old, new = self.root / uid, self.root / target
        if os.path.lexists(new) and not (_same(target) == _same(uid) and os.path.samefile(new, old)):      # jamais d'écrasement : sous POSIX, renommer un dossier sur un dossier vide le remplacerait
            raise InboxError(409, f"« {target} » existe déjà")
        os.rename(old, new)
        try:
            self.db.execute("UPDATE users SET inbox_dir=? WHERE id=?", (target, uid))
        except Exception:
            with contextlib.suppress(OSError):
                os.rename(new, old)
            raise

    # --- Réception ---------------------------------------------------------------
    @contextlib.contextmanager
    def admit(self, uid, length):
        """Accepte (ou refuse) un envoi de `length` octets avant d'en lire un seul, et le compte comme en cours jusqu'à la sortie.
        Dans l'ordre : compte inconnu (400), boîte inutilisable (503), pas assez de place sur le volume de la boîte (507), trop d'envois en même temps (429). Le dernier est le seul qui passe en réessayant."""
        if self.db.one("SELECT 1 FROM users WHERE id=?", (uid,)) is None:
            raise InboxError(400, "compte invalide")
        self._usable()
        with self._lock:
            try:
                free = shutil.disk_usage(self.root).free
            except OSError as e:
                raise self._unusable(f"espace libre illisible sur {self.root}", e)
            if free - self._pending - length < self.keep_free:
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
                except OSError as e:               # droits, volume en lecture seule, nom refusé… : le chemin va au journal, jamais au client
                    raise self._unusable(f"« {candidate + PART} » impossible à créer dans {folder}", e)
        raise InboxError(409, "trop de fichiers du même nom")

    def _publish(self, folder, final):
        """Renomme `<final>.part` en `<final>` sans rien écraser. Sous le même verrou que `_open_part` : un autre envoi du même nom ne peut pas choisir « final » entre le contrôle et le renommage. 409 si un fichier de ce nom est apparu autrement (déposé à la main)."""
        with self._names:
            if os.path.lexists(folder / final):
                raise InboxError(409, "un fichier de ce nom est apparu pendant l'envoi : recommence")
            try:
                os.rename(folder / (final + PART), folder / final)
            except FileExistsError:
                raise
            except OSError as e:
                raise self._unusable(f"« {final + PART} » impossible à renommer dans {folder}", e)

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

    def _write(self, folder, final, out, source, length):
        """Copie le corps dans `<final>.part` puis le publie ; le `.part` est effacé si l'envoi échoue."""
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

    def receive(self, uid, raw_name, source, length, raw_dir=None):
        """Range dans le dossier du compte `uid` (ou son sous-dossier `raw_dir`, voir `clean_dir`) les `length` octets de `source` sous un nom nettoyé, sans jamais écraser. Rend `{"name", "size", "account", "dir"}` (`account` : le nom du dossier du compte ; `dir` : le sous-dossier retenu, `""` à la racine).
        Le nom et le chemin sont vérifiés avant tout dossier : un refus (nom, extension, espace, envois en cours, plafond) n'en crée aucun. Le fichier s'écrit dans `<nom>.part`, renommé quand tout est là ; si l'envoi échoue (flux trop court, coupure, disque plein) le `.part` est effacé
        et le sous-dossier cible est retiré s'il est vide (avec ses parents devenus vides, comme `remove`), qu'il ait été créé par cet envoi ou par un autre qui a échoué avant lui ; un dossier qui contient encore quelque chose n'est jamais touché. InboxError avant toute lecture quand l'espace manque ou que trop d'envois sont en cours."""
        name, parts = clean_name(raw_name), clean_dir(raw_dir)
        with self.admit(uid, length):
            folder_name = self.folder_for(uid)
            account = self._folder(folder_name)
            if account is None:                 # un lien ou une jonction à la place du dossier, ou un dossier hors de la boîte : jamais écrire par là
                raise self._unusable(f"le dossier « {folder_name} » n'est pas un dossier de la boîte", "lien, jonction ou dossier absent")
            touched, stored = [], False
            try:
                folder, final, out = self._stage(account, parts, name, touched)
                self._write(folder, final, out, source, length)
                stored = True
            finally:
                if not stored:
                    self._prune(touched, account, climb=True)
        return {"name": final, "size": length, "account": folder_name, "dir": "/".join(folder.relative_to(account).parts)}

    # --- Nettoyage ---------------------------------------------------------------
    def sweep(self, now=None):
        """Efface, dans la boîte (HUB_INBOX, son volume), les `.part` vieux de plus de STALE secondes (un envoi abandonné par un arrêt brutal), sous-dossiers compris ; rend leur nombre. Un sous-dossier que cela laisse vide est retiré. Un envoi en cours écrit sans cesse : son `.part` est toujours récent."""
        limit = (time.time() if now is None else now) - STALE
        removed = 0
        for account in self._account_folders():
            emptied = []
            for folder, _, entries in self._tree(account):
                gone = sum(_discard(e.path) for e in entries if e.name.endswith(PART) and (info := _stat_file(e)) is not None and info.st_mtime < limit)
                removed += gone
                if gone:
                    emptied.append(folder)
            self._prune(emptied, account, climb=True)
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
