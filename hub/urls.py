"""Les URLs que l'API donne aux clients : relatives, sans `?w=` (la taille se demande en ajoutant `?w=`), sans jeton (le client ajoute le sien).

Un seul endroit pour les écrire : le serveur et les playlists les renvoient, la documentation (docs/api.md) les décrit.
"""

from __future__ import annotations


def item_cover(item_id):
    """L'affiche, la pochette ou la couverture d'un élément (film, série, album, série de livres) ; la route répond 404 s'il n'en a pas."""
    return f"/img/item/{item_id}"


def entry_image(entry_id):
    """L'image propre à un fichier (vignette d'épisode, couverture d'un tome) ; 404 s'il n'en a pas."""
    return f"/img/entry/{entry_id}"


def entry_file(entry_id):
    """Le fichier lui-même, avec les requêtes Range."""
    return f"/media/{entry_id}/file"


def missing_film(tmdb_id):
    """L'affiche d'un film qui manque à une saga (voir missing.py) ; 404 s'il n'en a pas."""
    return f"/img/film/{tmdb_id}"


def artist_image(lib_id, artist_id):
    """La photo d'un artiste ; 404 s'il n'en a pas."""
    return f"/img/artist/{lib_id}/{artist_id}"
