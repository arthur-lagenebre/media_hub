---
name: hub-server
description: Développe et corrige le serveur de media_hub (hub/server, hub/db.py, hub/scan, hub/media, hub/enrich.py, hub/playlists.py, hub/config.py) : routes HTTP, base SQLite, scans des bibliothèques, plan de lecture (direct / remux / transcodage ffmpeg), enrichissement TMDB et MusicBrainz, tâches de fond. À utiliser pour toute tâche côté Python.
tools: Read, Grep, Glob, Edit, Write, Bash
model: sonnet
---

Tu travailles sur le serveur de **media_hub** : un seul processus Python, **bibliothèque standard uniquement** (Pillow est facultatif et doit rester protégé par un `try/except ImportError`). Le hub *lit* les étiquettes écrites par `media_library` (dépôt voisin, importé via `libraries.*`) ; il n'écrit jamais dans les fichiers de la médiathèque.

## Où est quoi

| Fichier | Rôle |
| --- | --- |
| `hub/config.py` | réglages lus dans les variables `HUB_*` ; une nouvelle option s'ajoute ici **et** dans le tableau « Réglages » du README |
| `hub/db.py` | SQLite (WAL, une connexion par fil), tables `items`, `entries`, `progress`, `kv`… ; `SCHEMA_VERSION` |
| `hub/scan/` | un scanner par type de bibliothèque (`movies`, `shows`, `music`, `books`) ; incrémental (taille + date) ; écriture d'une bibliothèque en **une transaction** (`replace_library`) |
| `hub/media/` | `probe.py` (ffprobe, `plan`, `ffmpeg_command`), `subs.py` (WebVTT), `images.py` (cache, Pillow facultatif), `books.py` |
| `hub/server/app.py` | `ThreadingHTTPServer`, routage, authentification, toutes les routes `/api`, `/media`, `/img` |
| `hub/server/jobs.py` | tâches de fond (scan, enrichissement, nettoyage), **une seule à la fois**, dans l'ordre d'arrivée |
| `hub/enrich.py` | TMDB (séries), MusicBrainz / Wikipédia (fiches artistes, photos, liens) |
| `hub/playlists.py` | playlists musicales dans la base |

Lis le fichier concerné et ses voisins avant de modifier : le code est dense, commenté en français, et les helpers existent souvent déjà (`safe_json`, `make_id`, `number(...)`, `HttpError`).

## Règles

1. **Lecture seule sur `HUB_SRV`.** Le hub n'écrit que sous `HUB_DATA`. Exceptions déjà admises : le nettoyage des fichiers parasites, `cover.{ext}` et les photos d'artistes. Toute nouvelle écriture dans une bibliothèque doit être demandée explicitement.
2. **Sécurité.** Chemin venu d'une URL : le résoudre puis vérifier qu'il reste sous la racine (voir le traitement de `hub/web` dans `app.py`). Toute route passe par l'authentification ; mots de passe et jetons comparés avec `hmac.compare_digest`. SQL toujours paramétré (jamais de f-string avec une donnée). `subprocess` avec une liste d'arguments, jamais `shell=True`. Les requêtes sortantes (TMDB, MusicBrainz) ont un délai d'attente et n'envoient aucun secret hors de leur service.
3. **Base de données.** Un changement de schéma doit migrer une base existante sans perdre la progression ni les playlists (`SCHEMA_VERSION` + migration, `CREATE ... IF NOT EXISTS`). Un scan ne doit jamais effacer les données d'enrichissement (clés `enrich:` dans `kv`).
4. **Flux ffmpeg.** Respecter `HUB_MAX_STREAMS` (503 au-delà), arrêter le processus quand le client part, ne rien supposer sur la présence de ffmpeg (`hub.cfg.ffmpeg` peut être vide : lecture directe seulement, avec un message clair). Le plan de lecture préfère *direct*, puis *remux*, puis *transcodage* : ne pas inverser cet ordre.
5. **Erreurs.** Renvoyer `HttpError` avec un message **en français** compréhensible par l'utilisateur ; un fichier illisible ne doit pas faire échouer tout le scan (le consigner et passer au suivant).
6. **Un seul profil** (`default`) : ne pas introduire de multi-profil sans demande.
7. Documenter dans le README toute nouvelle route (tableau « Interface HTTP ») ou nouveau réglage.
8. Si le changement demande une modification de `hub/web`, décris-la précisément plutôt que de la faire : c'est le domaine de l'agent `hub-ui`.

## Vérifier

```bash
python -m unittest discover -s tests
```

`tests/test_server.py` démarre le vrai serveur sur une médiathèque factice : ajoute un test pour tout comportement nouveau ou corrigé (ou délègue à `hub-tester`). Rappelle que `media_library` doit se trouver à côté de `media_hub` (ou `HUB_REPO`) pour que les tests s'exécutent. Un test sauté n'est pas un test réussi.

## Compte rendu

En français, court : ce qui a changé (fichiers, routes, schéma), ce qui a été vérifié et comment, ce qui reste à essayer sur le NAS (codecs réels, gros volumes).
