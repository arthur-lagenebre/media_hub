"""Enrichissement depuis le web : les séries et les sagas de films (TMDB), les artistes (MusicBrainz, Wikipédia).

Séries : synopsis (de la série et de chaque saison), genres, année, casting, affiche.

Sagas de films : les films qui manquent à une saga (voir missing.py) : titre, année et affiche de ceux qu'on n'a pas.

Notes : la moyenne et le nombre des votes TMDB de chaque film et de chaque série (voir enrich_ratings), redemandés quand ils ont vieilli.

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
from urllib.parse import urlparse

from libraries.common import cache as tmdb_cache
from libraries.video import lookup
from libraries.music import artistinfo
from libraries.music.musicbrainz import MusicBrainz, MusicBrainzError
from libraries.video.tmdb import Tmdb, TmdbAuthError, TmdbError

from .media import images
from . import missing
from . import people as peoplefile
from . import sagas as sagafile

MB_USER_AGENT = "media_hub/1.0 ( https://github.com/arthur-lagenebre/media_hub )"
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


def cast_with_ids(tmdb, show_id):
    """([acteurs], {nom: identifiant TMDB}) du casting de la série entière : les acteurs les plus présents (par nombre d'épisodes), avec leur rôle principal. Une seule demande à TMDB ; `(None, None)` quand TMDB ne répond pas (on ne sait pas : ce n'est pas un casting vide).

    L'identifiant de chaque acteur permet de retrouver ses films (voir enrich_people). Un acteur sans identifiant lisible n'a pas d'entrée dans le dictionnaire, ni un nom que deux acteurs du casting portent avec des identifiants différents (deux homonymes : on ne sait pas lequel est lequel, comme ailleurs dans le hub)."""
    try:
        people = tmdb.aggregate_credits(show_id).get("cast", [])
    except TmdbAuthError:
        raise
    except TmdbError:
        return None, None
    people = sorted((p for p in people if p.get("name")), key=lambda p: (-(p.get("total_episode_count") or 0), p.get("order", 9999)))[:CAST_SIZE]
    valid = [p for p in people if isinstance(p.get("id"), int) and not isinstance(p.get("id"), bool) and p["id"] > 0]
    seen = {}
    for p in valid:
        seen.setdefault(peoplefile.key_of(p["name"]), set()).add(p["id"])
    ids = {p["name"]: p["id"] for p in valid if len(seen[peoplefile.key_of(p["name"])]) == 1}
    return [{"name": person["name"], "role": main_role(person)} for person in people], ids


def cast(tmdb, show_id):
    """Le casting de la série entière (voir cast_with_ids), sans les identifiants ; vide quand TMDB ne répond pas."""
    return cast_with_ids(tmdb, show_id)[0] or []


def main_role(person):
    """Le rôle où la personne joue le plus d'épisodes ; vide si TMDB n'en donne aucun."""
    roles = sorted(person.get("roles") or [], key=lambda r: -(r.get("episode_count") or 0))
    return (roles[0].get("character") or "") if roles else ""


def tmdb_client(cfg, read=True):
    """(client TMDB, dossier des affiches) ; RuntimeError sans clé. Le dossier des affiches est créé."""
    if not cfg.tmdb_key:
        raise RuntimeError("Pas de clé TMDB : renseigne TMDB_KEY (variable d'environnement, ou .env dans media_library).")
    tmdb = Tmdb(cfg.tmdb_key, "fr-FR", user_agent="media_hub/1.0", cache=tmdb_cache.Cache(folder=cfg.data / "tmdb-cache", read=read))
    posters = cfg.data / "tmdb"
    posters.mkdir(parents=True, exist_ok=True)
    return tmdb, posters


def show_key(lib, item):
    """La fiche gardée d'un élément, que le scan reprend (voir scan.enriched_for) : tout ce que TMDB dit d'une série ; d'un film, seulement sa note (ses tags disent le reste)."""
    return f"enrich:{lib.id}:{item['id']}"


