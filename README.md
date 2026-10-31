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
| Musique | `.flac`, `.mp3`, … regroupés en albums, pochette = `cover.{ext}` du dossier ; une image `1997 - OK Computer.jpg` dans le dossier de l'artiste déclare un album manquant (grisé, sur sa fiche seulement) | barre audio qui continue pendant la navigation |
| Livres | `.cbz` (pages), `.epub` (chapitres) ; `.cbr`, `.mobi`, `.azw3` en téléchargement | liseuse, sens manga, thèmes |

- **Accueil** : « Reprendre » (films, épisodes, livres, albums en cours) et les ajouts récents de chaque bibliothèque.
- **Bibliothèque** : recherche, index par lettre, genres, tri (titre, ajout, année), filtre « Non vus ».
- **Lecture adaptée au lecteur** : le navigateur annonce ce qu'il sait lire, le serveur choisit.
  1. *direct* : le fichier tel quel (requêtes Range), aucun calcul sur le NAS ;
  2. *remux* : l'image est recopiée, seul le son (E-AC-3, DTS…) passe en AAC : presque gratuit ;
  3. *transcodage* : l'image est ré-encodée en H.264 (1080p max par défaut) quand le codec n'est pas lisible.
  Si une lecture échoue côté navigateur, le lecteur retombe tout seul sur le mode suivant.
- **Sous-titres** : fichiers `.srt` / `.vtt` / `.ass` à côté de la vidéo et pistes texte du `.mkv`, affichés par le lecteur (PGS / VobSub : listés, grisés).
- **Progression** propre à chaque compte : vu / pas vu, reprise au point exact, livre rouvert à la bonne page.
- **Télécommande** : toute l'interface se pilote aux flèches (navigation spatiale), OK / Entrée, Retour. Dans le lecteur : ← → pour avancer ou reculer, OK pour pause, lecture / pause du clavier média. Souris et tactile marchent aussi (téléphone compris).

## Lancer en local (essai)

```bash
HUB_SRV=/chemin/vers/medias HUB_DATA=./demo-data python -m hub # http://127.0.0.1:8080
```

`media_library` doit se trouver à côté de `media_hub` (ou être désigné par `HUB_REPO`) : le serveur réutilise ses lecteurs de MKV, FLAC, MP3, CBZ, EPUB.

## Sur le NAS

Voir [deploy/README.md](deploy/README.md) : compose TrueNAS (Tailscale + media_hub), ffmpeg, mots de passe, télé.

