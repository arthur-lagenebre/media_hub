# media_hub

Le serveur de la médiathèque : **films, séries, animations, musique, livres**, depuis le NAS, dans un navigateur ou sur la télé (Android TV, Google TV, Fire TV). Même idée que Plex ou Jellyfin, mais sur mesure : rien à installer côté client, aucune dépendance pip (Pillow est facultatif), un seul processus Python.

```text
media_library   l'atelier : nettoie, renomme, convertit, étiquette (écrit dans les fichiers, ne produit aucune page)
media_hub       le lecteur : lit ces étiquettes et sert la médiathèque (n'écrit jamais dans les fichiers)
```

## Ce qu'il fait

| Bibliothèque | Contenu lu | Lecture |
| --- | --- | --- |
| Films | `.mkv` (titre, synopsis, casting, jaquette embarqués par `Metadata.py`), sagas regroupées en une vignette (bouton « Sagas ») | vidéo, sous-titres, reprise |
| Séries, Animations | dossiers série / saison / épisode, affiche TMDB (avec `TMDB_KEY`) | vidéo, épisode suivant |
| Musique | `.flac`, `.mp3`, … regroupés en albums, pochette = `cover.{ext}` du dossier | barre audio qui continue pendant la navigation |
| Livres | `.cbz` (pages), `.epub` (chapitres) ; `.cbr`, `.mobi`, `.azw3` en téléchargement | liseuse, sens manga, thèmes |

- **Accueil** : « Reprendre » (films, épisodes, livres, albums en cours) et les ajouts récents de chaque bibliothèque.
- **Bibliothèque** : recherche, index par lettre, genres, tri (titre, ajout, année), filtre « Non vus ».
- **Lecture adaptée au lecteur** : le navigateur annonce ce qu'il sait lire, le serveur choisit.
  1. *direct* : le fichier tel quel (requêtes Range), aucun calcul sur le NAS ;
  2. *remux* : l'image est recopiée, seul le son (E-AC-3, DTS…) passe en AAC : presque gratuit ;
  3. *transcodage* : l'image est ré-encodée en H.264 (1080p max par défaut) quand le codec n'est pas lisible.
  Si une lecture échoue côté navigateur, le lecteur retombe tout seul sur le mode suivant.
- **Sous-titres** : fichiers `.srt` / `.vtt` / `.ass` à côté de la vidéo et pistes texte du `.mkv`, affichés par le lecteur (PGS / VobSub : listés, grisés).
- **Progression** partagée (un seul profil) : vu / pas vu, reprise au point exact, livre rouvert à la bonne page.
- **Télécommande** : toute l'interface se pilote aux flèches (navigation spatiale), OK / Entrée, Retour. Dans le lecteur : ← → pour avancer ou reculer, OK pour pause, lecture / pause du clavier média. Souris et tactile marchent aussi (téléphone compris).

## Lancer en local (essai)

```bash
python tools/make_demo.py ./demo                  # une petite médiathèque factice (ffmpeg requis)
HUB_SRV=./demo HUB_DATA=./demo-data python -m hub # http://127.0.0.1:8080
```

`media_library` doit se trouver à côté de `media_hub` (ou être désigné par `HUB_REPO`) : le serveur réutilise ses lecteurs de MKV, FLAC, MP3, CBZ, EPUB.

## Sur le NAS

Voir [deploy/README.md](deploy/README.md) : compose TrueNAS (Tailscale + media_hub), ffmpeg, mots de passe, télé.