def pin_key(item):
    """Où se garde la fiche TMDB choisie à la main pour une série (voir choose_show). Hors du préfixe `enrich:` : le scan reprend celui-là tel quel, ce choix-ci n'est pas une donnée de TMDB."""
    return f"tmdb-pin:{item['id']}"


def doubts_of(printed):
    """Les doutes de la recherche (ses lignes « /!\\ »), sans le conseil qui vise la ligne de commande de media_library (« force --tmdb-id »)."""
    return [line[4:].split(" -> ")[0].strip() for line in printed.splitlines() if line.startswith("/!\\ ")]


def show_is_done(db, lib, item, force):
    """Déjà enrichie, avec les saisons, le casting, les doutes de la recherche et les identifiants des acteurs (une fiche d'avant leur ajout est refaite) ; `force` refait tout."""
    before = db.kv_get(show_key(lib, item))
    return not force and bool(before) and "season_summaries" in before and "doubts" in before and "person_ids" in before


def show_facts(show, show_id):
    return {
        "summary": show.get("overview") or "", "genres": [g.get("name", "") for g in show.get("genres", []) if g.get("name")],
        "year": (show.get("first_air_date") or "")[:4], "tmdb": int(show_id), "tmdb_name": show.get("name") or "",
        "seasons_total": show.get("number_of_seasons"), "episodes_total": show.get("number_of_episodes"), "status": show.get("status") or "",
    }


def enrich_show(tmdb, posters, db, lib, item):
    """Enrichit une série et garde le résultat (base et affiche sur le disque) ; (fiche TMDB, données gardées), ou None si TMDB ne la trouve pas."""
    meta = json.loads(item["meta"])
    folder = lib.path / meta.get("folder", "")
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        show_id = lookup.resolve_show_id(tmdb, folder, db.kv_get(pin_key(item)) or meta.get("tmdb_pinned") or None)         # le choix fait dans le hub passe devant le nom du dossier
    if not show_id:
        return None
    show = tmdb.series(show_id)
    data = show_facts(show, show_id)
    data["tmdb_rating"] = rating_of(show, show_id)
    data["doubts"] = doubts_of(printed.getvalue())          # toujours écrit, même vide : `meta` se fusionne, un doute levé doit disparaître
    if show.get("poster_path"):
        name = f"{show_id}.jpg"
        tmdb.save_image(show["poster_path"], "w500", posters / name)
        data["poster_file"] = name
    data["season_summaries"] = season_summaries(tmdb, show_id, meta.get("seasons", []))
    actors, ids = cast_with_ids(tmdb, show_id)
    if actors is None:             # TMDB n'a pas répondu : on garde ce qu'on avait, et sans `person_ids` la série sera refaite à la prochaine passe (voir show_is_done)
        before = db.kv_get(show_key(lib, item)) or {}
        data["actors"] = before.get("actors", [])
        if "person_ids" in before:
            data["person_ids"] = before["person_ids"]
    else:
        data["actors"], data["person_ids"] = actors, ids          # `person_ids` toujours écrit, même vide : la série n'est pas refaite à chaque passe
    db.kv_set(show_key(lib, item), data)
    fields = {"summary": data["summary"], "genres": data["genres"], "meta": {k: v for k, v in data.items() if k not in ("summary", "genres", "year")}}
    if data["year"]:
        fields["year"] = data["year"]
    db.update_item_meta(item["id"], **fields)
    return show, data


def choose_show(tmdb, posters, db, lib, item, show_id):
    """Fixe la fiche TMDB de la série et l'enrichit à nouveau ; rend ce que rend enrich_show. Si TMDB refuse, le choix d'avant est rendu."""
    before = db.kv_get(pin_key(item))
    db.kv_set(pin_key(item), int(show_id))
    try:
        return enrich_show(tmdb, posters, db, lib, item)
    except BaseException:
        if before:
            db.kv_set(pin_key(item), before)
        else:
            db.kv_delete(pin_key(item))
        raise


