"""python -m hub : lance le serveur.

    python -m hub user list
    python -m hub user add <nom> [--admin]     (le premier compte est toujours administrateur)
    python -m hub user passwd <nom>
    python -m hub user rm <nom>
    python -m hub user rights <nom> [--set <bibliothèque>=all|play|view|none ...]   (ce que le compte voit, lit et télécharge)

    python -m hub backup [archive.zip]         (sauvegarde base, sagas, affiches choisies, photos d'artistes ; serveur allumé ou non)
    python -m hub restore <archive.zip> [--replace]   (serveur arrêté ; --replace met l'installation actuelle de côté)

    python -m hub recommend <nom> [--limit N] [--day AAAA-MM-JJ]   (les rayons « Parce que tu as aimé » du compte, chaque carte avec son score détaillé ; lecture seule)

Les comptes se gèrent sans le serveur (il peut tourner pendant ce temps) : seule la base de HUB_DATA est lue. Pas d'inscription publique.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import __version__, accounts, backup, config, labels, runlock


def read_secret(prompt):
    """getpass au clavier (rien ne s'affiche) ; depuis un tube, une ligne de l'entrée standard par saisie : le mot de passe est demandé deux fois, donc `printf 'mdp\\nmdp\\n' | python -m hub user add <nom>`."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\r\n")


def ask_password(label="Mot de passe"):
    first = read_secret(f"{label} : ")
    if first != read_secret("Encore une fois : "):
        raise SystemExit("les deux saisies ne correspondent pas.")
    return first


