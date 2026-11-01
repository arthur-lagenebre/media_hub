"""Enrichissement depuis le web : les séries et les sagas de films (TMDB), les artistes (MusicBrainz, Wikipédia).

Séries : synopsis (de la série et de chaque saison), genres, année, casting, affiche.

Sagas de films : les films qui manquent à une saga (voir missing.py) : titre, année et affiche de ceux qu'on n'a pas.

Les épisodes portent leur propre fiche, mais pas celle de la série : rien dans un .mkv ne dit de quoi parle « Dark ». Ce passage interroge TMDB (par l'identifiant épinglé dans le nom du dossier, sinon par une recherche sur ce nom) et garde le résultat dans la base ET sur le disque (l'affiche), pour qu'un rescan ne l'efface pas.

Il ne tourne que sur demande (bouton « Enrichir »), et seulement pour les séries qui n'ont pas encore été enrichies, sauf `force`. Il demande une clé TMDB (TMDB_KEY).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import time
from datetime import date
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from libraries.common import cache as tmdb_cache
from libraries.video import lookup
from libraries.music import artistinfo
from libraries.music.musicbrainz import MusicBrainz, MusicBrainzError
from libraries.video.tmdb import Tmdb, TmdbAuthError, TmdbError

from .media import images
from . import missing
from . import sagas as sagafile

MB_USER_AGENT = "media_hub/1.0 ( https://github.com/arthur-lagenebre/media_hub )"
PHOTO_WIDTH = 960               # la photo de l'article, en 960 px quand l'originale est plus grande : de quoi remplir l'en-tête sans garder 5 000 px
PHOTO_MAX = 8 * 1024 * 1024
PHOTO_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org")        # l'API de Wikipédia sert ses vignettes depuis le second
PHOTO_TYPES = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"RIFF", ".webp"))
PHOTO_EXTS = tuple(ext for _, ext in PHOTO_TYPES)
SAFE_STEM = re.compile(r"[\w-]+")      # l'identifiant MusicBrainz vient des tags des fichiers : il ne sert de nom de fichier que s'il n'a rien d'un chemin
PHOTO_STEM = "artist"           # la photo d'un artiste : artist.webp, dans son dossier à la racine de la médiathèque


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
    return [{"name": person["name"], "role": main_role(person)} for person in people[:CAST_SIZE]]


def main_role(person):
    """Le rôle où la personne joue le plus d'épisodes ; vide si TMDB n'en donne aucun."""
    roles = sorted(person.get("roles") or [], key=lambda r: -(r.get("episode_count") or 0))
    return (roles[0].get("character") or "") if roles else ""


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


def enrich_sagas(cfg, db, lib, log=print, force=False):
    """Les films qui manquent aux sagas d'une bibliothèque de films : ceux des collections TMDB que dit l'étiquette, et ceux que les sagas de `sagas.json` listent sans qu'on les ait.

    Tous les films d'une collection sont décrits, ceux qu'on a comme les autres : un film retiré de la médiathèque doit pouvoir apparaître grisé sans attendre qu'on redemande la collection. Une collection déjà connue n'est redemandée qu'après un mois (ou avec `force`) ; un film décrit ne l'est pas deux fois. Le résultat se garde au fur et à mesure (voir missing.py) : une clé refusée à mi-chemin n'efface pas ce qui est fait.
    """
    if not cfg.tmdb_key:
        raise RuntimeError("Pas de clé TMDB : renseigne TMDB_KEY (variable d'environnement, ou .env dans media_library).")
    tmdb = Tmdb(cfg.tmdb_key, "fr-FR", user_agent="media_hub/1.0", cache=tmdb_cache.Cache(folder=cfg.data / "tmdb-cache", read=not force))
    posters = cfg.data / "tmdb"
    posters.mkdir(parents=True, exist_ok=True)
    info = missing.load(db)
    rows = db.query("SELECT * FROM items WHERE lib=? AND type='movie' ORDER BY sort", (lib.id,))
    owned = sagafile.by_tmdb(rows)
    now = int(time.time())
    counts = {"sagas": 0, "films": 0, "failed": 0}

    def keep(film):
        """Retient un film que TMDB décrit (titre, date, affiche)."""
        ident = film["id"]
        date_ = film.get("release_date") or ""
        data = {"title": film.get("title") or "", "date": date_, "year": date_[:4], "poster": ""}
        name = missing.poster_name(ident)
        if film.get("poster_path"):
            try:
                if force or not (posters / name).is_file():
                    partial = posters / (name + ".tmp")
                    tmdb.save_image(film["poster_path"], "w342", partial)
                    os.replace(partial, posters / name)         # d'un bloc : une écriture coupée ne laisse pas une affiche tronquée qu'on ne retélécharge plus
                data["poster"] = name
            except TmdbAuthError:
                raise
            except (TmdbError, OSError) as e:
                log(f"  {data['title']} : pas d'affiche ({e})")
        info["films"][str(ident)] = data
        counts["films"] += 1

    def describe(ident):
        """Décrit un film d'après sa fiche TMDB (un film dont la collection ne le donne pas, ou que seule une saga du fichier réclame)."""
        try:
            keep(tmdb.get(f"movie/{ident}"))
        except TmdbAuthError:
            raise
        except TmdbError as e:
            log(f"  film {ident} : {e}")
            counts["failed"] += 1

    groups = {}
    for row in rows:
        if row["grp"]:
            groups.setdefault(row["grp"], []).append(row)
    try:
        for name, members in sorted(groups.items()):
            known = info["collections"].get(name)
            if known and not force and now - known.get("at", 0) < missing.STALE:
                for ident in known["films"]:                    # connue avant qu'on décrive aussi les films qu'on a : ceux qui ont quitté la médiathèque depuis le sont maintenant
                    if ident not in owned and str(ident) not in info["films"]:
                        describe(ident)
                continue
            ident = next((i for i in map(sagafile.tmdb_of, members) if i is not None), None)
            if ident is None:
                log(f"  {name} : aucun film n'a d'identifiant TMDB")
                continue
            try:
                collection = (tmdb.get(f"movie/{ident}").get("belongs_to_collection") or {}).get("id")
                if not collection:
                    log(f"  {name} : TMDB ne rattache ce film à aucune collection")
                    counts["failed"] += 1
                    continue
                parts = tmdb.collection(collection).get("parts") or []
            except TmdbAuthError:
                raise
            except TmdbError as e:
                log(f"  {name} : {e}")
                counts["failed"] += 1
                continue
            parts = sorted((p for p in parts if p.get("id")), key=lambda p: (p.get("release_date") or "9999", p["id"]))
            for part in parts:
                keep(part)
            info["collections"][name] = {"id": collection, "films": [p["id"] for p in parts], "at": now}
            counts["sagas"] += 1
            absent = [p for p in parts if p["id"] not in owned]
            coming = [p for p in absent if not missing.released({"date": p.get("release_date")})]
            log(f"  {name} : {len(parts)} film(s), {len(absent) - len(coming)} absent(s)" + (f", {len(coming)} pas encore sorti(s)" if coming else ""))
        wanted = []
        for _, ids in sagafile.Sagas(cfg.data / "sagas.json").all():
            wanted += [i for i in ids if i not in owned and i not in wanted and (force or str(i) not in info["films"])]
        for ident in wanted:
            describe(ident)
    except TmdbAuthError as e:
        raise RuntimeError(f"TMDB : {e}")
    finally:
        missing.save(db, info)
    return counts