def enrich_shows(cfg, db, lib, log=print, force=False):
    tmdb, posters = tmdb_client(cfg)
    done = failed = 0
    for item in db.query("SELECT * FROM items WHERE lib=? AND type='show' ORDER BY sort", (lib.id,)):
        if show_is_done(db, lib, item, force):
            continue
        try:
            found = enrich_show(tmdb, posters, db, lib, item)
        except TmdbAuthError as e:
            raise RuntimeError(f"TMDB : {e}")
        except (TmdbError, OSError) as e:
            log(f"  {item['title']} : {e}")
            failed += 1
            continue
        if found is None:
            log(f"  {item['title']} : série introuvable sur TMDB")
            failed += 1
        else:
            done += 1
            log(f"  {item['title']} -> {found[0].get('name')} ({found[1]['year']})")
            for doubt in found[1].get("doubts", []):
                log(f"    /!\\ {doubt} : à vérifier sur la fiche (Changer la fiche TMDB)")
    return {"enriched": done, "failed": failed}


RATING_STALE = 30 * 24 * 3600   # au-delà, une note est redemandée : les votes continuent d'arriver
RATED_TYPES = {"movies": ("movie", "movie/{}"), "shows": ("show", "tv/{}")}          # sorte de bibliothèque -> (type des éléments, adresse TMDB de leur fiche)


def rating_of(sheet, tmdb_id, now=None):
    """La note d'une fiche TMDB (film ou série) : {"tmdb", "vote", "votes", "at"}. `vote` vaut None quand personne n'a voté (TMDB dit alors 0) ; `tmdb` dit de quelle fiche elle vient, `at` quand on l'a demandée."""
    votes = int(sheet.get("vote_count") or 0)
    vote = sheet.get("vote_average")
    return {"tmdb": int(tmdb_id), "vote": round(float(vote), 1) if votes and vote else None, "votes": votes, "at": int(now if now is not None else time.time())}


def rating_is_fresh(rating, tmdb_id, now, force=False, since=None):
    """Une note qu'on n'a pas à redemander : de la même fiche TMDB (un film réétiqueté change d'identifiant) et de moins de RATING_STALE. Avec `force`, seulement si elle date de `since` ou après (la tâche en cours vient de la demander : la fiche d'une série la donne)."""
    if not isinstance(rating, dict) or rating.get("tmdb") != tmdb_id:
        return False
    at = rating.get("at") or 0
    return (since is not None and at >= since) if force else now - at < RATING_STALE


def keep_rating(db, lib, item, rating):
    """Garde une note dans la fiche de l'élément (un scan la reprend) et dans l'élément lui-même (elle se voit sans attendre le scan). Rend False, sans rien écrire, si l'élément a changé de fiche TMDB pendant qu'on la demandait (« Changer la fiche TMDB » ne passe pas par les tâches) : la note serait celle d'une autre."""
    now = db.one("SELECT * FROM items WHERE id=?", (item["id"],))
    if now is None or sagafile.tmdb_of(now) != rating["tmdb"]:
        return False
    key = show_key(lib, item)
    db.kv_set(key, {**(db.kv_get(key) or {}), "tmdb_rating": rating})
    db.update_item_meta(item["id"], meta={"tmdb_rating": rating})
    return True


