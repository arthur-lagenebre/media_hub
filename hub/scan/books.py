"""Livres, BD, comics et mangas : les séries de l'étagère, lues dans leurs fiches (ComicInfo.xml, .opf).

Ouvrir des milliers d'archives sur un disque coûte cher : une fiche déjà lue n'est relue que si le fichier a changé (taille ou date).
"""

from __future__ import annotations

import json

from libraries.books import comicinfo, shelf

from .common import entry_row, item_row, unchanged
from .video import _Counts  # noqa: F401  (même forme de compte rendu)


def scan_books(lib, known, enriched=None):
    reused = read = 0
    items, entries = [], []
    for _section, series_list in shelf.scan(lib.path):
        for series in series_list:
            series.sort()
            for volume in series.volumes:
                row = known.get(volume.rel)
                meta = json.loads(row["meta"]) if unchanged(row, volume.size, volume.mtime) else None
                if meta is not None and "info" in meta:
                    volume.info, volume.error = meta["info"], meta.get("error") or None
                    reused += 1
                else:
                    shelf.read_info(volume)
                    read += 1
            years = series.years
            summary = series.summary[0]
            missing = [{"label": label, "numbers": numbers, "total": total} for label, numbers, total in series.missing()]
            meta = {
                "section": series.section, "collection": series.collection, "label": series.label, "authors": series.authors, "publisher": series.publisher,
                "missing": missing, "unit": shelf.unit(series.section, 1).split(" ", 1)[1], "units": shelf.unit(series.section, 2).split(" ", 1)[1], "tagged": series.tagged,
                "years": list(years) if years else [],
            }
            item = item_row(lib.id, f"series:{series.id}", "series", series.name, year=years[0] if years else "", summary=summary, genres=series.genres,
                            grp=series.label, meta=meta, added=max(v.mtime for v in series.volumes), count=len(series.volumes))
            items.append(item)
            for volume in series.volumes:
                number = comicinfo.number_value(volume.number)
                entries.append(entry_row(lib.id, item["id"], volume.rel, "book", title=volume.title, summary=volume.fields.get("Summary", ""), number=number,
                                         size=volume.size, mtime=volume.mtime,
                                         meta={"info": volume.info, "error": volume.error or "", "format": volume.kind, "sub": volume.sub, "label": volume.number or ""}))
    return items, entries, _Counts(reused, read)