def artist_key(mbid):
    return f"artist:{mbid}"


WANTED_LINKS = ("Site officiel", "Bandcamp", "SoundCloud", "YouTube")      # les seuls liens que la fiche montre, dans cet ordre
SHEET_VERSION = 2                                                           # 2 : liens limités ; une fiche plus ancienne est refaite


def pick_links(links):
    """[{'label', 'url'}] ramenés à WANTED_LINKS : un seul lien par libellé (le premier), dans l'ordre de WANTED_LINKS."""
    first = {}
    for link in links:
        if link.get("label") in WANTED_LINKS:
            first.setdefault(link["label"], link)
    return [first[label] for label in WANTED_LINKS if label in first]


def wanted_links(data):
    """Les liens voulus d'une fiche MusicBrainz brute. artistinfo.parse s'arrête à 8 liens, souvent pris par les réseaux sociaux : YouTube ou SoundCloud pourraient en sortir avant qu'on filtre."""
    found = []
    for rel in data.get("relations") or []:
        url = (rel.get("url") or {}).get("resource") or ""
        if rel.get("type") in artistinfo.LINK_TYPES and urlparse(url).scheme in ("http", "https") and urlparse(url).hostname:
            found.append({"label": artistinfo.LINK_LABELS.get(rel["type"]) or artistinfo.host_label(url), "url": url})
    return pick_links(found)


def fetch_artist(mb, mbid):
    """La fiche d'un artiste (voir artistinfo.fetch), avec ses liens limités à WANTED_LINKS. La fiche MusicBrainz est déjà en cache : pas de requête de plus."""
    info = artistinfo.fetch(mb, mbid)
    info["links"] = wanted_links(mb.artist(mbid))
    info["sheet"] = SHEET_VERSION
    return info