def enrich_ratings(cfg, db, lib, log=print, force=False, since=None):
    """Les notes TMDB des films ou des séries d'une bibliothèque : ceux qui ont un identifiant TMDB (l'étiquette d'un film, la fiche d'une série) et pas de note fraîche (voir rating_is_fresh).

    Toujours demandées à TMDB lui-même, pas au cache : c'est la fraîcheur qu'on vient chercher. Une fiche qui échoue est comptée, notée au journal et redemandée la fois suivante. `since` : le début de la tâche (voir rating_is_fresh).
    """
    kind, endpoint = RATED_TYPES[lib.kind]
    tmdb, _ = tmdb_client(cfg, read=False)
    now = int(time.time())
    counts = {"rated": 0, "failed": 0}
    for item in db.query("SELECT * FROM items WHERE lib=? AND type=? ORDER BY sort", (lib.id, kind)):
        tmdb_id = sagafile.tmdb_of(item)
        if tmdb_id is None or rating_is_fresh(json.loads(item["meta"]).get("tmdb_rating"), tmdb_id, now, force, since):
            continue
        try:
            sheet = tmdb.get(endpoint.format(tmdb_id))
        except TmdbAuthError as e:
            raise RuntimeError(f"TMDB : {e}")
        except TmdbError as e:
            log(f"  {item['title']} : pas de note ({e})")
            counts["failed"] += 1
            continue
        if keep_rating(db, lib, item, rating_of(sheet, tmdb_id, now)):
            counts["rated"] += 1
        else:
            log(f"  {item['title']} : fiche TMDB changée pendant la passe, note laissée")
    rated, failed = counts["rated"], counts["failed"]
    log(f"  Notes TMDB : {rated} demandée(s), {failed} en échec" if failed else f"  Notes TMDB : {rated} demandée(s)")         # en toutes lettres : l'interface traduit ces lignes (lang-en.js)
    return counts


def download_poster(tmdb, posters, film, title, log, force):
    """Le nom du fichier de l'affiche de `film` (sa fiche TMDB : `id` et `poster_path`), sous `posters`, téléchargée si elle n'y est pas (ou avec `force`) ; vide quand elle ne vient pas. Les sagas et les personnes partagent ces fichiers."""
    name = missing.poster_name(film["id"])
    try:
        if force or not (posters / name).is_file():
            partial = posters / (name + ".tmp")
            tmdb.save_image(film["poster_path"], "w342", partial)
            os.replace(partial, posters / name)         # d'un bloc : une écriture coupée ne laisse pas une affiche tronquée qu'on ne retélécharge plus
        return name
    except TmdbAuthError:
        raise
    except (TmdbError, OSError) as e:
        log(f"  {title} : pas d'affiche ({e})")
        return ""


class SagaPass:
    """Une passe sur les sagas d'une bibliothèque de films : ce qu'elle décrit se range dans `info` (voir missing.py), qui se garde à la fin de la passe."""

    def __init__(self, tmdb, posters, info, owned, log, force):
        self.tmdb, self.posters, self.info, self.owned, self.log, self.force = tmdb, posters, info, owned, log, force
        self.now = int(time.time())
        self.counts = {"sagas": 0, "films": 0, "failed": 0}

    def poster(self, film, title):
        """Le nom du fichier de l'affiche d'un film, téléchargée au besoin ; vide quand elle ne vient pas."""
        return download_poster(self.tmdb, self.posters, film, title, self.log, self.force)

    def keep(self, film):
        """Retient un film que TMDB décrit (titre, date, affiche)."""
        ident = film["id"]
        date_ = film.get("release_date") or ""
        data = {"title": film.get("title") or "", "date": date_, "year": date_[:4], "poster": ""}
        if film.get("poster_path"):
            data["poster"] = self.poster(film, data["title"])
        self.info["films"][str(ident)] = data
        self.counts["films"] += 1

    def describe(self, ident):
        """Décrit un film d'après sa fiche TMDB (un film dont la collection ne le donne pas, ou que seule une saga du fichier réclame)."""
        try:
            self.keep(self.tmdb.get(f"movie/{ident}"))
        except TmdbAuthError:
            raise
        except TmdbError as e:
            self.log(f"  film {ident} : {e}")
            self.counts["failed"] += 1

    def collection_of(self, name, members):
        """(identifiant de la collection TMDB, ses films) de la saga `name`, d'après un de ses films ; None, et une ligne au journal, quand TMDB ne la donne pas."""
        ident = next((i for i in map(sagafile.tmdb_of, members) if i is not None), None)
        if ident is None:
            self.log(f"  {name} : aucun film n'a d'identifiant TMDB")
            return None
        try:
            collection = (self.tmdb.get(f"movie/{ident}").get("belongs_to_collection") or {}).get("id")
            if not collection:
                self.log(f"  {name} : TMDB ne rattache ce film à aucune collection")
                self.counts["failed"] += 1
                return None
            return collection, self.tmdb.collection(collection).get("parts") or []
        except TmdbAuthError:
            raise
        except TmdbError as e:
            self.log(f"  {name} : {e}")
            self.counts["failed"] += 1
            return None

    def describe_missing(self, known):
        """Une collection connue avant qu'on décrive aussi les films qu'on a : ceux qui ont quitté la médiathèque depuis le sont maintenant."""
        for ident in known["films"]:
            if ident not in self.owned and str(ident) not in self.info["films"]:
                self.describe(ident)

    def refresh(self, name, members):
        """Décrit la collection de la saga `name`, sauf si on la connaît d'assez près (alors ne sont décrits que les films qu'on ne connaît pas encore)."""
        known = self.info["collections"].get(name)
        if known and not self.force and self.now - known.get("at", 0) < missing.STALE:
            return self.describe_missing(known)
        found = self.collection_of(name, members)
        if found is None:
            return
        collection, parts = found
        parts = sorted((p for p in parts if p.get("id")), key=lambda p: (p.get("release_date") or "9999", p["id"]))
        for part in parts:
            self.keep(part)
        self.info["collections"][name] = {"id": collection, "films": [p["id"] for p in parts], "at": self.now}
        self.counts["sagas"] += 1
        self.report(name, parts)

    def report(self, name, parts):
        absent = [p for p in parts if p["id"] not in self.owned]
        coming = [p for p in absent if not missing.released({"date": p.get("release_date")})]
        self.log(f"  {name} : {len(parts)} film(s), {len(absent) - len(coming)} absent(s)" + (f", {len(coming)} pas encore sorti(s)" if coming else ""))

    def wanted(self, path):
        """Les films que les sagas de `sagas.json` listent sans qu'on les ait, et qu'on n'a pas encore décrits (tous, avec `force`)."""
        wanted = []
        for _, ids in sagafile.Sagas(path).all():
            wanted += [i for i in ids if i not in self.owned and i not in wanted and (self.force or str(i) not in self.info["films"])]
        return wanted


