"""python -m hub : lance le serveur."""

from __future__ import annotations

import sys

from . import __version__, config


def main():
    cfg = config.load()
    config.use_media_library(cfg.repo)
    from .server.app import make_server
    server = make_server(cfg)
    hub = server.hub
    print(f"media_hub {__version__} : http://{cfg.bind}:{cfg.port}", flush=True)
    for lib in cfg.libraries:
        print(f"  {lib.label:<12} {lib.kind:<7} {lib.path}" + ("" if lib.path.is_dir() else "  (dossier introuvable)"), flush=True)
    if not cfg.libraries:
        print("  aucune bibliothèque : monte tes dossiers sous HUB_SRV (Films, Series, Animations, Musique, Livres) ou décris-les dans HUB_LIBRARIES.", file=sys.stderr)
    if not cfg.ffmpeg:
        print("  ffmpeg introuvable : lecture directe uniquement (voir deploy/README.md).", file=sys.stderr)
    if cfg.bind not in ("127.0.0.1", "localhost", "::1") and not cfg.password:
        print("  ATTENTION : écoute sur le réseau sans mot de passe (HUB_PASSWORD).", file=sys.stderr)
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
