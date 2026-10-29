"""Enrichissement depuis le web : les séries (TMDB) et les artistes (MusicBrainz, Wikipédia).

Séries : synopsis (de la série et de chaque saison), genres, année, casting, affiche.

Les épisodes portent leur propre fiche, mais pas celle de la série : rien dans un .mkv ne dit de quoi parle « Dark ». Ce passage interroge TMDB (par l'identifiant épinglé dans le nom du dossier, sinon par une recherche sur ce nom) et garde le résultat dans la base ET sur le disque (l'affiche), pour qu'un rescan ne l'efface pas.

Il ne tourne que sur demande (bouton « Enrichir »), et seulement pour les séries qui n'ont pas encore été enrichies, sauf `force`. Il demande une clé TMDB (TMDB_KEY).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from datetime import date
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from libraries.common import cache as tmdb_cache
from libraries.video import lookup
from libraries.music import artistinfo
from libraries.music.musicbrainz import MusicBrainz, MusicBrainzError
from libraries.video.tmdb import Tmdb, TmdbAuthError, TmdbError

MB_USER_AGENT = "media_hub/1.0 ( https://github.com/arthur-lagenebre/media_hub )"
PHOTO_WIDTH = 960               # la photo de l'article, en 960 px quand l'originale est plus grande : de quoi remplir l'en-tête sans garder 5 000 px
PHOTO_MAX = 8 * 1024 * 1024
PHOTO_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org")        # l'API de Wikipédia sert ses vignettes depuis le second
PHOTO_TYPES = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"RIFF", ".webp"))


CAST_SIZE = 20


def season_summaries(tmdb, show_id, seasons):
    """{numéro de saison: synopsis} pour les saisons présentes sur le disque. Une saison que TMDB ne connaît pas est simplement omise."""
    out = {}
    for number in seasons:
        try:
            overview = tmdb.season(show_id, number).get("overview") or ""
        except TmdbAuthError:
            raise
        except TmdbError:
            continue
        if overview:
            out[str(number)] = overview
    return out


def cast(tmdb, show_id):
    """Le casting de la série entière : les acteurs les plus présents (par nombre d'épisodes), avec leur rôle principal."""
    try:
        people = tmdb.aggregate_credits(show_id).get("cast", [])
    except TmdbAuthError:
        raise
    except TmdbError:
        return []
    people = sorted((p for p in people if p.get("name")), key=lambda p: (-(p.get("total_episode_count") or 0), p.get("order", 9999)))
    out = []
    for person in people[:CAST_SIZE]:
        roles = sorted(person.get("roles") or [], key=lambda r: -(r.get("episode_count") or 0))
        out.append({"name": person["name"], "role": (roles[0].get("character") or "") if roles else ""})
    return out


def enrich_shows(cfg, db, lib, log=print, force=False):
    if not cfg.tmdb_key:
        raise RuntimeError("Pas de clé TMDB : renseigne TMDB_KEY (variable d'environnement, ou .env dans media_library).")
    tmdb = Tmdb(cfg.tmdb_key, "fr-FR", user_agent="media_hub/1.0", cache=tmdb_cache.Cache(folder=cfg.data / "tmdb-cache", read=True))
    posters = cfg.data / "tmdb"
    posters.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    for item in db.query("SELECT * FROM items WHERE lib=? AND type='show' ORDER BY sort", (lib.id,)):
        key = f"enrich:{lib.id}:{item['id']}"
        done_before = db.kv_get(key)
        if not force and done_before and "season_summaries" in done_before:      # enrichi avant l'ajout des saisons et du casting : on le refait
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
            data["season_summaries"] = season_summaries(tmdb, show_id, meta.get("seasons", []))
            data["actors"] = cast(tmdb, show_id)
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


def artist_key(mbid):
    return f"artist:{mbid}"


def photo_urls(mb, wiki):
    """Les adresses possibles de la photo de l'article Wikipédia d'un artiste, la plus grande d'abord ; [] si l'article n'en a pas."""
    try:
        data = mb.external(artistinfo.WIKIPEDIA_URL.format(wiki["lang"], quote(wiki["title"].replace(" ", "_"), safe="")))
    except MusicBrainzError:
        return []
    thumb, original = (data.get("thumbnail") or {}).get("source", ""), data.get("originalimage") or {}
    urls = []
    if "/thumb/" in thumb and (original.get("width") or 0) > PHOTO_WIDTH:
        urls.append(re.sub(r"/\d+px-", f"/{PHOTO_WIDTH}px-", thumb, count=1))
    urls += [u for u in (thumb, original.get("source", "")) if u]
    return [u for u in urls if urlparse(u).scheme == "https" and urlparse(u).hostname in PHOTO_HOSTS]      # ce qu'on télécharge vient de Wikimedia, rien d'autre


def download_photo(urls, folder, mbid):
    """Garde la première photo qui se télécharge, sous artists/<mbid>.<ext> ; son nom de fichier, ou OSError si aucune ne vient."""
    folder.mkdir(parents=True, exist_ok=True)
    error = OSError("photo introuvable")
    for url in urls:
        try:
            with urlopen(Request(url, headers={"User-Agent": MB_USER_AGENT}), timeout=30) as response:
                data = response.read(PHOTO_MAX + 1)
        except (URLError, OSError) as e:
            error = e if isinstance(e, OSError) else OSError(str(e))
            continue
        ext = next((ext for magic, ext in PHOTO_TYPES if data.startswith(magic)), None)
        if ext and len(data) <= PHOTO_MAX:
            for old in folder.glob(f"{mbid}.*"):
                old.unlink()
            (folder / f"{mbid}{ext}").write_bytes(data)
            return f"{mbid}{ext}"
        error = OSError("ce n'est pas une image, ou trop grosse")
    raise error


def enrich_artists(cfg, db, lib, log=print, force=False):
    """La fiche de chaque artiste de la bibliothèque de musique qui porte un identifiant MusicBrainz : type, pays, dates, membres, liens, résumé Wikipédia.

    Sans clé (MusicBrainz n'en demande pas), mais à raison d'une requête par seconde : la première passe est longue. Une fiche de moins de 30 jours n'est pas redemandée, sauf `force`. Elle est gardée dans la base : un rescan ne l'efface pas.
    """
    mb = MusicBrainz(user_agent=MB_USER_AGENT, cache=tmdb_cache.Cache(folder=cfg.data / "musicbrainz-cache", read=True))
    artists = {}
    for row in db.query("SELECT grp, meta FROM items WHERE lib=? AND type='album'", (lib.id,)):
        mbid = json.loads(row["meta"]).get("artist_mbid")
        if mbid:
            artists.setdefault(mbid, row["grp"])
    done = failed = 0
    for mbid, name in sorted(artists.items(), key=lambda kv: kv[1].casefold()):
        kept = db.kv_get(artist_key(mbid))
        if not force and kept and not artistinfo.stale(kept) and "photo" in kept["info"]:        # sans "photo" : fiche d'avant les photos, on la refait
            continue
        try:
            info = artistinfo.fetch(mb, mbid)
        except MusicBrainzError as e:
            log(f"  {name} : {e}")
            failed += 1
            continue
        urls = photo_urls(mb, info["wiki"]) if info.get("wiki") else []
        try:
            info["photo"] = download_photo(urls, cfg.data / "artists", mbid) if urls else ""      # "" : pas de photo, et inutile de la redemander
        except OSError as e:
            log(f"  {name} : photo non récupérée ({e})")                                          # pas de clé "photo" : le prochain passage réessaie
        db.kv_set(artist_key(mbid), {"fetched": date.today().isoformat(), "info": info})
        done += 1
        log(f"  {name} : {artistinfo.facts(info) or 'fiche vide'}{' · photo' if info.get('photo') else ''}")
    return {"enriched": done, "failed": failed}
