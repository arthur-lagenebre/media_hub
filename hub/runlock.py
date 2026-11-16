"""Le verrou de HUB_DATA : qui se sert des données en ce moment.

Le serveur le prend au démarrage et le garde tant qu'il tourne ; une restauration (backup.restore) le prend avant de toucher aux données et le
garde jusqu'au bout. Si un serveur tourne, la restauration est refusée ; si une restauration est en cours, un serveur qui démarre le dit.

C'est un verrou du système sur le fichier `HUB_DATA/hub.lock` (`fcntl.flock` sous Linux et macOS, `msvcrt.locking` sous Windows), pas la simple
présence d'un fichier : le système le libère tout seul quand le processus s'arrête, même brutalement (conteneur tué, coupure de courant), et
aucun fichier périmé ne bloque la suite. Le fichier lui-même reste, vide. Deux conteneurs qui montent le même dossier partagent le noyau du NAS :
le verrou de l'un se voit de l'autre.
"""

from __future__ import annotations

from pathlib import Path

try:
    import fcntl
except ImportError:                    # Windows
    fcntl = None
    import msvcrt

NAME = "hub.lock"


def hold(data):
    """Prend le verrou de `data` (créé s'il manque) sans attendre ; rend la poignée à garder ouverte tant qu'on s'en sert, ou None s'il est déjà pris."""
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    handle = open(data / NAME, "a+b")
    try:
        if fcntl:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def release(handle):
    """Rend le verrou pris par `hold` ; None ne fait rien."""
    if handle is None:
        return
    try:
        if fcntl:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()