## Réglages (variables d'environnement)

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `HUB_SRV` | racine des bibliothèques (`Films`, `Series`, `Animations`, `Musique`, `Livres`) | `/srv` |
| `HUB_LIBRARIES` | fichier JSON `[{"id","label","kind","path"}]` pour d'autres dossiers ; `kind` : `movies`, `shows`, `music`, `books` | dossiers connus sous `HUB_SRV` |
| `HUB_DATA` | base SQLite et cache d'images | `./data` |
| `HUB_REPO` | dossier `media_library` | `../media_library`, `/app/media_library` |
| `HUB_BIND` / `HUB_PORT` | adresse et port d'écoute | `127.0.0.1` / `8080` |
| `HUB_PASSWORD` | mot de passe (cookie signé) ; **à mettre dès que `HUB_BIND` n'est pas local** | aucun |
| `HUB_FFMPEG` | dossier (ou fichier) de `ffmpeg` / `ffprobe` | `PATH`, puis `HUB_DATA/bin` |
| `HUB_MAX_STREAMS` | flux simultanés ; au-delà : HTTP 503 | `2` |
| `HUB_AUTOSCAN_HOURS` | rescan périodique (0 = jamais) | `0` |
| `TMDB_KEY` | enrichit les séries (affiche, synopsis, casting) ; lue aussi dans le `.env` de `media_library` | aucune |

## Interface HTTP

| Route | Rôle |
| --- | --- |
| `GET /api/home`, `/api/state`, `/api/search?q=` | accueil, bibliothèques, recherche |
| `GET /api/library/<id>?q&letter&genre&sort&filter&offset&limit` | liste d'une bibliothèque, avec lettres et genres ; `sagas=1` (films) regroupe les sagas, `group=` en liste les films (`sort=part`), `section=` (livres) filtre par section et renvoie les `sections` avec leur compte |
| `GET/POST /api/playlists`, `GET /api/playlist/<id>`, `POST /api/playlist/<id>/add\|remove\|move\|rename\|delete` | playlists musicales (stockées dans la base ; une piste déplacée disparaît sans casser la liste) |
| `GET /api/item/<id>` | fiche : épisodes, pistes, tomes, saga, casting, progression |
| `POST /api/play/<entry>` | plan de lecture (url, pistes audio, sous-titres, reprise, suivant) |
| `GET /media/<entry>/file`, `/media/<entry>/stream` | fichier (Range, `?download=1`) ; flux MP4 fragmenté (remux / transcodage, `?start=`) |
| `GET /api/subs/<entry>/<e\|s><n>.vtt` | sous-titres en WebVTT |
| `POST /api/progress`, `/api/item/<id>/seen`, `/api/entry/<id>/seen` | progression, vu / pas vu |
| `GET /api/book/<id>`, `/book/<id>/page/<n>`, `/book/<id>/epub/<chemin>` | pages d'un `.cbz`, fichiers d'un `.epub` (CSP sans script) |
| `GET /img/item/<id>?w=`, `/img/entry/<id>?w=` | affiches, pochettes, couvertures (réduites et gardées en cache) |
| `POST /api/scan`, `/api/enrich`, `/api/cleanup`, `GET /api/jobs` | mise à jour de l'index, enrichissement TMDB, nettoyage des fichiers parasites (`Thumbs.db`, `AlbumArt_{…}_Large.jpg` / `_Small.jpg`…, et en musique toute image autre que `cover.{ext}`) |

## Limites connues

- **Un seul profil** (« default ») : la progression est commune à tous les lecteurs.
- Les films ne sont indexés qu'en **`.mkv`** (c'est ce que `media_library` produit : `Mp4_To_Mkv.py`, `Avi_To_Mkv.py`).
- **Pas de carte vidéo capable d'encoder** (GT 730) : le transcodage est logiciel. Un 1080p H.264 passe sans peine sur un i3-13100F ; un 4K HEVC en transcodage est à éviter (le plan de lecture le signale). Le mieux reste la lecture directe : un Chromecast / Fire TV 4K lit H.264, HEVC et AV1 selon le modèle.
- Google TV n'a pas Chrome : installer un navigateur compatible télécommande (TV Bro, Puffin TV, Silk sur Fire TV). Une appli Android TV native (WebView plein écran) est la suite logique si la télécommande dans un navigateur ne convient pas.
- Les formats d'image des sous-titres (PGS, VobSub) ne sont pas affichés.

## Tests

```bash
python -m unittest discover -s tests          # 53 tests : 35 serveur + 18 interface
```

`tests/test_ui.py` pilote l'interface dans Chromium (Playwright, 18 tests) ; il est ignoré si Playwright ou ffmpeg manquent.