def photo_urls(mb, wiki):
    """Les adresses possibles de la photo de l'article Wikipédia d'un artiste, la plus grande d'abord ; [] si l'article n'en a pas."""
    try:
        data = mb.external(artistinfo.WIKIPEDIA_URL.format(wiki["lang"], quote(wiki["title"].replace(" ", "_"), safe="")))
    except MusicBrainzError:
        return []
    thumb, original = (data.get("thumbnail") or {}).get("source", ""), data.get("originalimage") or {}
    bigger = [re.sub(r"/\d+px-", f"/{PHOTO_WIDTH}px-", thumb, count=1)] if "/thumb/" in thumb and (original.get("width") or 0) > PHOTO_WIDTH else []
    return [u for u in bigger + [u for u in (thumb, original.get("source", "")) if u] if from_wikimedia(u)]      # ce qu'on télécharge vient de Wikimedia, rien d'autre


def from_wikimedia(url):
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname in PHOTO_HOSTS


def image_ext(data):
    """L'extension (.jpg, .png ou .webp) de l'image que ces octets contiennent, d'après leur début ; None si ce n'en est pas une."""
    ext = next((ext for magic, ext in PHOTO_TYPES if data.startswith(magic)), None)
    return None if ext == ".webp" and data[8:12] != b"WEBP" else ext


class _SameHosts(HTTPRedirectHandler):
    """Une redirection ne sort pas de PHOTO_HOSTS, en https : la liste ne vaut que pour l'adresse de départ si urlopen suit les redirections où elles mènent (une adresse interne du réseau, par exemple)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or (target.hostname or "") not in PHOTO_HOSTS:
            raise URLError(f"redirection refusée vers {target.hostname or newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_SameHosts)


def urlopen(request, timeout=30):
    return _OPENER.open(request, timeout=timeout)


def photo_file(folder, artist_id, mbid="", home=None):
    """Le fichier de la photo d'un artiste : artist.webp dans son dossier (`home`, à la racine de la médiathèque), sinon artists/<id>.<ext> sous HUB_DATA (`folder`). La seule qu'il ait, qu'elle vienne de Wikipédia ou qu'on l'ait envoyée ; None s'il n'en a pas.

    Avant, il y en avait deux dans artists/ : custom-<id>.<ext> (envoyée) et <mbid>.<ext> (Wikipédia). Elles sont ramenées au nom d'aujourd'hui au passage, l'envoyée d'abord.
    """
    if home and (home / f"{PHOTO_STEM}.webp").is_file():
        return home / f"{PHOTO_STEM}.webp"
    return find_photo(folder, artist_id) or rename_old_photo(folder, artist_id, mbid)


def find_photo(folder, stem):
    return next((p for p in (folder / f"{stem}{ext}" for ext in PHOTO_EXTS) if p.is_file()), None)


def rename_old_photo(folder, artist_id, mbid):
    """L'ancienne photo de l'artiste (custom-<id>, sinon <mbid>), ramenée au nom d'aujourd'hui ; elle garde son nom si le renommage échoue. None s'il n'y en a pas."""
    for stem in (f"custom-{artist_id}", mbid):
        old = find_photo(folder, stem) if stem and SAFE_STEM.fullmatch(stem) else None
        if old:
            try:
                return old.replace(folder / f"{artist_id}{old.suffix}")
            except OSError:
                return old
    return None


def store_photo(folder, stem, data, ext):
    """Pose une photo sous <stem><ext> dans `folder` à la place de l'ancienne (même nom, autre extension comprise), qui ne disparaît qu'une fois la nouvelle écrite ; son nom de fichier."""
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{stem}{ext}"
    part = folder / f"{stem}.part"
    part.write_bytes(data)
    part.replace(folder / name)
    for other in PHOTO_EXTS:
        if other != ext:
            (folder / f"{stem}{other}").unlink(missing_ok=True)
    return name


def save_photo(cfg, artist_id, data, home=None, box=None, mbid=""):
    """Garde `data` comme photo de l'artiste, à la place de la sienne, et le chemin du fichier.

    Convertie en WebP, rognée à `box` (voir images.crop_box : ce qui est hors du cadre disparaît) et réduite à 1280 px, puis posée dans son dossier (`home`). Sans de quoi convertir (ni Pillow ni ffmpeg) et sans `box`, elle est gardée telle quelle sous HUB_DATA/artists. ConvertError si elle ne se lit pas, ou si `box` est demandé sans de quoi le faire ; OSError si l'écriture échoue.
    """
    legacy = cfg.data / "artists"
    if images.can_webp(cfg.ffmpeg):
        data, ext = images.to_webp(data, cfg.ffmpeg, box), ".webp"
    elif box:
        raise images.NoConverter("ni Pillow ni ffmpeg : pas de recadrage possible")
    else:
        ext = image_ext(data)
    folder, stem = (home, PHOTO_STEM) if home and ext == ".webp" else (legacy, artist_id)
    target = folder / store_photo(folder, stem, data, ext)
    stale = [legacy / f"{s}{e}" for s in (artist_id, f"custom-{artist_id}", mbid) if s and SAFE_STEM.fullmatch(s) for e in PHOTO_EXTS]
    if home:
        stale.append(home / f"{PHOTO_STEM}.webp")           # ni l'ancienne d'artists/, ni l'autre dossier : une seule photo par artiste
    for path in stale:
        if path != target:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def download_photo(cfg, urls, artist_id, home=None, mbid=""):
    """Garde la première photo qui se télécharge, à la place de celle de l'artiste (voir save_photo) ; son nom de fichier, ou OSError si aucune ne vient."""
    error = OSError("photo introuvable")
    for url in urls:
        try:
            with urlopen(Request(url, headers={"User-Agent": MB_USER_AGENT}), timeout=30) as response:
                data = response.read(PHOTO_MAX + 1)
        except (URLError, OSError) as e:
            error = e if isinstance(e, OSError) else OSError(str(e))
            continue
        if not image_ext(data) or len(data) > PHOTO_MAX:
            error = OSError("ce n'est pas une image, ou trop grosse")
            continue
        try:
            return save_photo(cfg, artist_id, data, home, mbid=mbid).name
        except images.ConvertError as e:
            error = OSError(str(e))
    raise error