def film_groups(rows):
    """{nom de la saga de l'étiquette: ses films}."""
    groups = {}
    for row in rows:
        if row["grp"]:
            groups.setdefault(row["grp"], []).append(row)
    return groups


def enrich_sagas(cfg, db, lib, log=print, force=False):
    """Les films qui manquent aux sagas d'une bibliothèque de films : ceux des collections TMDB que dit l'étiquette, et ceux que les sagas de `sagas.json` listent sans qu'on les ait.

    Tous les films d'une collection sont décrits, ceux qu'on a comme les autres : un film retiré de la médiathèque doit pouvoir apparaître grisé sans attendre qu'on redemande la collection. Une collection déjà connue n'est redemandée qu'après un mois (ou avec `force`) ; un film décrit ne l'est pas deux fois. Le résultat se garde au fur et à mesure (voir missing.py) : une clé refusée à mi-chemin n'efface pas ce qui est fait.
    """
    tmdb, posters = tmdb_client(cfg, read=not force)
    rows = db.query("SELECT * FROM items WHERE lib=? AND type='movie' ORDER BY sort", (lib.id,))
    run = SagaPass(tmdb, posters, missing.load(db), sagafile.by_tmdb(rows), log, force)
    try:
        for name, members in sorted(film_groups(rows).items()):
            run.refresh(name, members)
        for ident in run.wanted(cfg.data / "sagas.json"):
            run.describe(ident)
    except TmdbAuthError as e:
        raise RuntimeError(f"TMDB : {e}")
    finally:
        missing.save(db, run.info)
    return run.counts


PERSON_STALE = 30 * 24 * 3600       # au-delà, la filmographie d'une personne est redemandée à TMDB (elle a peut-être tourné un film de plus, ou un film annoncé est sorti)
MAX_ORDER = 10                      # un acteur n'est retenu que parmi les premiers rôles du casting (« order » de TMDB, à partir de 0)...
MIN_VOTES = 100                     # ... et dans un film que au moins autant de personnes ont noté : on ne cherche pas les figurants ni les films inconnus
MAX_FILMS = 40


