"""Les réglages d'un compte : ce qu'il choisit d'afficher, gardé côté serveur pour le suivre d'un appareil à l'autre (ceux qui dépendent de l'écran, comme la qualité de lecture, restent dans le navigateur)."""

from __future__ import annotations

KEY = "prefs:"                                    # base de données : prefs:<id du compte> donne ses réglages
CHOICES = {"music_view": ("albums", "artists"), "movie_view": ("sagas", "films"), "home_latest": ("show", "hide"), "home_unseen": ("show", "hide"), "home_unseen_count": ("6", "12", "18", "24"),
           "home_unseen_sort": ("random", "rating", "added")}    # chaque réglage et les valeurs qu'il accepte
DEFAULTS = {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random"}      # la musique s'ouvre sur les albums, les films regroupés en sagas, l'accueil montre « Nouveautés » et « Pas encore vu », celui-ci dans un ordre qui change chaque jour


def _kept(db, uid):
    kept = db.kv_get(KEY + uid)
    return kept if isinstance(kept, dict) else {}


def load(db, uid):
    """Tous les réglages du compte : ce qu'il a choisi, sinon la valeur par défaut (une valeur qui n'est plus permise retombe sur la valeur par défaut)."""
    kept = _kept(db, uid)
    return {name: kept[name] if kept.get(name) in values else DEFAULTS[name] for name, values in CHOICES.items()}


def save(db, uid, changes):
    """Retient `changes` ({réglage: valeur}) et rend tous les réglages ; ValueError, sans rien changer, si un réglage ou une valeur est inconnu."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("aucun réglage à changer")
    for name, value in changes.items():
        if name not in CHOICES:
            raise ValueError(f"réglage inconnu : {name}")
        if value not in CHOICES[name]:
            raise ValueError(f"valeur inconnue pour {name} : {value}")
    db.kv_set(KEY + uid, {**_kept(db, uid), **changes})
    return load(db, uid)
