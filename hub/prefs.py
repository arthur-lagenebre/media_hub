"""Les réglages d'un compte : ce qu'il choisit d'afficher et d'entendre, gardé côté serveur pour le suivre d'un appareil à l'autre (ceux qui dépendent de l'écran, comme la qualité de lecture, restent dans le navigateur)."""

from __future__ import annotations

from .media.subs import canonical
from .media.tracks import SUB_MODES

KEY = "prefs:"                                    # base de données : prefs:<id du compte> donne ses réglages
SHELF_CARDS = 48                                  # les cartes d'un rayon de l'accueil, au plus : 3 rangées de 16 colonnes, la plus grande grille prévue ; le navigateur coupe à `home_shelf_rows` rangées de ce que l'écran montre
CHOICES = {"music_view": ("albums", "artists"), "movie_view": ("sagas", "films"), "home_latest": ("show", "hide"), "home_unseen": ("show", "hide"), "home_shelf_rows": ("1", "2", "3"), "home_unseen_count": ("6", "12", "18", "24"),
           "home_unseen_sort": ("random", "rating", "added"), "home_requests": ("show", "hide"), "home_recommend": ("show", "hide"), "hide_spoilers": ("show", "image", "image_summary", "all"), "sub_mode": SUB_MODES}    # chaque réglage et les valeurs qu'il accepte
DEFAULTS = {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_shelf_rows": "2", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show", "home_recommend": "show",
            "hide_spoilers": "show", "sub_mode": "forced"}      # la musique s'ouvre sur les albums, les films regroupés en sagas, l'accueil montre « Nouveautés », « Pas encore vu » (dans un ordre qui change chaque jour), les demandes arrivées et « Parce que tu as aimé », sur deux rangées ; les épisodes pas encore commencés montrés en clair (sinon l'interface floute leur image, leur image et leur résumé, ou tout : `image`, `image_summary`, `all`) ; les sous-titres forcés seulement
ROWS_OF_CARDS = {"6": "1", "12": "2", "18": "3", "24": "3"}      # l'ancien `home_unseen_count` en rangées (son équivalent sur un écran de 6 colonnes, 3 au plus) : le choix d'un compte qui n'a pas encore de `home_shelf_rows`
# Les réglages qui sont des listes ordonnées de langues (codes canoniques à deux lettres, voir subs.canonical), vides par défaut : sans langue audio, le fichier décide ;
# sans langue de sous-titres, c'est celle de la page (voir api_play).
LANG_LISTS = ("audio_langs", "sub_langs")
MAX_LANGS = 8


def _kept(db, uid):
    kept = db.kv_get(KEY + uid)
    return kept if isinstance(kept, dict) else {}


def _valid_langs(value):
    """Une liste d'au plus MAX_LANGS codes canoniques, chacun une seule fois."""
    return (isinstance(value, list) and len(value) <= MAX_LANGS and all(isinstance(c, str) and c and canonical(c) == c for c in value)
            and len(set(value)) == len(value))


def _valid(name, value):
    return _valid_langs(value) if name in LANG_LISTS else value in CHOICES[name]


def load(db, uid):
    """Tous les réglages du compte : ce qu'il a choisi, sinon la valeur par défaut (une valeur qui n'est plus permise retombe sur la valeur par défaut). Sans `home_shelf_rows` valide, les rangées se déduisent de son ancien `home_unseen_count`, sans rien écrire."""
    kept = _kept(db, uid)
    chosen = {name: kept[name] if _valid(name, kept.get(name)) else DEFAULTS[name] for name in CHOICES}
    if not _valid("home_shelf_rows", kept.get("home_shelf_rows")) and _valid("home_unseen_count", kept.get("home_unseen_count")):
        chosen["home_shelf_rows"] = ROWS_OF_CARDS[kept["home_unseen_count"]]
    return {**chosen, **{name: list(kept[name]) if _valid(name, kept.get(name)) else [] for name in LANG_LISTS}}


def save(db, uid, changes):
    """Retient `changes` ({réglage: valeur}) et rend tous les réglages ; ValueError, sans rien changer, si un réglage ou une valeur est inconnu."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("aucun réglage à changer")
    for name, value in changes.items():
        if name not in CHOICES and name not in LANG_LISTS:
            raise ValueError(f"réglage inconnu : {name}")
        if not _valid(name, value):
            raise ValueError(f"valeur inconnue pour {name} : {value}")
    db.kv_set(KEY + uid, {**_kept(db, uid), **changes})
    return load(db, uid)