def person_key(tmdb_id):
    """Où se garde la filmographie d'une personne (voir enrich_person). Hors du préfixe `enrich:` : le scan reprend celui-là, ce qui suit n'est pas une fiche d'élément."""
    return f"personinfo:{tmdb_id}"


def _credits(credits, key):
    """Les films de la liste `key` (« cast » ou « crew ») d'une réponse de TMDB qui ont un identifiant ; le reste est écarté."""
    listed = credits.get(key) if isinstance(credits, dict) else None
    return [film for film in listed if isinstance(film, dict) and isinstance(film.get("id"), int)] if isinstance(listed, list) else []


def is_top_billed(film):
    """Un rôle d'acteur qui compte : parmi les `MAX_ORDER` premiers du casting, dans un film noté par au moins `MIN_VOTES` personnes."""
    order, votes = film.get("order"), film.get("vote_count")
    return isinstance(order, int) and isinstance(votes, int) and order <= MAX_ORDER and votes >= MIN_VOTES


def person_films(credits):
    """[{tmdb, title, year, date, role, poster_path}] des films que TMDB dit d'une personne (`person/<id>/movie_credits`), du plus récent au plus ancien, `MAX_FILMS` au plus.

    Réalisateur : `job` vaut « Director ». Acteur : voir `is_top_billed`. Seulement des films sortis (voir missing.released) ; un film réalisé **et** joué n'est listé qu'une fois, comme réalisé."""
    chosen = [(film, "director") for film in _credits(credits, "crew") if film.get("job") == "Director"] + [(film, "actor") for film in _credits(credits, "cast") if is_top_billed(film)]
    found = {}
    for film, role in chosen:
        when = film.get("release_date") or ""
        if missing.released({"date": when}):
            found.setdefault(film["id"], {"tmdb": film["id"], "title": film.get("title") or "", "year": when[:4], "date": when, "role": role, "poster_path": film.get("poster_path") or ""})
    return sorted(found.values(), key=lambda film: (film["date"], film["tmdb"]), reverse=True)[:MAX_FILMS]


def is_fresh(known, now, force):
    """La filmographie gardée d'une personne est-elle assez récente (moins de `PERSON_STALE`) pour ne pas la redemander ? Jamais avec `force`."""
    return bool(known) and not force and now - known.get("at", 0) < PERSON_STALE


def fetch_person(tmdb, posters, db, person_id, now, log, force, owned=()):
    """Demande à TMDB les films de la personne `person_id` et les garde sous `personinfo:<id>` ; `{"films": n, "failed": 0}`, ou `{"films": 0, "failed": 1}` (et une ligne au journal) quand TMDB refuse cette personne : ce qui était gardé reste.
    Pas d'affiche pour les films de `owned` (ceux qu'on a : ils n'en ont pas besoin, et c'est ce qui fait la différence sur des milliers de personnes) ; une clé refusée arrête tout (RuntimeError)."""
    try:
        films = person_films(tmdb.get(f"person/{person_id}/movie_credits"))
        kept = [{**{k: v for k, v in film.items() if k != "poster_path"},
                 "poster": download_poster(tmdb, posters, {"id": film["tmdb"], "poster_path": film["poster_path"]}, film["title"], log, force) if film["poster_path"] and film["tmdb"] not in owned else ""} for film in films]
    except TmdbAuthError as e:
        raise RuntimeError(f"TMDB : {e}")
    except TmdbError as e:
        log(f"  personne {person_id} : {e}")
        return {"films": 0, "failed": 1}
    db.kv_set(person_key(person_id), {"at": now, "films": kept})
    return {"films": len(kept), "failed": 0}


