"""Enrichissement des séries depuis TMDB : synopsis, genres, année, affiche.

Les épisodes portent leur propre fiche, mais pas celle de la série : rien dans un .mkv ne dit de quoi parle « Dark ». Ce passage interroge TMDB (par l'identifiant épinglé dans le nom du dossier, sinon par une recherche sur ce nom) et garde le résultat dans la base ET sur le disque (l'affiche), pour qu'un rescan ne l'efface pas.

Il ne tourne que sur demande (bouton « Enrichir »), et seulement pour les séries qui n'ont pas encore été enrichies, sauf `force`. Il demande une clé TMDB (TMDB_KEY).
"""

from __future__ import annotations

import contextlib
import io
import json

from libraries.common import cache as tmdb_cache
from libraries.video import lookup
from libraries.video.tmdb import Tmdb, TmdbAuthError, TmdbError


def enrich_shows(cfg, db, lib, log=print, force=False):
    if not cfg.tmdb_key:
        raise RuntimeError("Pas de clé TMDB : renseigne TMDB_KEY (variable d'environnement, ou .env dans media_library).")
    tmdb = Tmdb(cfg.tmdb_key, "fr-FR", user_agent="media_hub/1.0", cache=tmdb_cache.Cache(folder=cfg.data / "tmdb-cache", read=True))
    posters = cfg.data / "tmdb"
    posters.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    for item in db.query("SELECT * FROM items WHERE lib=? AND type='show' ORDER BY sort", (lib.id,)):
        key = f"enrich:{lib.id}:{item['id']}"
        if not force and db.kv_get(key):
            continue
        meta = json.loads(item["meta"])
        folder = lib.path / meta.get("folder", "")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                show_id = lookup.resolve_show_id(tmdb, folder, meta.get("tmdb_pinned") or None)
            if not show_id:
                log(f"  {item['title']} : série introuvable sur TMDB")
                failed += 1
                continue
            show = tmdb.series(show_id)
            data = {
                "summary": show.get("overview") or "", "genres": [g.get("name", "") for g in show.get("genres", []) if g.get("name")],
                "year": (show.get("first_air_date") or "")[:4], "tmdb": int(show_id), "tmdb_name": show.get("name") or "",
                "seasons_total": show.get("number_of_seasons"), "episodes_total": show.get("number_of_episodes"), "status": show.get("status") or "",
            }
            if show.get("poster_path"):
                name = f"{show_id}.jpg"
                tmdb.save_image(show["poster_path"], "w500", posters / name)
                data["poster_file"] = name
            db.kv_set(key, data)
            fields = {"summary": data["summary"], "genres": data["genres"], "meta": {k: v for k, v in data.items() if k not in ("summary", "genres", "year")}}
            if data["year"]:
                fields["year"] = data["year"]
            db.update_item_meta(item["id"], **fields)
            done += 1
            log(f"  {item['title']} -> {show.get('name')} ({data['year']})")
        except TmdbAuthError as e:
            raise RuntimeError(f"TMDB : {e}")
        except (TmdbError, OSError) as e:
            log(f"  {item['title']} : {e}")
            failed += 1
    return {"enriched": done, "failed": failed}