def user_parser():
    parser = argparse.ArgumentParser(prog="python -m hub user", description="Gère les comptes de media_hub.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="liste les comptes")
    add = sub.add_parser("add", help="crée un compte")
    add.add_argument("name", help="nom du compte")
    add.add_argument("--admin", action="store_true", help="en fait un administrateur (le premier compte l'est toujours)")
    sub.add_parser("passwd", help="change le mot de passe d'un compte").add_argument("name")
    sub.add_parser("rm", help="supprime un compte, sa progression et ses playlists").add_argument("name")
    rights = sub.add_parser("rights", help="montre, ou change avec --set, ce qu'un compte voit, lit et télécharge par bibliothèque")
    rights.add_argument("name")
    rights.add_argument("--set", action="append", default=[], metavar="BIBLIOTHEQUE=DROITS", help="all (voir, lire, télécharger), play (voir et lire, sans télécharger), view (voir seulement) ou none (rien) ; peut se répéter")
    return parser


def known_account(db, name):
    row = accounts.by_name(db, name)
    if row is None:
        raise SystemExit(f"compte inconnu : {name}")
    return row


def user_list(db, args):
    for row in accounts.rows(db):
        print(f"{row['name']:<24} {row['role']:<6} {row['id']}")


def user_add(db, args):
    first = not accounts.exist(db)
    row = accounts.create(db, args.name, ask_password(), "admin" if args.admin else "user")
    print(f"compte « {row['name']} » créé ({row['role']}" + (", il reprend la progression et les playlists d'avant les comptes" if first else "") + ").")


def user_passwd(db, args):
    row = known_account(db, args.name)
    accounts.set_password(db, row["id"], ask_password("Nouveau mot de passe"))
    print(f"mot de passe de « {row['name']} » changé ; ses sessions ouvertes sont fermées.")


def user_rm(db, args):
    row = known_account(db, args.name)
    accounts.delete(db, row["id"])
    print(f"compte « {row['name']} » supprimé, avec sa progression et ses playlists.")


LEVELS = {"all": {"view": True, "play": True, "download": True}, "play": {"view": True, "play": True, "download": False}, "view": {"view": True, "play": False, "download": False},
          "none": {"view": False, "play": False, "download": False}}


def user_rights(db, args):
    row = known_account(db, args.name)
    lib_ids = [lib.id for lib in config.load().libraries]
    changes = {}
    for text in args.set:
        lib, _, level = text.partition("=")
        if level not in LEVELS:
            raise SystemExit(f"« {text} » : attendu <bibliothèque>=all|play|view|none")
        changes[lib] = LEVELS[level]
    granted = accounts.set_rights(db, row["id"], set(lib_ids), changes) if changes else accounts.grants(db, row["id"])
    print(f"{row['name']} ({row['role']})" + (" : un administrateur a tous les droits" if row["role"] == "admin" else ""))
    for lib, rights in accounts.rights_json(row["role"], granted, lib_ids).items():
        print(f"  {lib:<16} {'voir' if rights['view'] else '----'}  {'lire' if rights['play'] else '----'}  {'télécharger' if rights['download'] else '-----------'}")


USER_ACTIONS = {"list": user_list, "add": user_add, "passwd": user_passwd, "rm": user_rm, "rights": user_rights}


def user_command(argv):
    args = user_parser().parse_args(argv)
    from .db import Database
    db = Database(config.load().data / "hub.db")
    try:
        USER_ACTIONS[args.action](db, args)
    except accounts.AccountError as e:
        raise SystemExit(e.message)


FEATURE_LABELS = {"director": "réalisateur", "saga": "saga", "actor": "acteur", "genre": "genre", "decade": "décennie"}


def card_count(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("au moins 1")
    return value


def recommend_parser():
    parser = argparse.ArgumentParser(prog="python -m hub recommend", description="Montre les rayons « Parce que tu as aimé » d'un compte tels que l'accueil les calcule, chaque carte avec son score et ce qui le compose, "
                                     "pour régler les poids en tête de hub/recommend.py. Lecture seule ; le serveur peut tourner.")
    parser.add_argument("name", help="nom du compte")
    parser.add_argument("--limit", type=card_count, metavar="N", help="cartes montrées par rayon (par défaut : toutes)")
    parser.add_argument("--day", metavar="AAAA-MM-JJ", help="le jour qui ordonne les ancres (par défaut : aujourd'hui)")
    return parser


def described(item):
    return f"« {item.row['title']} »" + (f" ({item.row['year'][:4]})" if item.row["year"][:4] else "")


def card_detail(rec, anchor, cand, scope, name):
    """La carte et la ligne qui détaille son score : chaque particularité partagée avec son poids, la note, le malus."""
    score, common, bonus, malus = rec.breakdown(anchor, cand, scope)
    parts = [f"{FEATURE_LABELS[kind]} {text} {weight:+.2f}" for weight, kind, text in common]
    if bonus:
        parts.append(f"note TMDB {cand.vote:g} {bonus:+.2f}")
    if malus:
        parts.append(f"ressemble à ce que {name} n'aime pas {-malus:+.2f}")
    return f"{score:6.2f}  {described(cand)}  [{cand.row['id']}]", " · ".join(parts)


def settings_line(rec):
    weights = " · ".join(f"{FEATURE_LABELS[kind]} {rec.WEIGHTS[kind]:g}" for kind in rec.KINDS)
    return (f"réglages : {weights} · MAX_SHARE {rec.MAX_SHARE:g} · note autour de {rec.QUALITY_PIVOT:g} (×{rec.QUALITY_SLOPE:g}, ±{rec.QUALITY_CAP:g} au plus)"
            f" · malus ×{rec.DOWN_WEIGHT:g} ({rec.DOWN_CAP:g} au plus)")


def playable_video_libraries(db, row, libraries):
    """Les bibliothèques vidéo que ce compte peut lire : celles où l'accueil cherche ses recommandations."""
    from .server.app import UNSEEN_KINDS
    granted = accounts.grants(db, row["id"])
    return {lib.id for lib in libraries if lib.kind in UNSEEN_KINDS and accounts.allows(row["role"], granted, lib.id, "play")}


def print_shelf(rec, anchor, found, scope, opinions, name, limit):
    """Un rayon gardé : son ancre, puis ses cartes (`limit` au plus), chacune avec le détail de son score."""
    print(f"Parce que tu as aimé {described(anchor)}, {'noté J’aime' if anchor.row['id'] in opinions else 'terminé sans avis'}  [{anchor.row['id']}]")
    shown = found[:limit] if limit else found
    for n, (cand, _) in enumerate(shown, 1):
        card, detail = card_detail(rec, anchor, cand, scope, name)
        print(f"  {n:>2}. {card}")
        print(f"        {detail}")
    if len(found) > len(shown):
        print(f"      … et {len(found) - len(shown)} autres")


def recommend_report(db, libraries, argv):
    """Écrit les rayons du compte : chaque ancre essayée, dans l'ordre du jour, et pour un rayon gardé ses cartes, de la plus proche à la moins proche."""
    args = recommend_parser().parse_args(argv)
    from . import missing, recommend as rec
    from .server.app import shown_rating
    row = known_account(db, args.name)
    name, uid, readable = row["name"], row["id"], playable_video_libraries(db, row, libraries)
    opinions, progress = db.dated_ratings_for(uid), rec.progress_of(db, uid)
    idx = rec.index(db, lambda meta: (shown_rating(meta.get("tmdb_rating")) or {}).get("vote"))          # comme GET /api/recommendations
    day, possible = args.day or missing.today(), rec.anchors(idx, opinions, progress, readable)
    liked = sum(ident in opinions for ident in possible)
    print(f"{name}, le {day} : {len(possible)} ancre(s) possible(s) ({liked} aimée(s), {len(possible) - liked} terminée(s)) ; bibliothèques lisibles : {', '.join(sorted(readable)) or 'aucune'}")
    print(settings_line(rec))
    if len(possible) < rec.MIN_ANCHORS:
        print(f"pas assez de signal : il faut au moins {rec.MIN_ANCHORS} films ou séries aimés ou terminés.")
        return
    for anchor, found, keep, scope in rec.attempts(idx, opinions, progress, readable, uid, day):
        print()
        if keep:
            print_shelf(rec, anchor, found, scope, opinions, name, args.limit)
        else:
            print(f"Ancre {described(anchor)} [{anchor.row['id']}] : {len(found)} candidat(s), rayon abandonné (il en faut {rec.SHELF_MIN}).")


def recommend_command(argv):
    cfg = config.load()
    config.use_media_library(cfg.repo)
    from .db import Database
    recommend_report(Database(cfg.data / "hub.db"), cfg.libraries, argv)


def announce(cfg, hub):
    """Ce que le serveur dit au démarrage : l'adresse, les bibliothèques, et ce qui manque ou expose (sur la sortie d'erreur)."""
    print(f"media_hub {__version__} : http://{cfg.bind}:{cfg.port}", flush=True)
    for lib in cfg.libraries:
        print(f"  {labels.shown(hub.db, lib):<12} {lib.kind:<7} {lib.path}" + ("" if lib.path.is_dir() else "  (dossier introuvable)"), flush=True)
    if not cfg.libraries:
        print("  aucune bibliothèque : monte tes dossiers sous HUB_SRV (Films, Series, Musique, Livres) ou décris-les dans HUB_LIBRARIES.", file=sys.stderr)
    problem = hub.inbox.check()
    if problem:
        print(f"  ATTENTION : {problem}", file=sys.stderr)
    if not cfg.ffmpeg:
        print("  ffmpeg introuvable : lecture directe uniquement (voir deploy/README.md).", file=sys.stderr)
    if cfg.bind not in ("127.0.0.1", "localhost", "::1") and not hub.auth():
        print("  ATTENTION : écoute sur le réseau sans compte (HUB_PASSWORD, ou python -m hub user add).", file=sys.stderr)
    if cfg.password and len(cfg.password) < accounts.MIN_PASSWORD:
        print(f"  ATTENTION : HUB_PASSWORD fait moins de {accounts.MIN_PASSWORD} caractères (accepté pour les installations d'avant, mais trop court si le hub est ouvert à Internet ; il ne sert qu'à créer le premier compte).", file=sys.stderr)


def backup_command(argv):
    parser = argparse.ArgumentParser(prog="python -m hub backup", description="Sauvegarde les données propres au hub (base, sagas, affiches choisies, photos d'artistes), sans les médias ni les caches. Le serveur peut tourner pendant ce temps.")
    parser.add_argument("archive", nargs="?", help=f"fichier .zip à écrire (par défaut : HUB_DATA/{backup.BACKUPS}/<nom daté>)")
    args = parser.parse_args(argv)
    data = config.load().data
    out = Path(args.archive) if args.archive else data / backup.BACKUPS / backup.archive_name()
    try:
        manifest = backup.create(data / "hub.db", data, out)
    except (backup.BackupError, OSError) as e:
        raise SystemExit(f"sauvegarde impossible : {getattr(e, 'message', e)}")
    print(f"sauvegarde écrite : {out} ({len(manifest['files'])} fichiers, base en version {manifest['schema']}).")
    print("Elle contient les hachés des mots de passe et la clé des sessions, sans chiffrement : garde-la comme un mot de passe.")


def restore_command(argv):
    parser = argparse.ArgumentParser(prog="python -m hub restore", description="Restaure une sauvegarde de `python -m hub backup` ou de Gestion. Serveur arrêté.")
    parser.add_argument("archive", help="le fichier .zip de la sauvegarde")
    parser.add_argument("--replace", action="store_true", help=f"remplace une installation existante (mise de côté dans HUB_DATA/{backup.ASIDE}-<date>/, rien n'est effacé)")
    args = parser.parse_args(argv)
    data = config.load().data
    try:
        done = backup.restore(args.archive, data / "hub.db", data, replace=args.replace)
    except (backup.BackupError, OSError) as e:
        raise SystemExit(f"restauration refusée : {getattr(e, 'message', e)}")
    print(f"sauvegarde restaurée dans {data} ({done['files']} fichiers, base en version {done['schema']} ; elle sera mise à jour au démarrage si besoin).")
    if done["aside"]:
        print(f"l'installation d'avant est dans {done['aside']} : supprime ce dossier une fois le hub vérifié.")


COMMANDS = {"user": "user_command", "backup": "backup_command", "restore": "restore_command", "recommend": "recommend_command"}      # par nom : la fonction se cherche à l'appel


def main():
    main_with(sys.argv[1:])


def main_with(argv):
    if argv[:1] and argv[0] in COMMANDS:
        globals()[COMMANDS[argv[0]]](argv[1:])
        return
    cfg = config.load()
    lock = runlock.hold(cfg.data)          # gardé tant que le serveur tourne : une restauration le voit et refuse (voir runlock.py)
    try:
        serve(cfg, lock)
    finally:
        runlock.release(lock)


def serve(cfg, lock):
    config.use_media_library(cfg.repo)
    from .server.app import make_server
    server = make_server(cfg)
    hub = server.hub
    announce(cfg, hub)
    if lock is None:
        print(f"  ATTENTION : un autre serveur (ou une restauration) utilise déjà {cfg.data} : deux serveurs sur les mêmes données s'y contredisent ; arrête l'un des deux.", file=sys.stderr)
    hub.first_scan()
    hub.jobs.probe_pending()
    hub.autoscan()
    hub.inbox.migrate()
    hub.inbox.keep_clean()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("arrêt.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