def enrich_person(cfg, db, person_id, log=print, force=False):
    """Les films que TMDB attribue à la personne `person_id` (réalisés ou joués, voir `person_films`), gardés sous `personinfo:<id>` avec leurs affiches (HUB_DATA/tmdb) : `{"at", "films": [{tmdb, title, year, date, role, poster}]}`.

    Les films qu'on a ne sont pas retirés ici : cela se calcule à chaque requête (voir missing.of_person), la médiathèque change plus vite que TMDB. Une personne connue depuis moins de `PERSON_STALE` n'est pas redemandée (ni même le client TMDB créé), sauf avec `force`. Un échec de TMDB laisse ce qui était gardé."""
    known = db.kv_get(person_key(person_id))
    now = int(time.time())
    if is_fresh(known, now, force):
        return {"films": len(known.get("films") or []), "failed": 0}
    tmdb, posters = tmdb_client(cfg, read=not force)
    counts = fetch_person(tmdb, posters, db, person_id, now, log, force)
    if not counts["failed"]:
        log(f"  personne {person_id} : {counts['films']} film(s) retenu(s)")
    return counts


PEOPLE_PROGRESS = 100        # une ligne de journal toutes les 100 personnes : le journal d'une tâche ne garde que 400 lignes
PEOPLE_MAX_NOTES = 40        # lignes d'échec ou d'affiche manquante écrites au journal ; les suivantes sont comptées sans être écrites (elles chasseraient le début du journal)
PEOPLE_MAX_FAILS = 15        # échecs de suite au bout desquels TMDB est jugé injoignable : la passe s'arrête (une tâche ne reste pas des heures sur un réseau coupé, et elle reprendra où elle en était)


def enrich_people(cfg, db, libs, log=print, force=False):
    """Les films de tout le monde (voir enrich_person) : les réalisateurs et les acteurs de premier plan des films de ces bibliothèques `libs` (objets Library) dont le nom désigne une seule personne (voir people.bulk_ids). `{"people", "asked", "fresh", "failed", "films"}`.

    Un seul client TMDB pour toute la passe. Chaque personne est gardée dès qu'elle est faite : une passe interrompue (clé refusée, redémarrage) reprend où elle en était, les personnes connues depuis moins de `PERSON_STALE` étant sautées (sauf `force`). Une personne que TMDB refuse est comptée en échec et la passe continue. Pas d'affiche pour les films qu'on a."""
    tmdb, posters = tmdb_client(cfg, read=not force)
    ids = peoplefile.bulk_ids(db, [lib.id for lib in libs], MAX_ORDER + 1)
    owned, now = missing.owned_films(db, cfg.libraries), int(time.time())          # « qu'on a » vaut pour toutes les bibliothèques, pas seulement celles de la passe : pas d'affiche pour un film rangé ailleurs
    counts = {"people": len(ids), "asked": 0, "fresh": 0, "failed": 0, "films": 0}
    log(f"  {len(ids)} personne(s) à parcourir")
    notes, streak = [], 0

    def note(line):
        notes.append(line)
        if len(notes) <= PEOPLE_MAX_NOTES:
            log(line)
        elif len(notes) == PEOPLE_MAX_NOTES + 1:
            log("  … les lignes suivantes de ce genre ne sont pas écrites")

    for n, ident in enumerate(ids, 1):
        if is_fresh(db.kv_get(person_key(ident)), now, force):
            counts["fresh"] += 1
        else:
            done = fetch_person(tmdb, posters, db, ident, now, note, force, owned)
            counts["asked"] += 1
            counts["failed"] += done["failed"]
            counts["films"] += done["films"]
            streak = streak + 1 if done["failed"] else 0
            if streak >= PEOPLE_MAX_FAILS:
                raise RuntimeError(f"TMDB injoignable : {streak} échecs de suite, passe interrompue (relance-la pour reprendre où elle en était)")
        if n % PEOPLE_PROGRESS == 0 and n < len(ids):
            log(f"  {n}/{len(ids)} personne(s)")
    log(f"  {counts['asked']} interrogée(s), {counts['fresh']} déjà connue(s), {counts['films']} film(s) retenu(s), {counts['failed']} en échec")
    return counts


def artist_key(mbid):
    return f"artist:{mbid}"