## Réglages (variables d'environnement)

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `HUB_SRV` | racine des bibliothèques (`Films`, `Series`, `Animations`, `Musique`, `Livres`) | `/srv` |
| `HUB_LIBRARIES` | fichier JSON `[{"id","label","kind","path"}]` pour d'autres dossiers ; `kind` : `movies`, `shows`, `music`, `books` | dossiers connus sous `HUB_SRV` |
| `HUB_DATA` | base SQLite, cache d'images et `sagas.json` (les sagas que l'on compose soi-même, voir ci-dessous) | `./data` |
| `HUB_REPO` | dossier `media_library` | `../media_library`, `/app/media_library` |
| `HUB_BIND` / `HUB_PORT` | adresse et port d'écoute | `127.0.0.1` / `8080` |
| `HUB_PASSWORD` | amorçage : tant qu'aucun compte n'existe, crée le compte administrateur avec ce mot de passe (ensuite il est ignoré : les comptes se gèrent par `python -m hub user …` ou dans l'interface) ; **à mettre dès que `HUB_BIND` n'est pas local** | aucun (serveur ouvert, sans compte) |
| `HUB_ADMIN` | nom du compte créé par `HUB_PASSWORD` | `admin` |
| `HUB_FFMPEG` | dossier (ou fichier) de `ffmpeg` / `ffprobe` | `PATH`, puis `HUB_DATA/bin` |
| `HUB_MAX_STREAMS` | flux simultanés ; au-delà : HTTP 503 | `2` |
| `HUB_AUTOSCAN_HOURS` | rescan périodique (0 = jamais) ; un volume démonté (donc vide) ne fait perdre aucune progression de lecture, et un `.mkv` illisible n'arrête pas le scan | `0` |
| `TMDB_KEY` | enrichit les séries (affiche, synopsis, casting) et décrit les films qui manquent aux sagas ; lue aussi dans le `.env` de `media_library` | aucune |

## Sagas

Un film porte la saga que dit son étiquette (la collection TMDB posée par `Metadata.py`). On peut en composer d'autres (une trilogie que TMDB ne connaît pas, l'ordre du MCU) dans `sagas.json`, sous `HUB_DATA` : des listes ordonnées de films, désignés par leur identifiant TMDB (celui de l'étiquette `TMDB` du fichier, qui survit aux renommages et à une base refaite).

```json
{"sagas": [{"name": "Trilogie Cornetto", "films": [747, 4638, 49051]}]}
```

Les films qui **manquent** à une saga (le 2 d'une trilogie dont on n'a que le 1 et le 3) et qui sont **déjà sortis** apparaissent grisés à leur place, avec leur affiche, dans la page de la saga et sur la fiche d'un film ; le film 3 garde son numéro. Ils viennent de TMDB : *Gestion* > bibliothèque *Films* > *Sagas (TMDB)* (avec `TMDB_KEY`) interroge la collection de chaque saga de l'étiquette et décrit les films que ta saga du fichier réclame sans que tu les aies. Ce ne sont pas des éléments : aucun compteur ne les compte. Un film annoncé, ou sans date de sortie, n'est pas montré : il apparaît le jour de sa sortie, sans rien relancer (les films qu'une saga de `sagas.json` liste elle-même s'affichent toujours). Une collection TMDB trop large (un spin-off, un court-métrage) se règle en cachant le film : *Ne pas afficher* sous sa case grisée dans la page de la saga ; *Gestion* > *Sagas* > *Films cachés* le rétablit (`"ignore": [identifiant TMDB]` dans `sagas.json`). Si `sagas.json` devient illisible (une virgule en trop après une édition à la main), le hub n'y écrit plus rien et le dit : corrige-le ou supprime-le.

L'ordre de la liste est l'ordre de la saga ; la place d'un film dans la liste est son numéro, même quand on n'a pas ceux qui le précèdent. Un film peut être dans plusieurs sagas : la grille ne lui donne qu'une vignette (celle de la première saga du fichier, avant celle de l'étiquette), et sa fiche les liste toutes. Le fichier est relu dès qu'il change ; absent ou illisible, il ne donne aucune saga. Pas besoin de l'écrire à la main : l'administrateur compose ses sagas dans l'interface, *Gestion* > *Sagas* (créer, renommer, monter ou descendre une saga ou un film, ajouter des films en les cherchant ou toute une saga d'un coup, trier par date de sortie), ou depuis la fiche d'un film avec « Ajouter à une saga ». Le serveur écrit le fichier d'un bloc et refuse ce qu'il ne comprend pas (`/api/sagas`, voir `docs/api.md`). Seuls les films étiquetés avec un identifiant TMDB peuvent y entrer.

## Comptes

Chaque compte a sa progression, ses playlists et son « Reprendre » ; les bibliothèques sont communes. Au premier démarrage, `HUB_PASSWORD` crée l'administrateur (`HUB_ADMIN`, « admin » par défaut) et reprend la progression d'avant les comptes ; ensuite on gère les comptes depuis l'interface (administrateur) ou en ligne de commande, serveur arrêté ou non : Les essais de connexion ratés d'une même adresse sont ralentis (jusqu'à 5 s), puis plus rien n'est vérifié pendant 5 minutes après 10 échecs (réponse 429) ; un essai réussi remet le compteur à zéro.

```bash
python -m hub user list
python -m hub user add <nom> [--admin]     # mot de passe demandé (8 caractères au moins) ; le premier compte est toujours administrateur
python -m hub user passwd <nom>
python -m hub user rm <nom>                # supprime aussi sa progression et ses playlists ; jamais le dernier administrateur
```

Seul l'administrateur lance les mises à jour (scan, enrichissement, nettoyage), change les photos d'artistes et gère les comptes. Sans compte ni `HUB_PASSWORD`, le serveur reste ouvert à tous. Pas d'inscription publique.

## Interface HTTP

Le contrat complet pour un client tiers (champs, codes d'erreur, authentification, plan de lecture) est dans [docs/api.md](docs/api.md). Résumé :

| Route | Rôle |
| --- | --- |
| `GET /api/home`, `/api/state`, `/api/search?q=` | accueil, bibliothèques, recherche |
| `GET /api/library/<id>?q&letter&genre&sort&filter&offset&limit` | liste d'une bibliothèque, avec lettres et genres ; `sagas=1` (films) regroupe les sagas, `group=` en liste les films (`sort=part`), `section=` (livres) filtre par section et renvoie les `sections` avec leur compte |
| `GET/POST /api/playlists`, `GET /api/playlist/<id>`, `POST /api/playlist/<id>/add\|remove\|move\|rename\|delete` | playlists musicales (stockées dans la base ; une piste déplacée disparaît sans casser la liste) |
| `GET /api/item/<id>` | fiche : épisodes, pistes, tomes, sagas, casting, progression |
| `POST /api/play/<entry>` | plan de lecture (url, pistes audio, sous-titres, reprise, suivant) |
| `GET /media/<entry>/file`, `/media/<entry>/stream` | fichier (Range, `?download=1`) ; flux MP4 fragmenté (remux / transcodage, `?start=`) |
| `GET /api/subs/<entry>/<e\|s><n>.vtt` | sous-titres en WebVTT |
| `POST /api/progress`, `/api/item/<id>/seen`, `/api/entry/<id>/seen` | progression, vu / pas vu |
| `GET /api/book/<id>`, `/book/<id>/page/<n>`, `/book/<id>/epub/<chemin>` | pages d'un `.cbz`, fichiers d'un `.epub` (CSP sans script) |
| `GET /img/item/<id>?w=`, `/img/entry/<id>?w=` | affiches, pochettes, couvertures (réduites et gardées en cache) |
| `POST /api/token` | pour un client natif (Android TV, iOS, Chromecast) : corps `{"user": "…", "password": "…"}` (`user` facultatif s'il n'y a qu'un compte), réponse `{"token": "…", "media_token": "…", "user": {…}}` (401 `{"error": "mot de passe incorrect"}` sinon ; jetons vides si le serveur est ouvert, sans compte). Le jeton est celui du compte ; le jeton de média est commun à tout le serveur |
| `GET /api/sagas`, `POST /api/sagas`, `POST /api/sagas/add`, `GET /api/sagas/films` | sagas composées à la main (`sagas.json`, administrateur seulement) : lire, tout remplacer (avec la `version` lue, pour ne pas écraser un autre), ajouter des films, cacher un film des manquants (`POST /api/sagas/ignore`), chercher des films à y mettre ; `GET /img/film/<tmdb>` : l'affiche d'un film manquant |
| `GET /api/users`, `POST /api/users`, `POST /api/user/<id>/password\|delete` | comptes (administrateur seulement) : lister, créer, donner un nouveau mot de passe (à un autre compte ; pour le sien, l'administrateur redonne l'actuel, `current`), supprimer (avec sa progression et ses playlists ; jamais le dernier administrateur) |
| `POST /api/me/password` | chacun change son propre mot de passe (`{"current": "…", "new": "…"}`) ; les autres sessions du compte sont fermées |
| Authentification (dès qu'un compte existe) | trois façons de se présenter : cookie `hub=<compte>.<mac>` (navigateur, posé par `/api/login`) ; en-tête `Authorization: Bearer <token>` (toutes les routes) ; `?token=<media_token>` dans l'URL, **uniquement** sur `/media/`, `/img/`, `/book/` et `/api/subs/` (balises `<img>` / `<video>` / `<track>`, Cast), nulle part ailleurs sur `/api/`. Le jeton de média est distinct du jeton de session : une URL de pochette qui fuit n'ouvre ni le scan ni le nettoyage. Le jeton est masqué dans le journal. Changer un mot de passe ferme les sessions de ce compte ; le jeton de média, lui, ne change pas |
| `POST /api/scan`, `/api/enrich`, `/api/cleanup`, `GET /api/jobs` | (les trois `POST`, comme les photos d'artistes et `GET /api/jobs?log=1` (le journal des tâches), sont réservés à l'administrateur : 403 sinon ; `GET /api/jobs` sans `log` reste ouvert à tout compte) mise à jour de l'index, enrichissement TMDB, nettoyage des fichiers parasites (`Thumbs.db`, `AlbumArt_{…}_Large.jpg` / `_Small.jpg`…, et en musique toute image autre que `cover.{ext}` et que les pochettes d'albums manquants) |

## Limites connues

- **Pas d'inscription publique** : seul un administrateur crée les comptes. Chaque compte a sa progression, ses playlists et son « Reprendre » ; les bibliothèques, les fiches enrichies et les albums manquants sont communs. Sans compte ni `HUB_PASSWORD`, le serveur reste ouvert (profil unique `default`).
- Les films ne sont indexés qu'en **`.mkv`** (c'est ce que `media_library` produit : `Mp4_To_Mkv.py`, `Avi_To_Mkv.py`).
- **Pas de carte vidéo capable d'encoder** (GT 730) : le transcodage est logiciel. Un 1080p H.264 passe sans peine sur un i3-13100F ; un 4K HEVC en transcodage est à éviter (le plan de lecture le signale). Le mieux reste la lecture directe : un Chromecast / Fire TV 4K lit H.264, HEVC et AV1 selon le modèle.
- Google TV n'a pas Chrome : installer un navigateur compatible télécommande (TV Bro, Puffin TV, Silk sur Fire TV). Une appli Android TV native (WebView plein écran) est la suite logique si la télécommande dans un navigateur ne convient pas.
- Les formats d'image des sous-titres (PGS, VobSub) ne sont pas affichés.

## Développer avec Claude Code

Six agents spécialisés (architecture, serveur, interface, tests, relecture, déploiement) sont dans `.claude/agents/` : voir [docs/agents.md](docs/agents.md). Ce qu'il reste à faire ou à décider : [docs/TODO.md](docs/TODO.md).

## Tests

```bash
python -m unittest discover -s tests          # 263 tests : 226 serveur + 37 interface
```

`tests/test_ui.py` pilote l'interface dans Chromium (Playwright, 37 tests) ; il est ignoré si Playwright ou ffmpeg manquent.