def musicbrainz(cfg):
    return MusicBrainz(user_agent=MB_USER_AGENT, cache=tmdb_cache.Cache(folder=cfg.data / "musicbrainz-cache", read=True))


def wikipedia_photo(cfg, artist_id, wiki, home=None, mbid=""):
    """Remplace la photo d'un artiste par celle de son article Wikipédia ; son nom de fichier, ou OSError si l'article n'en a pas ou si elle ne vient pas."""
    urls = photo_urls(musicbrainz(cfg), wiki)
    if not urls:
        raise OSError("l'article n'a pas de photo")
    return download_photo(cfg, urls, artist_id, home, mbid)


def enrich_artists(cfg, db, lib, log=print, force=False):
    """La fiche de chaque artiste de la bibliothèque de musique qui porte un identifiant MusicBrainz : type, pays, dates, membres, liens, résumé Wikipédia.

    Sans clé (MusicBrainz n'en demande pas), mais à raison d'une requête par seconde : la première passe est longue. Une fiche de moins de 30 jours n'est pas redemandée, sauf `force`. Elle est gardée dans la base : un rescan ne l'efface pas.
    """
    mb = musicbrainz(cfg)
    artists, ids, homes = {}, {}, {}
    for row in db.query("SELECT grp, meta FROM items WHERE lib=? AND type='album'", (lib.id,)):
        meta = json.loads(row["meta"])
        mbid = meta.get("artist_mbid")
        if mbid:
            artists.setdefault(mbid, row["grp"])
            if meta.get("artist_id"):
                ids.setdefault(mbid, set()).add(meta["artist_id"])
                if meta.get("artist_dir") and (lib.path / meta["artist_dir"]).is_dir():
                    homes[meta["artist_id"]] = lib.path / meta["artist_dir"]
    folder = cfg.data / "artists"
    done = failed = 0
    for mbid, name in sorted(artists.items(), key=lambda kv: kv[1].casefold()):
        kept = db.kv_get(artist_key(mbid))
        if not force and kept and not artistinfo.stale(kept) and "photo" in kept["info"] and kept["info"].get("sheet") == SHEET_VERSION:        # sans "photo" ou d'une version plus ancienne : on la refait
            continue
        try:
            info = fetch_artist(mb, mbid)
        except MusicBrainzError as e:
            log(f"  {name} : {e}")
            failed += 1
            continue
        urls = photo_urls(mb, info["wiki"]) if info.get("wiki") else []
        info["photo"] = ""                                                                          # "" : pas de photo de Wikipédia, et inutile de la redemander
        for artist_id in sorted(ids.get(mbid, ())):
            have = photo_file(folder, artist_id, mbid, homes.get(artist_id))
            if have:                                                                                # elle a déjà sa photo, d'où qu'elle vienne : on n'y touche pas
                info["photo"] = have.name
                continue
            try:
                info["photo"] = download_photo(cfg, urls, artist_id, homes.get(artist_id), mbid) if urls else ""
            except OSError as e:
                log(f"  {name} : photo non récupérée ({e})")
                info.pop("photo", None)                                                             # pas de clé "photo" : le prochain passage réessaie
        db.kv_set(artist_key(mbid), {"fetched": date.today().isoformat(), "info": info})
        done += 1
        log(f"  {name} : {artistinfo.facts(info) or 'fiche vide'}{' · photo' if info.get('photo') else ''}")
    return {"enriched": done, "failed": failed}