def links_key(mbid):
    """Les liens que l'administrateur a retirés ou ajoutés à la fiche d'un artiste : à part de `artist_key`, que « Fiches artistes » réécrit en entier à chaque rafraîchissement."""
    return f"artistlinks:{mbid}"


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


def image_ext(data):
    """L'extension (.jpg, .png ou .webp) de l'image que ces octets contiennent, d'après leur début ; None si ce n'en est pas une."""
    ext = next((ext for magic, ext in PHOTO_TYPES if data.startswith(magic)), None)
    return None if ext == ".webp" and data[8:12] != b"WEBP" else ext


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
    data, ext = converted_photo(cfg, data, box)
    folder, stem = (home, PHOTO_STEM) if home and ext == ".webp" else (legacy, artist_id)
    target = folder / store_photo(folder, stem, data, ext)
    for path in other_photos(legacy, artist_id, mbid, home):
        if path != target:
            try:
                path.unlink()
            except OSError:
                pass
    return target


def converted_photo(cfg, data, box):
    """(octets, extension) de la photo à garder : en WebP rognée à `box` quand on sait convertir ; telle quelle sinon, ce qui exclut de la rogner."""
    if images.can_webp(cfg.ffmpeg):
        return images.to_webp(data, cfg.ffmpeg, box), ".webp"
    if box:
        raise images.NoConverter("ni Pillow ni ffmpeg : pas de recadrage possible")
    return data, image_ext(data)


def other_photos(legacy, artist_id, mbid, home):
    """Tous les fichiers où l'artiste a pu avoir une photo, ancien nom compris : ni dans artists/, ni dans l'autre dossier, il n'en reste qu'une."""
    stale = [legacy / f"{s}{e}" for s in (artist_id, f"custom-{artist_id}", mbid) if s and SAFE_STEM.fullmatch(s) for e in PHOTO_EXTS]
    return stale + [home / f"{PHOTO_STEM}.webp"] if home else stale


def musicbrainz(cfg):
    return MusicBrainz(user_agent=MB_USER_AGENT, cache=tmdb_cache.Cache(folder=cfg.data / "musicbrainz-cache", read=True))


def album_artists(db, lib):
    """({identifiant MusicBrainz: nom}, {identifiant MusicBrainz: {identifiants d'artiste}}, {identifiant d'artiste: son dossier}) d'après les albums de la bibliothèque ; seuls comptent les artistes qui portent un identifiant MusicBrainz."""
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
    return artists, ids, homes


def sheet_is_fresh(kept):
    """La fiche gardée se passe d'être refaite : récente et de la version d'aujourd'hui."""
    return bool(kept) and not artistinfo.stale(kept) and kept["info"].get("sheet") == SHEET_VERSION


def enrich_artists(cfg, db, lib, log=print, force=False):
    """La fiche de chaque artiste de la bibliothèque de musique qui porte un identifiant MusicBrainz : type, pays, dates, membres, liens, résumé Wikipédia.

    Sans clé (MusicBrainz n'en demande pas), mais à raison d'une requête par seconde : la première passe est longue. Une fiche de moins de 30 jours n'est pas redemandée, sauf `force`. Elle est gardée dans la base : un rescan ne l'efface pas. Aucune photo n'est téléchargée : celle d'un artiste est celle que l'utilisateur envoie.
    """
    mb = musicbrainz(cfg)
    artists = album_artists(db, lib)[0]
    done = failed = 0
    for mbid, name in sorted(artists.items(), key=lambda kv: kv[1].casefold()):
        if not force and sheet_is_fresh(db.kv_get(artist_key(mbid))):
            continue
        try:
            info = fetch_artist(mb, mbid)
        except MusicBrainzError as e:
            log(f"  {name} : {e}")
            failed += 1
            continue
        db.kv_set(artist_key(mbid), {"fetched": date.today().isoformat(), "info": info})
        done += 1
        log(f"  {name} : {artistinfo.facts(info) or 'fiche vide'}")
    return {"enriched": done, "failed": failed}
