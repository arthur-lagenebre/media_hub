"""python -m hub : lance le serveur.

    python -m hub user list
    python -m hub user add <nom> [--admin]     (le premier compte est toujours administrateur)
    python -m hub user passwd <nom>
    python -m hub user rm <nom>
    python -m hub user rights <nom> [--set <bibliothèque>=all|play|view|none ...]   (ce que le compte voit, lit et télécharge)

Les comptes se gèrent sans le serveur (il peut tourner pendant ce temps) : seule la base de HUB_DATA est lue. Pas d'inscription publique.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import __version__, accounts, config


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


def announce(cfg, hub):
    """Ce que le serveur dit au démarrage : l'adresse, les bibliothèques, et ce qui manque ou expose (sur la sortie d'erreur)."""
    print(f"media_hub {__version__} : http://{cfg.bind}:{cfg.port}", flush=True)
    for lib in cfg.libraries:
        print(f"  {lib.label:<12} {lib.kind:<7} {lib.path}" + ("" if lib.path.is_dir() else "  (dossier introuvable)"), flush=True)
    if not cfg.libraries:
        print("  aucune bibliothèque : monte tes dossiers sous HUB_SRV (Films, Series, Animations, Musique, Livres) ou décris-les dans HUB_LIBRARIES.", file=sys.stderr)
    if not cfg.ffmpeg:
        print("  ffmpeg introuvable : lecture directe uniquement (voir deploy/README.md).", file=sys.stderr)
    if cfg.bind not in ("127.0.0.1", "localhost", "::1") and not hub.auth():
        print("  ATTENTION : écoute sur le réseau sans compte (HUB_PASSWORD, ou python -m hub user add).", file=sys.stderr)


def main():
    if sys.argv[1:2] == ["user"]:
        user_command(sys.argv[2:])
        return
    cfg = config.load()
    config.use_media_library(cfg.repo)
    from .server.app import make_server
    server = make_server(cfg)
    hub = server.hub
    announce(cfg, hub)
    hub.first_scan()
    hub.autoscan()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("arrêt.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
