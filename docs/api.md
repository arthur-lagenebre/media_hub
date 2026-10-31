# Interface HTTP de media_hub

Ce document décrit ce que le serveur (`hub/server/app.py`) fait aujourd'hui, pour un client que le dépôt ne contrôle pas : appli Android TV, iOS, récepteur Chromecast, script. Il est écrit d'après le code ; ce qui n'y est pas déterminé est signalé par **Ambigu**.

- Version du contrat : `GET /api/state` renvoie `"api": 1`. Ce nombre ne change qu'en cas de **rupture** (champ retiré, renommé, ou de sens différent). Ajouter un champ n'en est pas une : **un client ignore les champs qu'il ne connaît pas**.
- « optionnel » = la clé est **absente** de l'objet (jamais `null`), sauf mention contraire.
- Tout est en UTF-8. Les messages d'erreur et les libellés sont en français.

## Conventions

| Sujet | Règle |
| --- | --- |
| URLs | Les URLs renvoyées par l'API (`cover`, `image`, `file`, `url`, `page_url`…) sont **relatives à la racine du serveur** (`/img/item/ab12…`). Le client les accole à son adresse de base. Elles ne contiennent ni `?w=` ni jeton. |
| Images | Ajouter `?w=<largeur>` pour une version réduite (JPEG). Les largeurs sont arrondies à la valeur supérieure de **160, 320, 480, 720, 1280** (au-delà : 1280). Sans `w` (ou `w=0`) : l'image d'origine, avec son type MIME. |
| Jeton | `?token=<media_token>` s'ajoute à l'URL d'une image, d'un fichier, d'un flux ou d'une page de livre quand le client ne peut pas poser d'en-tête. C'est le **jeton de média** (`media_token` de `POST /api/token`), pas le jeton principal : voir « Authentification ». Si l'URL porte déjà une requête (`/media/<id>/stream?m=remux&…`), on ajoute `&token=…`. |
| Durées | En **secondes**, nombres (parfois décimaux). Une durée inconnue vaut `0`. |
| Dates | `added`, `updated`, `scanned`, `started`, `ended`, `queued` : horodatage Unix en secondes. `added` est calculé par le scan à partir des fichiers de l'élément (le plus récent d'entre eux, à la lecture du code). `year` est une **chaîne** (`""` si inconnue). |
| Identifiants | Opaques : chaînes alphanumériques (hexadécimales en pratique) fournies par le serveur. Ne pas les construire, ni les interpréter, ni les garder d'une base à l'autre (une base reconstruite peut en produire d'autres). Les identifiants de bibliothèque (`films`, `musique`…) sont ceux du serveur. |
| Corps des requêtes | `POST` en JSON (`Content-Type: application/json`), 1 Mo au plus (sinon 413). Une requête `POST` sans corps utile envoie `{}`. |
| Réponses | JSON compact avec `Cache-Control: no-store`, sauf images, fichiers, sous-titres. Pas d'en-têtes CORS : un client web d'une autre origine ne peut pas lire l'API ; les balises `<img>` / `<video>` fonctionnent. |
| Erreurs | `{"error": "message lisible"}` avec le code HTTP. Voir « Codes d'erreur ». |
| Profil | Un seul (« default ») : la progression est commune à tous les clients. |

### Codes d'erreur

| Code | Sens |
| --- | --- |
| 400 | requête invalide (paramètre, corps, fichier d'un autre genre que celui demandé) |
| 401 | `connexion requise` (routes `/api/*`) ; `mot de passe incorrect` pour `/api/token` |
| 302 | **hors `/api/`**, une requête non authentifiée n'a pas de 401 : elle est redirigée vers `/login`. Un client natif qui lit `/media/…`, `/img/…` ou `/book/…` sans jeton reçoit donc `302`, pas `401`. |
| 404 | identifiant inconnu, image absente, route inconnue (`introuvable`). Une route existante appelée avec la mauvaise méthode donne aussi 404. |
| 413 | corps trop gros |
| 415 | image refusée (photo d'artiste) |
| 416 | `Range` hors du fichier |
| 422 | livre illisible |
| 500 | erreur interne, ou ffprobe / ffmpeg a échoué (le message le dit) |
| 501 | ffmpeg est introuvable sur le serveur (flux converti impossible) |
| 502 | service extérieur injoignable (photo Wikipédia) |
| 503 | trop de conversions en cours (`HUB_MAX_STREAMS`) |

## Authentification

Si le serveur n'a pas de mot de passe (`"auth": false` dans `/api/state`), tout est ouvert. Sinon, le serveur connaît **deux jetons**, communs à tous les appareils et stables tant que la base du serveur n'est pas effacée :

- le **jeton** (`token`) ouvre **tout** : l'API, les fichiers, et aussi les tâches de gestion (scan, nettoyage avec suppression de fichiers) ;
- le **jeton de média** (`media_token`) ne sert que dans l'URL des fichiers (`/media/`, `/img/`, `/book/`) : si une URL de pochette fuit (journal d'un proxy, historique, récepteur Cast), son jeton n'ouvre ni l'API ni les tâches de gestion (scan, nettoyage), mais il donne la **lecture de tous les fichiers** de la médiathèque (films, livres, images, flux convertis dans la limite de `max_streams`). Il ne se déduit pas du jeton principal. Il n'y a pas de rotation : seul l'effacement du secret du serveur (clé `secret` de la base) révoque les jetons, tous à la fois.

Trois façons de se présenter :

1. **Cookie** `hub=<jeton>` : posé par le formulaire `POST /api/login` (réponse 303). C'est le mode du navigateur.
2. **`Authorization: Bearer <jeton>`** : valable sur toutes les routes. C'est le mode d'un client natif. Le jeton de média y est refusé (401).
3. **`?token=<media_token>`** dans l'URL : **seulement** pour les chemins `/media/`, `/img/`, `/book/` et `/api/subs/` (les sous-titres, lecture seule), nulle part ailleurs sur `/api/`. Pour les balises `<img>`, `<video>`, `<track>` et le Cast, qui n'envoient pas toujours d'en-tête. **Le jeton principal y est refusé** (302 sur `/media/`, `/img/` et `/book/` ; 401 sur `/api/subs/`). Le serveur masque le jeton dans son journal ; un proxy placé devant ne le fait pas forcément.

### `POST /api/token`

Échange le mot de passe contre les deux jetons. Aucune authentification requise.

- Corps : `{"password": "…"}`.
- 200 : `{"token": "…", "media_token": "…"}` (64 caractères hexadécimaux chacun). Sans mot de passe configuré : `{"token": "", "media_token": ""}`.
- 401 : `{"error": "mot de passe incorrect"}` (aussi pour un corps illisible). La réponse prend au moins 0,4 s : pas de force brute rapide.

`POST /api/login` (formulaire `password=…` ou JSON) pose le cookie mais répond par une **redirection 303** (`/` ou `/login?error=1`) : à éviter hors navigateur. `GET /api/logout` efface le cookie (303).

## Un client en quatre appels

1. `POST /api/token` → `token` et `media_token` ; ensuite `Authorization: Bearer <token>` pour l'API, et `?token=<media_token>` au bout des URLs de fichiers quand le lecteur ne sait pas poser d'en-tête.
2. `GET /api/state` → bibliothèques (`id`, `kind`), disponibilité de ffmpeg, version du contrat.
3. `GET /api/library/<id>` puis `GET /api/item/<id>` → de quoi afficher et choisir un fichier.
4. `POST /api/play/<entry>` (vidéo) ou directement `file` (audio, livre) → quoi lire.

## État, accueil, recherche

### `GET /api/state`

```json
{"version": "…", "api": 1, "libraries": [{"id": "films", "label": "Films", "kind": "movies", "count": 120, "scanned": 1730000000, "exists": true}],
 "ffmpeg": true, "ffprobe": true, "tmdb": false, "auth": false, "busy": false, "streams": 0, "max_streams": 2}
```

- `libraries[].kind` : `movies` | `shows` | `music` | `books`. `scanned` : horodatage du dernier scan, ou `null`. `exists` : le dossier est présent.
- `ffmpeg` / `ffprobe` : disponibles sur le serveur (sans eux : lecture directe seulement).
- `busy` : une tâche de fond tourne. `streams` / `max_streams` : conversions en cours / maximum.
- **Ambigu** : la route demande l'authentification ; `api` n'est donc lisible qu'après avoir obtenu le jeton.

### `GET /api/home`

```json
{"continue": [<carte>], "libraries": [{"id": "…", "label": "…", "kind": "…", "count": 12, "recent": [<élément>]}]}
```

- `recent` : au plus 24 éléments, du plus récemment ajouté au plus ancien.
- `continue` : au plus 24 cartes « à reprendre ». Une **carte** est un objet *fichier* (voir plus bas) avec en plus : `item` (l'objet *élément* auquel il appartient), `position` (secondes ou page), `total`, `resume` (`true` : reprendre ici ; `false` : le fichier **suivant** d'un épisode ou d'un tome fini, à lancer depuis le début), `updated`.

### `GET /api/search?q=<texte>`

`{"results": [{"id": "films", "label": "Films", "kind": "movies", "items": [<élément>]}]}` — au plus 40 éléments par bibliothèque, ceux dont le titre ou le groupe (artiste, saga…) contient le texte (sans accents ni casse). Moins de 2 caractères : `{"results": []}`.

## Objets communs

### Élément (`<élément>`)

Un film, une série, un album, une série de livres.

| Champ | Type | Sens |
| --- | --- | --- |
| `id` | chaîne | identifiant |
| `type` | chaîne | `movie` \| `show` \| `album` \| `series` (livres) ; `saga` (voir plus bas, seulement dans une liste de bibliothèque) |
| `title` | chaîne | |
| `year` | chaîne | `""` si inconnue |
| `grp` | chaîne | groupe : artiste (album), saga (film), `""` sinon |
| `n` | entier | nombre de fichiers (pistes, épisodes, tomes, parties) |
| `letter` | chaîne | lettre d'index (`#` pour les autres) |
| `added` | entier | date d'ajout |
| `genres` | liste de chaînes | |
| `cover` | chaîne | URL de l'affiche / pochette / couverture. **Peut répondre 404** (aucune image) : prévoir une case de remplacement. |
| `part` | nombre, optionnel | rang dans la saga (films) |
| `duration` | nombre, optionnel | durée de l'album (albums) |
| `artist_id` | chaîne, optionnel | identifiant d'artiste (albums), pour `/api/artist/<lib>/<id>` |
| `missing` | entier, optionnel | **séries de livres seulement** (`type: "series"`) : nombre de tomes manquants ; absent s'il n'en manque aucun. **Attention : dans la fiche (`GET /api/item/<id>`), le même nom porte une liste** (voir « Même nom, deux types »). Les albums n'ont pas de `missing` : leurs albums manquants sont dans `wanted` (`/api/artist`). |
| `seen`, `started` | entiers, optionnels | présents si au moins un fichier a une progression (hors musique) : fichiers **vus**, fichiers **entamés** non finis. Un film est vu quand `seen >= max(1, n)`. |

### Fichier (`<fichier>`)

Un film, un épisode, une piste, un tome.

| Champ | Type | Sens |
| --- | --- | --- |
| `id` | chaîne | identifiant du fichier (`entry`) |
| `kind` | chaîne | `video` \| `audio` \| `book` |
| `title`, `summary` | chaînes | `""` si vides |
| `season`, `disc` | entier ou `null` | |
| `number` | nombre ou `null` | numéro d'épisode, de piste, de tome (peut être décimal) |
| `duration` | nombre | secondes. **Ambigu** pour un livre : le code ne la définit pas pour ce genre. |
| `format`, `label`, `sub` | chaînes | `format` : `cbz` / `epub` pour un livre. **Ambigu** : le sens exact de `label` et `sub` dépend de la bibliothèque (ce sont des libellés d'affichage tirés de l'étiquetage) ; ne pas s'y fier pour décider. |
| `file` | chaîne | URL du fichier (`/media/<id>/file`) |
| `image` | chaîne, optionnel | URL de l'image propre au fichier (vignette d'épisode, couverture de tome) ; absent pour une piste audio. **Peut répondre 404** : retomber sur `cover` de l'élément. |
| `date` | chaîne, optionnel | date de sortie (vidéo, quand l'étiquette la donne) |
| `artist` | chaîne, optionnel | artiste de la piste (audio ; peut être `""`) |
| `pages` | entier, optionnel | nombre de pages (livre, quand l'étiquette la donne) |
| `progress` | objet, optionnel | `{"position", "duration", "done", "updated"}` ; présent dans les `entries` d'un élément quand il y a une progression. `position` : secondes, ou page / chapitre pour un livre. |

### Piste (artiste, playlist)

Objet plus léger : `id`, `title`, `artist`, `album` (titre), `item` (id de l'album), `duration`, **`cover`** (URL de la pochette de l'album), **`file`**. Les pistes d'une playlist ont en plus `number`. Une piste n'a pas d'image propre : sa pochette est `cover`.

## Une bibliothèque

### `GET /api/library/<id>`

Paramètres (tous optionnels) :

| Paramètre | Effet |
| --- | --- |
| `q` | filtre sur le titre / le groupe |
| `letter` | une lettre d'index |
| `genre` | un genre |
| `sort` | `title` (défaut ; par artiste en musique, par collection en livres) \| `added` \| `year` \| `year-asc` \| `part` |
| `filter=unseen` | seulement ce qui n'est pas entièrement vu |
| `offset`, `limit` | pagination (`limit` : 240 par défaut, 600 au plus) |
| `sagas=1` | films : regroupe les sagas (au moins deux films) en une vignette `type: "saga"` |
| `group` | films : les films d'une saga, par son nom (`grp`) ; désactive `sagas` |
| `section` | livres : une section (Manga, BD…) |

Réponse :

```json
{"library": {"id": "…", "label": "…", "kind": "…"}, "total": 120, "offset": 0, "items": [<élément>],
 "letters": [["A", 12], ["#", 3]], "genres": [["Action", 30]]}
```

- `total` : éléments après filtres (avant pagination). `letters` et `genres` sont comptés sur **toute** la bibliothèque (hors filtres de lettre / genre / texte) ; `genres` : 60 au plus, du plus fréquent au moins fréquent.
- Livres : `sections` : `[["Manga", 40], …]`. Avec `sagas=1` : `sagas` (nombre de vignettes de saga) et `films` (nombre de films en tout).
- Une vignette de **saga** : `id`, `lib`, `type: "saga"`, `title`, `year`, `years` (`[première, dernière]` ou `[]`), `grp: ""`, `n`, `letter`, `added`, `genres`, `covers` (ids des trois premiers films), **`cover_urls`** (leurs URLs), `seen` / `started` optionnels. **Elle n'a pas de `cover`** : l'identifiant d'une saga n'est pas celui d'un élément, `/img/item/<saga>` répondrait 404. Pour ses films : `GET /api/library/<id>?group=<title>&sort=part`. **Ambigu** : `lib` n'est présent que sur les sagas.
- 404 : bibliothèque inconnue.

### `GET /api/item/<id>`

Un `<élément>` (champs ci-dessus) plus :

| Champ | Type | Sens |
| --- | --- | --- |
| `library` | objet | `{id, label, kind}` |
| `summary` | chaîne | synopsis / description |
| `tagged` | booléen | au moins un fichier de l'élément porte les étiquettes de `media_library` (le serveur n'en expose pas le nombre) |
| `entries` | liste de `<fichier>` | triés (saison, disque, numéro, nom) ; avec `progress` quand il y en a une |
| `resume` | chaîne ou `null` | `id` du fichier à proposer : l'entamé le plus récent, sinon le premier non vu, sinon le premier |
| `links` | liste, optionnel | `[{"label": "TMDB", "url": "https://…"}]` (films et séries) |
| `seasons` | liste, optionnel | séries ; **Ambigu** : forme tirée de l'étiquetage, non figée par le serveur |
| `saga` | liste d'`<élément>`, optionnel | films d'une saga : **tous** les films de la saga, **y compris le film consulté** (le client le reconnaît à son `id`), triés par `part` (les films sans rang à la fin) puis par titre |
| `missing` | liste, optionnel | séries de livres : `[{"label": "Naruto", "numbers": [3], "total": 3}]` : par série (ou arc), les numéros de tomes manquants et le total attendu. Absent s'il ne manque rien. **Remplace le compte** que portent les listes. |
| `tracks_total` | entier, optionnel | albums : nombre de pistes |
| autres | | selon ce que l'étiquetage ou l'enrichissement a fourni, quand ils sont non vides : `directors`, `writers`, `actors`, `artist`, `artist_id`, `label`, `catalog`, `formats`, `authors`, `publisher`, `section`, `collection`, `status`, `episodes_total`, `seasons_total`, `tmdb`, `unit`, `units`, `years`, `discs`, `season_summaries`. **Ambigu** : leurs types ne sont pas contrôlés par le serveur (listes de chaînes pour les personnes, structures libres pour `discs`, `season_summaries`). |

404 : élément inconnu.

#### Même nom, deux types : `missing`

| Où | Type | Sens |
| --- | --- | --- |
| `<élément>` dans une liste (`/api/library`, `/api/home`, `/api/search`) | entier | nombre de tomes manquants (séries de livres) |
| `GET /api/item/<id>` (série de livres) | liste de `{"label", "numbers", "total"}` | le détail des manques |
| `GET /api/playlist/<id>` | entier | pistes de la playlist dont le fichier a disparu |

Un client qui lit `missing` doit donc tester son type. C'est une particularité connue, conservée parce que l'interface web s'en sert telle quelle ; un renommage serait une rupture (`api` passerait à 2).

### `GET /api/artist/<bibliothèque>/<artist_id>`

Musique seulement (404 sinon, ou si l'artiste n'a pas d'album).

```json
{"library": {…}, "id": "…", "name": "Daft Punk", "albums": [<élément>], "tracks": [<piste>], "genres": ["Electro"], "duration": 3600,
 "wanted": [{"year": "1997", "title": "Homework", "cover": "/img/wanted/…" }], "photo": false}
```

- `albums` : du plus ancien au plus récent. `tracks` : toutes les pistes de tous les albums, dans l'ordre. `genres` : 5 au plus.
- `photo` : booléen (inchangé). **`image`** (optionnel) : URL de la photo, **présente seulement si `photo` est vrai** ; sinon utiliser la pochette du premier album.
- `info` (optionnel, après « Fiches artistes » côté serveur) : `{"facts": "Groupe · France · 1993 – 2021", "disambiguation": "", "wiki": {"lang", "title", "url", "extract"} ou null, "members": [{"name", "roles", "period", "former"}], "links": [{"label", "url"}]}`.
- `wanted` : albums de l'artiste qui manquent dans la médiathèque ; `cover` est une URL relative, ou `""` s'il n'y a pas d'image. Ce ne sont ni des éléments ni des fichiers : rien à lire.
- Administration (usage interne de l'interface) : `POST /api/artist/<lib>/<id>/photo` (corps = image JPEG / PNG / WebP, 10 Mo au plus → `{"ok": true}`, 415 sinon) et `POST …/photo/wikipedia` (404 sans article connu, 502 si Wikipédia ne répond pas).
- `GET /img/artist/<lib>/<id>?w=` : la photo (404 s'il n'y en a pas). `GET /img/wanted/<lib>/<artist_id>/<clé>?w=` : pochette d'un album manquant.

## Playlists (musique)

Une playlist ne retient que des identifiants de pistes ; une piste dont le fichier a disparu n'apparaît plus (et revient avec le fichier).

| Route | Corps | Réponse |
| --- | --- | --- |
| `GET /api/playlists` | | `{"playlists": [<résumé>]}` (par nom) |
| `POST /api/playlists` | `{"name": "…", "entries": ["<id>", …]}` | **201**, `<détail>`. Un nom déjà pris (sans tenir compte de la casse) reprend la playlist existante. |
| `GET /api/playlist/<id>` | | `<détail>` |
| `POST /api/playlist/<id>/add` | `{"entries": [...]}` | `<détail>` ; un titre peut figurer deux fois |
| `POST /api/playlist/<id>/remove` | `{"index": 0}` | `<détail>` ; `index` compte dans la liste **affichée** (sans les pistes disparues) |
| `POST /api/playlist/<id>/move` | `{"from": 0, "to": 3}` | `<détail>` ; mêmes index |
| `POST /api/playlist/<id>/rename` | `{"name": "…"}` | `<détail>` |
| `POST /api/playlist/<id>/delete` | `{}` | `{"ok": true}` |

- `<résumé>` : `{"id", "name", "count", "duration", "covers": [id d'albums, 4 au plus], "cover_urls": [leurs URLs], "updated"}`.
- `<détail>` : le résumé plus `tracks` (`<piste>`) et `missing` (pistes disparues, non listées).
- Erreurs : 400 (`donne un nom à la playlist`, `aucune piste valide à ajouter`, `position invalide`, 5000 pistes au plus par playlist), 404 `playlist inconnue`.

## Progression

| Route | Corps | Réponse |
| --- | --- | --- |
| `POST /api/progress` | `{"entry": "<id>", "position": 123.4, "duration": 5400, "done": false}` | `{"ok": true, "done": bool}` |
| `POST /api/entry/<id>/seen` | `{"done": true}` (défaut `true`) | `{"ok": true}` |
| `POST /api/item/<id>/seen` | `{"done": true, "season": 2}` (`season` optionnel : une saison) | `{"ok": true, "count": 12}` (fichiers touchés) |
| `POST /api/item/<id>/dismiss` | `{}` | `{"ok": true}` : retire l'élément de `continue` sans toucher à la progression ; il y revient dès qu'on le relance |

- `done` est décidé par le serveur : vrai si `done` est envoyé, ou — vidéo — si 92 % est atteint ou s'il reste moins de `min(90 s, 8 % de la durée)` ; livre : `position >= duration - 1` ; musique : au-delà de 80 % (la position est alors remise à 0, la musique n'a pas de reprise). `duration` absente ou nulle : celle du fichier.
- Un client vidéo envoie sa position toutes les ~10 s en lecture, à la pause et à la sortie (l'interface web n'envoie rien en dessous de 20 s). `continue` ignore une position inférieure à 20 s (vidéo) ou à 1 (livre).
- 400 `position invalide` ; 404 `fichier inconnu` / `élément inconnu`. `/api/item/<id>/seen` avec `done: false` efface la progression.

## Lecture

### `POST /api/play/<entry>` — le plan de lecture d'une vidéo

À appeler **à l'ouverture**, puis à chaque changement de piste audio ou de qualité. Le client annonce ce qu'il sait lire ; le serveur choisit. 400 si le fichier n'est pas une vidéo, 404 s'il est inconnu, 500 si ffprobe est absent ou échoue (le message l'explique).

Corps :

```json
{"caps": {"video": ["h264", "hevc"], "audio": ["aac", "mp3", "flac"], "containers": ["mp4"], "channels": 2, "hdr": false},
 "audio": null, "max_kbps": 0, "max_height": 0}
```

| Champ | Sens |
| --- | --- |
| `caps.video` | codecs vidéo lisibles (voir ci-dessous) |
| `caps.audio` | codecs audio lisibles |
| `caps.containers` | conteneurs lus tels quels. Liste absente ou vide : `["mp4", "webm"]`. L'interface web annonce `["mp4"]` et ajoute `webm`, `mkv` quand le navigateur dit les lire. |
| `caps.channels` | nombre de voies audio acceptées (2 ou 6 dans l'interface web). Défaut : 2. |
| `caps.hdr` | le HDR est affichable (sinon un fichier HDR est converti) |
| `caps.height` | optionnel : hauteur maximale lisible. **L'interface web ne l'envoie pas** ; le serveur la lit s'il est là. |
| `audio` | indice de piste audio voulu (`audio[].index` d'un plan précédent), ou `null` : la piste par défaut qui n'est pas un commentaire, sinon la première |
| `max_kbps` | débit maximal voulu (0 : aucun). Un fichier plus gros de plus de 5 % est converti. |
| `max_height` | hauteur maximale voulue (0 : aucune). Prime sur `caps.height`. |

**Noms de codecs** (ce que le serveur compare ; ce sont des noms ffprobe normalisés, pas des chaînes MIME) :

- vidéo : `h264`, `hevc`, `vp9`, `av1`, `vp8`, `mpeg4` ; pour le **10 bits**, ajouter le nom suffixé de `10` (`hevc10`, `vp910`). Un fichier 10 bits n'est lu tel quel que si ce nom est dans `caps.video` (l'interface web n'annonce que `hevc10` et `vp910`, donc un H.264 10 bits est toujours converti).
- audio : `aac`, `mp3`, `opus`, `vorbis`, `flac`, `ac3`, `eac3`, `dts`, `truehd`, `alac` (le serveur laisse inchangé un nom ffprobe qu'il ne connaît pas).
- Comment l'interface web les construit (`hub/web/js/player.js`, `capabilities`) : elle interroge `canPlayType` du navigateur pour une valeur de test par codec (`avc1.640029` → `h264`, `hvc1.1.6.L120.90` → `hevc`, `hvc1.2.4.L120.90` → `hevc10`, `vp9` → `vp9`, `vp09.02.10.10` → `vp910`, `av01.0.08M.08` → `av1` ; `mp4a.40.2` → `aac`, `audio/mpeg` → `mp3`, `opus`, `vorbis`, `audio/flac` → `flac`, `ac-3` → `ac3` et `ec-3` → `eac3` seulement si l'utilisateur a activé le son surround). Un client natif répond avec ce que son décodeur matériel accepte.
- **Repli conseillé** (c'est ce que fait l'interface web) : si la lecture échoue malgré le plan, redemander le plan avec moins de capacités — d'abord `containers: ["mp4"]` seul, puis sans aucun codec vidéo (`video: []`), ce qui force la conversion complète.

Réponse :

| Champ | Type | Sens |
| --- | --- | --- |
| `entry`, `item` | chaînes | fichier et élément |
| `title` | chaîne | titre du fichier |
| `show` | chaîne | titre de la série (`""` pour un film) |
| `season`, `number` | entier / nombre ou `null` | |
| `mode` | chaîne | `direct` \| `remux` \| `transcode` (voir plus bas) |
| `why` | liste de chaînes | raisons de ne pas lire en direct (vide en `direct`) ; texte en français, à titre d'information |
| `url` | chaîne | **l'URL à lire** (relative ; le client y ajoute `&token=<media_token>` s'il ne pose pas d'en-tête) |
| `seekable` | booléen | `true` seulement en `direct` (voir « Avancer ») |
| `duration` | nombre | secondes (celle de ffprobe, sinon celle de l'index) |
| `resume` | nombre | position de reprise en secondes ; `0` s'il n'y en a pas, si le fichier est fini ou en deçà de 20 s |
| `warning` | chaîne | `""`, ou un avertissement à afficher (4K à convertir sans carte vidéo) |
| `audio` | liste | `{"index", "label", "lang", "selected", "commentary"}` ; `selected` : la piste que ce plan utilise |
| `subs` | liste | `{"id", "label", "lang", "forced", "default", "text", "where", "url"}` ; `where` : `mkv` (dans le fichier) ou `fichier` (fichier voisin) ; `url` : WebVTT, **`""` si `text` est faux** (sous-titres image PGS / VobSub : non pris en charge) |
| `video` | objet ou `null` | `{"codec", "width", "height", "hdr"}` |
| `next` | objet ou `null` | `{"id", "title", "season", "number"}` : le fichier vidéo suivant du même élément (épisode, partie) |

#### Les trois modes

L'ordre de préférence est toujours **direct, puis remux, puis transcode**.

- **`direct`** — `url` = `/media/<id>/file` : le fichier tel quel, avec `Range`. Aucun calcul côté serveur. Choisi si l'image est lisible (codec dans `caps.video`, 8 bits ou 10 bits annoncé, pas de HDR non géré, pas plus haut que la limite, pas plus lourd que `max_kbps`), si le conteneur est dans `caps.containers`, si le son est lisible (codec dans `caps.audio` et pas plus de voies que `caps.channels`) **et** si la piste est celle par défaut.
- **`remux`** — `url` = `/media/<id>/stream?m=remux&…` : l'image est recopiée sans calcul ; le son est recopié s'il est lisible (`ca=1`), sinon converti en AAC (`ca=0`). Choisi quand seul le conteneur, le son ou le choix d'une autre piste audio empêche le mode direct.
- **`transcode`** — `url` = `/media/<id>/stream?m=transcode&…` : l'image est ré-encodée en H.264 (logiciel, pas de carte vidéo) et le son en AAC. Choisi quand le codec / 10 bits / HDR n'est pas lisible, que l'image dépasse la hauteur voulue ou que le débit dépasse `max_kbps`. Une image de plus de 1080 p convertie sans `max_height` est réduite à 1080 p ; un 4K HEVC / AV1 produit un `warning`.

Paramètres de `…/stream` (déjà dans `url`, ne pas les recalculer) : `m` (`remux` \| `transcode`), `a` (indice de piste audio), `ca` (`1` : recopier le son, seulement en remux), `k` (débit en kb/s), `h` (hauteur), `c` (voies audio, 1 à 6), `start` (secondes, décimal).

### `GET /media/<entry>/file`

Le fichier d'origine : `Range` (206, `Accept-Ranges: bytes`, 416 hors limites), `Content-Length`, `Content-Type` d'après l'extension (`.mkv` → `video/x-matroska`…). `?download=1` ajoute `Content-Disposition: attachment`. 404 si le fichier est introuvable. Sert aussi la musique et les livres.

### `GET /media/<entry>/stream` — le flux converti

Un MP4 fragmenté produit à la volée par ffmpeg.

- **Pas de `Content-Length`, pas de `Range`** : la réponse porte `Accept-Ranges: none` et `Connection: close`, et le corps est envoyé au fil de l'eau. On ne peut pas demander un octet précis.
- **Avancer** : redemander la même URL avec `&start=<secondes>` (la conversion repart de là). La durée totale et la position absolue viennent du plan (`duration`), pas du lecteur : en flux converti, la position absolue est `start + position du lecteur`. L'interface web fait ainsi (`base + currentTime`) ; en `direct` (`seekable: true`), on se déplace dans le fichier normalement.
- Un flux converti occupe un emplacement (`max_streams`) jusqu'à ce que le client coupe la connexion. Au-delà : **503** (`N conversions en cours : attends la fin de l'une d'elles.`). Pas de ffmpeg : **501**. ffmpeg qui ne produit rien : 500 avec sa dernière ligne d'erreur. Une erreur **après** le début du flux ne peut plus être signalée : la connexion se ferme.
- 400 `paramètre <nom> invalide` pour un nombre mal formé.
- Les sous-titres ne sont jamais incrustés : le client les affiche lui-même.

### `GET /api/subs/<entry>/<e|s><n>.vtt`

Le sous-titre en WebVTT (`text/vtt`). `e<n>` : piste n° `n` du fichier ; `s<n>` : fichier voisin n° `n`. Les valeurs viennent de `subs[].url` : ne pas les construire. 404 `sous-titres introuvables`, 500 si la conversion échoue. Le cache est gardé côté serveur. Comme `/media/`, cette route accepte `?token=<media_token>` : un `<track src>` ou une piste de texte Cast peut donc charger `subs[].url` + `?token=…` sans en-tête.

## Images

`GET /img/item/<id>?w=` (affiche, pochette, couverture d'un élément) et `GET /img/entry/<id>?w=` (image d'un fichier). Voir « Conventions » pour `w`. Réponse : `image/jpeg` (réduite) ou le type d'origine, avec `ETag` et `Cache-Control: private, no-cache` ; envoyer `If-None-Match` pour obtenir un **304**. 404 `pas d'image` : fréquent et normal (un épisode sans vignette, un album sans `cover.jpg`).

## Livres

- `GET /api/book/<entry>` : `{"id", "item", "title", "format", "position", "file", "next": {"id", "title"} ou null}` plus, selon le format :
  - `cbz` : `pages` (entier) et `page_url` : un gabarit `/book/<id>/page/{n}` où `{n}` est le numéro de page **à partir de 0** ;
  - `epub` : `chapters` : liste d'URLs `/book/<id>/epub/<chemin>` dans l'ordre de lecture ;
  - autre format : `readable: false` (seul `file` est utilisable).
  - `position` : 0 si le livre est fini ou jamais ouvert, sinon la dernière position envoyée (page ou chapitre, **Ambigu** : le serveur la stocke telle quelle).
  - 400 si ce n'est pas un livre, 422 si l'archive est illisible.
- `GET /book/<entry>/page/<n>` : l'image de la page `n` (404 `page inexistante`).
- `GET /book/<entry>/epub/<chemin>` : un fichier de l'epub (XHTML, CSS, images), servi avec une politique `Content-Security-Policy` sans script. **Attention (jeton)** : les chapitres contiennent des liens relatifs (feuilles de style, images) ; une URL relative ne reprend pas le `?token=` de la page qui la contient, ces sous-ressources reçoivent donc un 302 vers `/login`. Un client natif qui affiche un chapitre doit ajouter lui-même `?token=<media_token>` à chaque requête (interception, ou réécriture des liens).

## Tâches de fond (gestion)

Une seule tâche tourne à la fois, les autres attendent. Redemander une tâche déjà en attente ou en cours renvoie celle-ci.

| Route | Corps | Réponse |
| --- | --- | --- |
| `GET /api/jobs?log=1` | | `{"jobs": [<tâche>], "busy": bool}` (30 dernières ; `log=1` ajoute le journal) |
| `POST /api/scan` | `{"lib": "<id>"}` (sans `lib` : tout) | **202** `{"job": <tâche>}` |
| `POST /api/enrich` | `{"lib": "<id>", "force": false}` | **202** `{"job": <tâche>}`. Séries / animations : TMDB (400 `pas de clé TMDB…` sans clé) ; musique : fiches artistes ; autre genre : 400 |
| `POST /api/cleanup` | `{"apply": false}` | **202** `{"job": <tâche>}` ; sans `apply`, simule |

`<tâche>` : `{"id", "label", "status": "waiting" \| "running" \| "done" \| "failed", "queued", "started", "ended", "result", "log"?}`. `started` / `ended` : `null` tant qu'ils n'ont pas eu lieu. **Ambigu** : la forme de `result` dépend de la tâche (compte rendu du scan, liste des fichiers du nettoyage…) et n'est pas figée ; ne pas en dépendre. Ces routes **modifient l'index et, pour `cleanup` avec `apply`, suppriment des fichiers parasites** de la médiathèque : un client de lecture n'a pas à les appeler.
