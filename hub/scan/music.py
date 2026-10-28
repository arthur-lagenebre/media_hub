"""Musique : les albums tels que leurs tags les décrivent (FLAC et MP3), lus par media_library.

Un album est un dossier qui contient de l'audio (lui ou ses CD1, CD2...) ; les dossiers d'artiste au-dessus ne font que ranger. Le travail de reconnaissance est celui de libraries/music : on ne fait ici que le ranger dans la base.
"""

from __future__ import annotations

import json

from libraries.music import library

from .common import entry_row, item_row, unchanged
from .video import _Counts


def scan_music(lib, known, enriched=None):
    releases = library.scan(lib.path)
    reused = read = 0
    items, entries = [], []
    for release in releases:
        for track in release.tracks:
            row = known.get(track.rel)
            cached = json.loads(row["meta"]).get("info") if unchanged(row, track.size, track.mtime) else None
            if cached:
                track.info = cached
                reused += 1
            else:
                library.read_info(track)
                read += 1
        progress = release.progress()
        meta = {
            "artist": release.artist, "artist_id": release.artist_id, "artist_mbid": release.artist_mbid, "mbid": release.mbid, "label": release.label,
            "catalog": release.catalog, "formats": release.formats, "tagged": release.tagged, "folder": release.rel,
            "cover_file": release.cover_file.name if release.cover_file else "", "has_cover": bool(release.cover_file or release.cover_track),
            "progress": list(progress) if progress else None, "discs": len(release.discs), "duration": release.duration,
        }
        item = item_row(lib.id, release.rel or ".", "album", release.title, year=release.year, genres=release.genres, grp=release.artist, meta=meta,
                        added=max(t.mtime for t in release.tracks), count=len(release.tracks))
        items.append(item)
        for track in release.tracks:
            entries.append(entry_row(lib.id, item["id"], track.rel, "audio", title=track.title, disc=track.disc, number=track.number, duration=track.duration,
                                     size=track.size, mtime=track.mtime, meta={"info": track.info, "error": track.error or "", "format": track.kind}))
    return items, entries, _Counts(reused, read)

