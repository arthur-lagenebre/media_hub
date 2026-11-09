"""Réglages du serveur : variables d'environnement, et la liste des bibliothèques.

Tout a une valeur par défaut qui marche sur le NAS (dossiers montés sous /srv) ; rien n'est obligatoire.

    HUB_SRV        racine des bibliothèques                       /srv
    HUB_LIBRARIES  fichier JSON qui décrit les bibliothèques      (sinon : les dossiers connus sous HUB_SRV)
    HUB_DATA       base SQLite et cache d'images                  ./data
    HUB_REPO       dépôt media_library (ses lecteurs de tags)     ../media_library, /app/media_library
    HUB_BIND       adresse d'écoute                               127.0.0.1
    HUB_PORT       port                                           8080
    HUB_PASSWORD   mot de passe du premier compte (amorçage)      (aucun : mode ouvert, sans compte)
    HUB_ADMIN      nom de ce premier compte (administrateur)      admin
    HUB_FFMPEG     chemin de ffmpeg / ffprobe                     cherchés dans le PATH, puis HUB_DATA/bin
    HUB_MAX_STREAMS   transcodages simultanés                     2
    HUB_AUTOSCAN_HOURS  rescan périodique (0 = jamais)            0
    HUB_INBOX_KEEP_FREE  espace libre à garder sous HUB_DATA, en Mo     10240
    HUB_INBOX_MAX_UPLOADS  envois simultanés (429 au-delà)              2
    TMDB_KEY       clé TMDB, pour enrichir les séries             (aussi lue dans le .env de HUB_REPO)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("movies", "shows", "music", "books")

# (id, titre affiché, genre, noms de dossier reconnus sous HUB_SRV)
DEFAULTS = (
    ("films", "Films", "movies", ("Films", "Movies")),
    ("series", "Séries", "shows", ("Series", "Séries", "TV_Shows")),
    ("animations", "Animations", "shows", ("Animations", "Anime", "Animation")),
    ("musique", "Musique", "music", ("Musique", "Music")),
    ("livres", "Livres", "books", ("Livres", "Books")),
)


@dataclass(frozen=True)
class Library:
    id: str
    label: str
    kind: str
    path: Path


@dataclass
class Config:
    libraries: list = field(default_factory=list)
    data: Path = Path("data")
    repo: Path | None = None
    bind: str = "127.0.0.1"
    port: int = 8080
    password: str = ""          # amorçage seulement : sert à créer le compte administrateur tant qu'aucun compte n'existe (voir accounts.py)
    admin: str = "admin"
    ffmpeg: str = ""
    ffprobe: str = ""
    max_streams: int = 2
    autoscan_hours: float = 0
    tmdb_key: str = ""
    inbox_keep_free: int = 10240 << 20    # octets (les variables HUB_INBOX_* se donnent en Mo)
    inbox_max_uploads: int = 2
    web: Path = Path(__file__).resolve().parent / "web"

    def library(self, lib_id):
        return next((lib for lib in self.libraries if lib.id == lib_id), None)


def _find_repo(env):
    here = Path(__file__).resolve().parent.parent
    candidates = [env.get("HUB_REPO"), here.parent / "media_library", "/app/media_library", here / "media_library"]
    for candidate in candidates:
        if candidate and (Path(candidate) / "libraries").is_dir():
            return Path(candidate)
    return None


def _first_file(*candidates):
    return next((str(path) for path in candidates if path.is_file()), "")


def _explicit_tool(name, given):
    """HUB_FFMPEG : le fichier lui-même (ou, s'il porte le nom de l'autre outil, son voisin), ou le dossier qui le contient."""
    if given.is_file():
        return str(given) if given.name.startswith(name) else _first_file(given.parent / (name + given.suffix))
    return _first_file(given / name, given / f"{name}.exe")


def _find_tool(name, env, data):
    explicit = env.get("HUB_FFMPEG")
    return (explicit and _explicit_tool(name, Path(explicit))) or shutil.which(name) or _first_file(data / "bin" / name, data / "bin" / f"{name}.exe")


def _read_dotenv_key(repo):
    if not repo:
        return ""
    for folder in (repo, repo.parent):
        try:
            for line in (folder / ".env").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "TMDB_KEY" and value.strip():
                    return value.strip().strip("'\"")
        except OSError:
            continue
    return ""


def _declared(item):
    if item["kind"] not in KINDS:
        raise ValueError(f"genre de bibliothèque inconnu : {item['kind']!r} (attendu : {', '.join(KINDS)})")
    return Library(item["id"], item.get("label", item["id"]), item["kind"], Path(item["path"]))


def _declared_libraries(path):
    """Les bibliothèques que décrit le fichier HUB_LIBRARIES : une liste, ou un objet qui la porte sous `libraries`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [_declared(item) for item in (data["libraries"] if isinstance(data, dict) else data)]


def _known_libraries(srv):
    """Les dossiers connus sous HUB_SRV (la casse ne compte pas) ; pour chaque bibliothèque, le premier de ses noms qui existe."""
    existing = {p.name.casefold(): p for p in srv.iterdir() if p.is_dir()} if srv.is_dir() else {}
    found = []
    for lib_id, label, kind, names in DEFAULTS:
        name = next((n.casefold() for n in names if n.casefold() in existing), None)
        if name:
            found.append(Library(lib_id, label, kind, existing[name]))
    return found


def _libraries(env, srv):
    explicit = env.get("HUB_LIBRARIES")
    return _declared_libraries(explicit) if explicit else _known_libraries(srv)


def _megabytes(env, name, default, least):
    """Une taille donnée en Mo dans l'environnement, rendue en octets ; jamais moins que `least` Mo."""
    return max(least, int(env.get(name, default))) << 20


def load(env=None):
    env = os.environ if env is None else env
    data = Path(env.get("HUB_DATA", "data")).resolve()
    repo = _find_repo(env)
    srv = Path(env.get("HUB_SRV", "/srv"))
    return Config(
        libraries=_libraries(env, srv),
        data=data,
        repo=repo,
        bind=env.get("HUB_BIND", "127.0.0.1"),
        port=int(env.get("HUB_PORT", "8080")),
        password=env.get("HUB_PASSWORD", ""),
        admin=env.get("HUB_ADMIN", "").strip() or "admin",
        ffmpeg=_find_tool("ffmpeg", env, data),
        ffprobe=_find_tool("ffprobe", env, data),
        max_streams=max(1, int(env.get("HUB_MAX_STREAMS", "2"))),
        autoscan_hours=float(env.get("HUB_AUTOSCAN_HOURS", "0")),
        tmdb_key=env.get("TMDB_KEY") or _read_dotenv_key(repo),
        inbox_keep_free=_megabytes(env, "HUB_INBOX_KEEP_FREE", 10240, 0),
        inbox_max_uploads=max(1, int(env.get("HUB_INBOX_MAX_UPLOADS", "2"))),
    )


def use_media_library(repo):
    """Rend importable le paquet `libraries` de media_library (ses lecteurs de MKV, FLAC, MP3, CBZ, EPUB)."""
    if repo is None:
        raise SystemExit("media_library introuvable : pose-le à côté de media_hub, ou indique-le avec HUB_REPO.")
    if str(repo) not in sys.path:
        sys.path.append(str(repo))      # à la fin : media_library a aussi un paquet « tests », qui ne doit pas masquer les nôtres
