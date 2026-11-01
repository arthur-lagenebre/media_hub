"""Musique : les albums tels que leurs tags les décrivent (FLAC et MP3), lus par media_library.

Un album est un dossier qui contient de l'audio (lui ou ses CD1, CD2...) ; les dossiers d'artiste au-dessus ne font que ranger. Le travail de reconnaissance est celui de libraries/music : on ne fait ici que le ranger dans la base.
"""

from __future__ import annotations

import json
from collections import Counter

from libraries.music import library, wanted

from .common import entry_row, is_album_cover, item_row, unchanged
from .video import _Counts


def _read_tracks(release, known):
    """Lit les tags des pistes d'un album, ou reprend ceux du scan précédent ; (reprises, lues)."""
    reused = read = 0
    for track in release.tracks:
        row = known.get(track.rel)
        cached = json.loads(row["meta"]).get("info") if unchanged(row, track.size, track.mtime) else None
        if cached:
            track.info = cached
            reused += 1
        else:
            library.read_info(track)
            read += 1
    return reused, read


def _album_item(lib, release, homes):
    progress = release.progress()
    cover = release.cover_file if release.cover_file and is_album_cover(release.cover_file.name) else None     # cover.{ext} seul : folder.jpg et les pochettes embarquées ne comptent pas
    meta = {
        "artist": release.artist, "artist_id": release.artist_id, "artist_mbid": release.artist_mbid, "mbid": release.mbid, "label": release.label,
        "catalog": release.catalog, "formats": release.formats, "tagged": release.tagged, "folder": release.rel,
        "artist_dir": homes.get(release.artist_id, ""),
        "cover_file": cover.name if cover else "", "has_cover": bool(cover),
        "progress": list(progress) if progress else None, "discs": len(release.discs), "duration": release.duration,
    }
    return item_row(lib.id, release.rel or ".", "album", release.title, year=release.year, genres=release.genres, grp=release.artist, meta=meta,
                    added=max(t.mtime for t in release.tracks), count=len(release.tracks))


def _album_entries(lib, item, release):
    return [entry_row(lib.id, item["id"], track.rel, "audio", title=track.title, disc=track.disc, number=track.number, duration=track.duration,
                      size=track.size, mtime=track.mtime, meta={"info": track.info, "error": track.error or "", "format": track.kind})
            for track in release.tracks]


def scan_music(lib, known, enriched=None):
    releases = library.scan(lib.path)
    reused = read = 0
    items, entries = [], []
    homes = artist_dirs(releases)
    for release in releases:
        more_reused, more_read = _read_tracks(release, known)
        reused, read = reused + more_reused, read + more_read
        item = _album_item(lib, release, homes)
        items.append(item)
        entries += _album_entries(lib, item, release)
    counts = _Counts(reused, read)
    counts.kv = {f"wanted:{lib.id}": wanted_sheet(lib.path, wanted.find(lib.path, releases))}       # les albums manquants se rangent à part : ce ne sont pas des items, aucune liste ne doit les compter
    return items, entries, counts


def artist_dirs(releases):
    """{id d'artiste: le nom de son dossier, à la racine de la médiathèque} : celui où il a la majorité des albums qui s'y rangent. Un dossier de genre ou de compilations, qui range plusieurs artistes sans majorité, n'est celui de personne. C'est là que va sa photo (artist.webp)."""
    inside = {}
    for release in releases:
        parts = release.rel.split("/")
        if len(parts) >= 2:             # un album posé à la racine n'a pas de dossier d'artiste
            inside.setdefault(parts[0], Counter())[release.artist_id] += 1
    best = {}
    for name, counts in sorted(inside.items()):
        artist, n = counts.most_common(1)[0]
        if n * 2 > sum(counts.values()) and n > best.get(artist, ("", 0))[1]:
            best[artist] = (name, n)
    return {artist: name for artist, (name, _) in best.items()}


def wanted_sheet(root, found):
    """{id d'artiste: [{year, title, key, cover}]} : ce que la fiche d'un artiste montre en grisé. `cover` est le chemin de l'image depuis la racine de la bibliothèque, ou ""."""
    return {artist: [{"year": w.year, "title": w.title, "key": w.key, "cover": w.cover.relative_to(root).as_posix() if w.cover else ""} for w in albums] for artist, albums in found.items()}

