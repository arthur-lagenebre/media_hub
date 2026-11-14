# Interface HTTP de media_hub

Ce document décrit ce que le serveur (`hub/server/app.py`) fait aujourd'hui, pour un client que le dépôt ne contrôle pas : appli Android TV, iOS, récepteur Chromecast, script. Il est écrit d'après le code ; ce qui n'y est pas déterminé est signalé par **Ambigu**.

- Version du contrat : `GET /api/state` renvoie `"api": 1`. Ce nombre ne change qu'en cas de **rupture** (champ retiré, renommé, ou de sens différent). Ajouter un champ n'en est pas une : **un client ignore les champs qu'il ne connaît pas**.
- « optionnel » = la clé est **absente** de l'objet (jamais `null`), sauf mention contraire.
- Tout est en UTF-8. Les messages d'erreur et les libellés sont en français, sauf si la requête porte le cookie `hub_lang=en` (que l'interface pose quand on choisit l'anglais) : alors les libellés que le serveur compose lui-même (`label` des pistes audio et des sous-titres, le pays dans `info.facts` d'un artiste) et la page `/login` sont en anglais. Les autres messages (erreurs, tâches) restent en français : c'est le client qui les traduit (voir [lang-en.js](../hub/web/js/lang-en.js)). Un client natif qui veut l'anglais fait de même, ou pose ce cookie.

## Conventions

| Sujet | Règle |
| --- | --- |
| URLs | Les URLs renvoyées par l'API (`cover`, `image`, `file`, `url`, `page_url`…) sont **relatives à la racine du serveur** (`/img/item/ab12…`). Le client les accole à son adresse de base. Elles ne contiennent ni `?w=` ni jeton. |
| Images | Ajouter `?w=<largeur>` pour une version réduite (JPEG). Les largeurs sont arrondies à la valeur supérieure de **160, 320, 480, 720, 1280** (au-delà : 1280). Sans `w` (ou `w=0`) : l'image d'origine, avec son type MIME. |
| Jeton | `?token=<media_token>` s'ajoute à l'URL d'une image, d'un fichier, d'un flux ou d'une page de livre quand le client ne peut pas poser d'en-tête. C'est le **jeton de média** (`media_token` de `POST /api/token`), propre au compte, pas le jeton principal : voir « Authentification ». Si l'URL porte déjà une requête (`/media/<id>/stream?m=remux&…`), on ajoute `&token=…`. |
| Durées | En **secondes**, nombres (parfois décimaux). Une durée inconnue vaut `0`. |
| Dates | `added`, `updated`, `scanned`, `started`, `ended`, `queued` : horodatage Unix en secondes. `added` est calculé par le scan à partir des fichiers de l'élément (le plus récent d'entre eux, à la lecture du code). `year` est une **chaîne** (`""` si inconnue). |
| Identifiants | Opaques : chaînes alphanumériques (hexadécimales en pratique) fournies par le serveur. Ne pas les construire, ni les interpréter, ni les garder d'une base à l'autre (une base reconstruite peut en produire d'autres). Les identifiants de bibliothèque (`films`, `musique`…) sont ceux du serveur. |
| Corps des requêtes | `POST` en JSON (`Content-Type: application/json`), 1 Mo au plus (sinon 413). Une requête `POST` sans corps utile envoie `{}`. |
| Réponses | JSON compact avec `Cache-Control: no-store`, sauf images, fichiers, sous-titres. Pas d'en-têtes CORS : un client web d'une autre origine ne peut pas lire l'API ; les balises `<img>` / `<video>` fonctionnent. |
| Erreurs | `{"error": "message lisible"}` avec le code HTTP. Voir « Codes d'erreur ». |
| Profil | Un par compte : la progression, les playlists et « Reprendre » (`continue`) sont ceux du compte qui s'est connecté. Les bibliothèques, les fiches enrichies et les albums manquants sont communs. Serveur ouvert (`"auth": false`) : un profil unique, `default`, administrateur. |
| Droits | Un compte `user` a, pour chaque bibliothèque, le droit de **voir** (liste, fiches, images), celui de **lire** en ligne (`/api/play`, `/media/<id>/file` et `/stream`, sous-titres, livres) et celui de **télécharger** (`?download=1`). Lire et télécharger supposent voir, sans dépendre l'un de l'autre. Par défaut tout est permis ; l'administrateur règle les droits (voir « Comptes »). Une bibliothèque qu'on ne voit pas n'apparaît ni dans `/api/state`, ni dans l'accueil, la recherche ou « Reprendre ». Un administrateur a tous les droits. |

### Codes d'erreur

| Code | Sens |
| --- | --- |
| 400 | requête invalide (paramètre, corps, fichier d'un autre genre que celui demandé) |
| 401 | `connexion requise` (routes `/api/*`) ; `mot de passe incorrect` ou `nom d'utilisateur requis` pour `/api/token` |
| 403 | `réservé à l'administrateur` : scan, enrichissement, nettoyage, relecture des covers, photos d'artistes et gestion des comptes demandés par un compte `user`. `accès à cette bibliothèque non autorisé` : le compte ne voit pas la bibliothèque de l'élément, du fichier ou de l'image demandés. `lecture non autorisée` : la lecture (plan, flux, fichier sans `download`, sous-titres, livre) sans le droit de lire. `téléchargement non autorisé` : `?download=1` sans le droit de télécharger. |
| 302 | **hors `/api/`**, une requête non authentifiée n'a pas de 401 : elle est redirigée vers `/login`. Un client natif qui lit `/media/…`, `/img/…` ou `/book/…` sans jeton reçoit donc `302`, pas `401`. |
| 404 | identifiant inconnu, image absente, route inconnue (`introuvable`). Une route existante appelée avec la mauvaise méthode donne aussi 404. |
| 409 | ce nom de compte est déjà pris |
| 411 | `Content-Length obligatoire` : un envoi dans la boîte de réception sans cet en-tête, ou avec un corps en morceaux (`Transfer-Encoding`) |
| 413 | corps trop gros (corps JSON de plus de 1 Mo, image ou fichier à taille limitée) ; **jamais** pour la boîte de réception, qui n'a aucune limite de taille |
| 429 | trop d'essais de connexion depuis cette adresse (`/api/token`) ; boîte de réception : trop d'envois en cours (`HUB_INBOX_MAX_UPLOADS`) |
| 415 | image refusée (photo d'artiste : pas une image, ou illisible) ; boîte de réception : extension de fichier refusée |
| 507 | boîte de réception : pas assez d'espace libre sous `HUB_DATA` (`HUB_INBOX_KEEP_FREE`), ou disque plein pendant l'écriture |
| 416 | `Range` hors du fichier |
| 422 | livre illisible |
| 500 | erreur interne, ou ffprobe / ffmpeg a échoué (le message le dit) |
| 501 | ffmpeg est introuvable sur le serveur (flux converti impossible), ou ni Pillow ni ffmpeg pour recadrer une photo d'artiste |
| 503 | trop de conversions en cours (`HUB_MAX_STREAMS`) |

## Authentification

Si le serveur n'a aucun compte (`"auth": false` dans `/api/state`), tout est ouvert : il n'y a alors qu'un profil, `default`, administrateur. Dès qu'un compte existe, le serveur connaît deux sortes de jetons :

- le **jeton de session** (`token`), propre à un compte : il ouvre **tout** ce que les droits du compte permettent : l'API, les fichiers, et pour un administrateur les tâches de gestion (scan, nettoyage avec suppression de fichiers) et les comptes. C'est `<identifiant du compte>.<mac>` (64 caractères hexadécimaux après le point) ; **à traiter comme opaque**. Il ne se périme pas, mais il cesse de valoir quand le mot de passe du compte change, quand le compte demande de déconnecter ses autres appareils (`POST /api/me/signout-others`) ou quand le compte est supprimé ;
- le **jeton de média** (`media_token`), **propre au compte** (`<identifiant>.<signature>`, comme le jeton de session mais sous une autre signature) : il ne sert que dans l'URL des fichiers (`/media/`, `/img/`, `/book/`, `/api/subs/`) : si une URL de pochette fuit (journal d'un proxy, historique, récepteur Cast), son jeton n'ouvre ni l'API ni les tâches de gestion, mais il donne la **lecture des fichiers que ce compte peut voir** (films, livres, images, flux convertis dans la limite de `max_streams`), avec **les droits de ce compte** : une bibliothèque fermée ou un téléchargement refusé le sont aussi avec son jeton. Il ne se déduit pas du jeton de session. Il est révoqué avec les sessions du compte (changement de mot de passe, *Déconnecter les autres appareils*, suppression du compte) : un client qui avait gardé un jeton de média l'obtient de nouveau par `POST /api/token`. Les jetons de média d'avant les droits (64 caractères hexadécimaux, communs à tout le serveur) ne valent plus.

Les comptes sont créés par un administrateur (`POST /api/users`, ou `python -m hub user add` sur le serveur) : **il n'y a pas d'inscription publique**. Au tout premier démarrage, `HUB_PASSWORD` crée le compte administrateur (nom `HUB_ADMIN`, `admin` par défaut) tant que la base n'a aucun compte ; ensuite cette variable est ignorée. Ce premier compte reprend la progression et les playlists d'avant les comptes.

Trois façons de se présenter :

1. **Cookie** `hub=<jeton de session>` : posé par le formulaire `POST /api/login` (réponse 303). C'est le mode du navigateur.
2. **`Authorization: Bearer <jeton de session>`** : valable sur toutes les routes. C'est le mode d'un client natif. Le jeton de média y est refusé (401).
3. **`?token=<media_token>`** dans l'URL : **seulement** pour les chemins `/media/`, `/img/`, `/book/` et `/api/subs/` (les sous-titres, lecture seule), nulle part ailleurs sur `/api/`. Pour les balises `<img>`, `<video>`, `<track>` et le Cast, qui n'envoient pas toujours d'en-tête. **Le jeton de session y est refusé** (302 sur `/media/`, `/img/` et `/book/` ; 401 sur `/api/subs/`). Le serveur masque le jeton dans son journal ; un proxy placé devant ne le fait pas forcément.

### `POST /api/token`

Échange un compte et son mot de passe contre les deux jetons. Aucune authentification requise.

- Corps : `{"user": "…", "password": "…"}`. `user` (le nom du compte, sans tenir compte de la casse) est **facultatif quand le serveur n'a qu'un seul compte** ; avec plusieurs comptes, il est obligatoire.
- 200 : `{"token": "…", "media_token": "…", "user": {"id", "name", "role"}}`. Sans compte (serveur ouvert) : `{"token": "", "media_token": ""}` (pas de `user`).
- 401 : `{"error": "mot de passe incorrect"}` (mot de passe faux, nom inconnu ou corps illisible : même message), ou `{"error": "nom d'utilisateur requis"}` (plusieurs comptes et pas de `user`). La réponse prend au moins 0,4 s, et de plus en plus après chaque échec depuis la même adresse (jusqu'à 5 s) ; après 10 échecs en 5 minutes, plus rien n'est vérifié : **429** `{"error": "trop d'essais : réessaie dans N s"}`, même avec le bon mot de passe. Pas de verrouillage de compte : l'attente est par adresse, et se vide d'elle-même ou dès qu'un essai réussit (derrière un proxy, toutes les adresses n'en font qu'une).

`POST /api/login` (formulaire `user=…&password=…` ou JSON ; `user` facultatif de la même façon) pose le cookie mais répond par une **redirection 303** (`/`, `/login?error=1` ou `/login?error=2` quand il y a trop d'essais) : à éviter hors navigateur. `GET /api/logout` efface le cookie (303). Le formulaire `/login` demande le nom quand il y a plusieurs comptes.

## Un client en quatre appels

1. `POST /api/token` → `token` et `media_token` ; ensuite `Authorization: Bearer <token>` pour l'API, et `?token=<media_token>` au bout des URLs de fichiers quand le lecteur ne sait pas poser d'en-tête.
2. `GET /api/state` → bibliothèques (`id`, `kind`), disponibilité de ffmpeg, version du contrat.
3. `GET /api/library/<id>` puis `GET /api/item/<id>` → de quoi afficher et choisir un fichier.
4. `POST /api/play/<entry>` (vidéo) ou directement `file` (audio, livre) → quoi lire.

## État, accueil, recherche

### `GET /api/state`

```json
{"version": "…", "api": 1, "libraries": [{"id": "films", "label": "Films", "default_label": "Films", "kind": "movies", "count": 120, "scanned": 1730000000, "exists": true, "play": true, "download": true}],
 "ffmpeg": true, "ffprobe": true, "tmdb": false, "auth": true, "user": {"id": "default", "name": "admin", "role": "admin", "can_upload": true, "can_request": true}, "can_request": true, "requests_pending": 0, "settings": {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random"}, "busy": false, "streams": 0, "max_streams": 2}
```

- `libraries[].label` : le nom **affiché** de la bibliothèque (celui que l'administrateur a choisi avec `POST /api/library/<id>/label`, voir plus bas, sinon `default_label`) ; `libraries[].default_label` : son nom d'origine (celui de la configuration), où revenir. **L'identifiant `id` ne change jamais** : droits, préférences et données s'y accrochent ; un client s'en sert comme clé et n'écrit que `label`. Le même `label` s'écrit dans `GET /api/home`, `/api/search`, `/api/person`, `library` de `GET /api/library/<id>`, `/api/item/<id>` et `/api/artist/…`, et dans l'intitulé des tâches (`Mise à jour · Feuilletons`). Les droits d'un compte (`GET /api/users`) sont indexés par identifiant : le client y met les libellés de `/api/state`.
- `libraries[].kind` : `movies` | `shows` | `music` | `books`. `scanned` : horodatage du dernier scan, ou `null`. `exists` : le dossier est présent.
- `ffmpeg` / `ffprobe` : disponibles sur le serveur (sans eux : lecture directe seulement).
- `libraries` ne contient que les bibliothèques que **le compte de la requête voit** ; `libraries[].play` et `libraries[].download` disent s'il peut y lire en ligne et y télécharger (`true` pour un administrateur et sur un serveur ouvert). Un client masque ses boutons de lecture ou de téléchargement quand ils valent `false`. `continue` (accueil) ne propose rien des bibliothèques où le compte ne peut pas lire.
- `auth` : le serveur a des comptes. `user` : **le compte de la requête** (`role` : `admin` ou `user`) ; serveur ouvert : `{"id": "default", "name": <HUB_ADMIN>, "role": "admin", "can_upload": false, "can_request": true}`. `can_upload` : le compte peut envoyer dans la [boîte de réception](#boîte-de-réception) (`true` pour un administrateur ; `false` sur un serveur ouvert, où l'envoi est refusé). Un client masque les actions d'administration quand `role` n'est pas `admin`.
- `settings` : les réglages d'affichage **du compte de la requête** (voir `GET /api/me/settings`), pour qu'un client n'ait pas d'appel de plus au démarrage.
- `busy` : une tâche de fond tourne. `streams` / `max_streams` : conversions en cours / maximum.
- **Ambigu** : la route demande l'authentification ; `api` n'est donc lisible qu'après avoir obtenu le jeton.

### `GET /api/home`

```json
{"continue": [<carte>], "libraries": [{"id": "…", "label": "…", "kind": "…", "count": 12, "recent": [<élément>], "unseen": [<élément>]}], "requests_ready": [<demande arrivée>]}
```

- `recent` : au plus 24 éléments, du plus récemment ajouté au plus ancien. **Musique** : un album ajouté à un artiste qu'on avait déjà reste un album ; l'**arrivée d'un artiste** (au moins deux albums, tous arrivés à une semaine d'écart au plus, hors compilations et artistes inconnus) n'est qu'une vignette `type: "artist"` : `id` (celui de sa fiche), `lib`, `title`, `n` (nombre d'albums), `album` (son plus ancien album, pour la pochette), `added` (celui de son dernier album).
- `unseen` : **présent seulement sur les bibliothèques de film ou de série** (`kind` `movies` ou `shows`, quel que soit leur nom ; jamais la musique ni les livres). Au plus 48 éléments de cette bibliothèque que le compte n'a **pas touchés** : aucune progression sur aucun de leurs fichiers (48 et non 24 : l'interface en retire ceux de `recent`, puis coupe ; avec 24 seulement, les tris `added` et `rating` vidaient le rayon). **Sans ce que le compte a noté `down`** (« Je n'aime pas » : son « pas intéressé », voir `rating`), qui reste dans la bibliothèque et dans son filtre `todo`. Dans l'ordre du réglage `home_unseen_sort` du compte (voir `GET /api/me/settings`), appliqué avant la coupe à 48 : par défaut (`random`) un ordre qui ne dépend que du compte et du jour (le même toute la journée, un autre demain) ; `rating` : les mieux notés d'abord, ce qui n'a pas de note montrée à la fin ; `added` : les plus récemment ajoutés d'abord. L'interface en fait un rayon par bibliothèque, 12 par défaut, 24 au plus (réglage `home_unseen_count`), sans ceux que « Nouveautés » affiche déjà.
- `continue` : au plus 24 cartes « à reprendre ». Une **carte** est un objet *fichier* (voir plus bas) avec en plus : `item` (l'objet *élément* auquel il appartient), `position` (secondes ou page), `total`, `resume` (`true` : reprendre ici ; `false` : le fichier **suivant** d'un épisode ou d'un tome fini, à lancer depuis le début), `updated`.

### `GET /api/search?q=<texte>`

```json
{"results": [{"id": "films", "label": "Films", "kind": "movies", "items": [<élément>]}], "people": [{"name": "Penélope Cruz", "titles": 6}]}
```

- `results` : au plus 40 éléments par bibliothèque, ceux dont le titre ou le groupe (artiste, saga…) contient le texte (sans accents ni casse).
- `people` : au plus 8 personnes (réalisateurs, acteurs des films et séries) dont le nom **contient** le texte, comparé comme `results` (sans accents, casse ni espaces en trop). Ordre : le nom exact, puis ceux qui commencent par le texte, puis les autres ; à rang égal, le plus de titres d'abord, puis l'ordre alphabétique. `name` est la graphie la plus fréquente dans les bibliothèques que le compte voit, `titles` le nombre de titres distincts qu'il y voit (le même que dans `/api/person`). Une personne sans aucun titre visible n'est pas listée. Indépendant de `results` : un nom peut être trouvé sans qu'aucun titre ne porte le texte.
- Moins de 2 caractères : `{"results": [], "people": []}`.

### `GET /api/person?name=<nom>`

Ce qu'une personne a réalisé ou joué dans les **films et séries** (`movie`, `show`) :

```json
{"name": "Penélope Cruz", "roles": {"director": 0, "actor": 6}, "results": [{"id": "films", "label": "Films", "kind": "movies", "items": [<élément>]}],
 "enrichable": true, "missing": [{"tmdb": 1234, "title": "Volver", "year": "2006", "rank": 1, "cover": "/img/film/1234", "role": "actor"}]}
```

- `name` : la graphie la plus fréquente de ce nom dans les bibliothèques que le compte voit (à égalité, la première dans l'ordre des caractères : « PENELOPE » avant « Penelope »). Pour un nom inconnu : le nom demandé, espaces ramenés à un seul.
- Comparaison **exacte** une fois les accents, la casse et les espaces en trop retirés : `Penelope Cruz` trouve `Penélope Cruz`, mais `Chris Evans` ne ramène pas `Chris Evans Jr`, ni `Chris` seul. Le nom se passe encodé dans l'adresse (`%26` pour `&`, `%2F` pour `/`).
- `roles` : combien d'éléments cette personne a réalisés (`director`) et dans combien elle joue (`actor`). Un élément où elle est les deux compte dans chacun, mais n'est listé qu'une fois dans `results`.
- `results` : groupé par bibliothèque, dans l'ordre des bibliothèques visibles, sans les bibliothèques où elle n'a rien ; `<élément>` comme dans `/api/search` (avec `seen` / `started`). Du plus récent au plus ancien (`year`, ceux sans année en dernier, puis par titre).
- Une bibliothèque que le compte ne peut pas voir n'existe pas ici : ni ses éléments, ni son compte dans `roles`, ni sa graphie. Un nom qu'elle seule connaît répond comme un nom inconnu.
- Un nom inconnu : `200` avec `"roles": {"director": 0, "actor": 0}` et `"results": []`. `name` absent, vide ou fait d'espaces, ou de plus de 200 caractères : `400`.
- `enrichable` : toujours présent. `true` quand le bouton « films manquants » a un sens : le nom désigne **une seule** personne ayant un identifiant TMDB (voir `person_ids` ci-dessous) **et** le serveur a une clé `TMDB_KEY`. `false` sinon (nom inconnu ou que le compte ne voit pas, aucun identifiant, homonymes, pas de clé). Il ne dit pas si la personne a déjà été enrichie.
- `missing` : liste, **absente quand elle est vide** (rien d'enrichi, rien à montrer, pas d'identifiant). Les films que TMDB attribue à la personne (voir `POST /api/enrich/person`) et qu'**aucune bibliothèque que le compte voit** n'a ; même forme que `sagas[].missing` (`tmdb`, `title`, `year`, `cover`, `rank`) plus `role` (`"director"` ou `"actor"`). Du plus récent au plus ancien ; `rank` est la place dans cette liste, de 1 à N. `cover` : `/img/film/<tmdb>`, ou `""` sans affiche. La comparaison se fait **à chaque requête**, par identifiant TMDB du film : l'étiquette `TMDB` des films des bibliothèques de films visibles, **et** celle des fichiers étiquetés `movie/<n>` d'une bibliothèque de séries visible (un long-métrage rangé dans une bibliothèque de séries y est un élément série ; seule l'étiquette de son fichier dit que c'est un film, et un fichier étiqueté `tv/…` ne compte pas) : un film arrivé dans la médiathèque disparaît de la liste sans nouvel enrichissement ; un film que seule une bibliothèque cachée au compte possède y figure. Ces films ne sont ni des éléments ni des entrées : aucune liste, recherche ni compteur ne les voit.
- **Homonymes** : l'index se fait par le nom. Deux personnes qui s'appellent pareil (ou un nom écrit de deux façons qui se confondent une fois les accents retirés) n'en font qu'une, et leurs films sont mêlés. Une personne dont le nom est écrit différemment selon les fichiers (`Chris Evans` et `Christopher Evans`) en fait deux. Seul l'identifiant TMDB lève le doute : voir ci-dessous.
- **`person_ids`** : les étiquettes des films portent l'identifiant TMDB de chaque personne sous `DIRECTOR`, `WRITTEN_BY` et `ACTOR` (un tag fils `<TAG>/TMDB` valant `person/1234`, juste après son père ; une personne sans fils n'en a pas ; un tag `ACTOR` s'écrit « Nom (Rôle) » et le rôle peut avoir ses propres parenthèses : « Tom Hanks (Woody (voice)) » donne le nom « Tom Hanks », clé de `person_ids`, et le rôle « Woody (voice) »). Le scan les range dans le `meta` du film (`person_ids`: `{nom: nombre}`, absent des épisodes ; non exposé par `GET /api/item`). Pour une **série**, c'est l'enrichissement TMDB de la série (`POST /api/enrich`) qui écrit le même `person_ids` pour son casting (les 20 acteurs les plus présents ; deux acteurs de même nom n'y ont aucun identifiant), jamais les étiquettes des épisodes. L'index en tire, par nom, les identifiants vus avec leur nombre de films. **Une personne a un identifiant quand ses étiquettes n'en disent qu'un** ; avec deux ou plus (homonymes), elle n'en a aucun et n'est pas enrichissable. Un film sans identifiant ne contredit pas ceux qui en ont. Seules comptent les étiquettes des **bibliothèques que le compte voit** (`enrichable` et `missing` ne laissent rien deviner d'une bibliothèque cachée) ; l'administrateur les voit toutes.
- Les noms viennent des étiquettes des films et de la fiche TMDB des séries ; `GET /api/person` n'interroge rien en ligne (`missing` vient de ce que `POST /api/enrich/person` a gardé). L'index est construit au premier appel puis refait après chaque scan et chaque enrichissement.

### `POST /api/enrich/person`

Corps `{"name": "<nom>", "force": false}`. **Administrateur seulement** (403 sinon). Demande à TMDB les films de la personne (`person/<id>/movie_credits`) et les garde dans la base (`personinfo:<identifiant TMDB>`, commune à tous les comptes), avec leurs affiches sous `HUB_DATA/tmdb` (les mêmes fichiers que les films manquants des sagas). **202** `{"job": <tâche>}` ; la tâche s'appelle « Films de <nom> ».

- Retenus : les films **déjà sortis** (un film annoncé n'y entre qu'à la prochaine demande), réalisés par la personne (`job` « Director »), ou joués dans un des premiers rôles du casting (`order` de TMDB, qui commence à 0, au plus 10 : onze rôles) dans un film noté par au moins 100 personnes ; un film réalisé et joué n'y est qu'une fois, comme réalisé ; 40 au plus, du plus récent au plus ancien. **Le plafond de 40 est appliqué avant de comparer avec la médiathèque** : pour une filmographie plus longue, les films les plus anciens ne sont jamais proposés, même si on a tous les récents.
- Une personne connue depuis moins de 30 jours n'est pas redemandée (la tâche réussit sans appeler TMDB), sauf avec `force`.
- Erreurs : `400` nom absent, vide, non textuel ou de plus de 200 caractères ; `400 pas de clé TMDB…` sans `TMDB_KEY` ; `404 personne inconnue` ; `400` si le nom n'a aucun identifiant TMDB (« n'a pas d'identifiant TMDB »), ou en désigne plusieurs (homonymes). Une clé refusée par TMDB fait échouer la tâche (`status: "failed"`, le journal le dit) ; une réponse d'erreur de TMDB pour cette personne est comptée en échec (`result.failed`) sans rien effacer de ce qui était gardé.


### `POST /api/enrich/people`

Corps `{"lib": "<id>", "force": false}` (`lib` facultatif). **Administrateur seulement** (403 sinon). Fait pour les réalisateurs et les acteurs de premier plan ce que `POST /api/enrich/person` fait pour une personne : une tâche de fond (**202** `{"job": <tâche>}`). **Sans `lib`** : les bibliothèques de **films**, tâche « Films manquants · bibliothèques de films ». **Avec `lib`** : cette seule bibliothèque (films ou **séries** : une catégorie à la fois), tâche « Films manquants · <nom de la bibliothèque> » ; `400` pour une autre bibliothèque (musique, livres) ou un `lib` qui n'est pas un texte, `404` pour une bibliothèque inconnue. Une seule tâche à la fois, comme les autres ; la redemander pendant qu'elle tourne renvoie la même.

- **Qui** : les réalisateurs et les 11 premiers acteurs (l'ordre du casting dans les étiquettes) de chaque film, dont le nom désigne **une seule** personne ayant un identifiant TMDB (voir `person_ids` ; les homonymes et les personnes sans identifiant sont laissés). Pour une **série** : ses 11 acteurs les plus présents (le casting de TMDB, par nombre d'épisodes), **seulement si l'enrichissement TMDB de la série a gardé leur identifiant** (`POST /api/enrich`, « Enrichir (TMDB) » : une série enrichie avant cette version est refaite une fois, sans `force`). Une série pas encore enrichie ne donne personne. Chacune une fois. Pour quelques centaines de films, quelques milliers de personnes : un appel TMDB par personne (quelques dizaines de minutes), **puis les affiches** (des dizaines de milliers de films distincts sont plausibles). **Comptez une heure ou plus la première fois** et plusieurs centaines de Mo sous `HUB_DATA/tmdb` (estimation, non mesurée) ; les passes suivantes ne refont que les personnes de plus de 30 jours.
- **Reprise** : chaque personne est gardée dès qu'elle est faite ; une passe interrompue (clé refusée, redémarrage) reprend où elle en était, une personne connue depuis moins de 30 jours n'étant pas redemandée (sauf `force`).
- **Affiches** : pas d'affiche pour les films qu'on a déjà **au moment de la passe**, sur l'ensemble des bibliothèques (ceux des bibliothèques de films, et les fichiers étiquetés `movie/<n>` d'une bibliothèque de séries) ; seuls les films qui manquent en ont une. Conséquence : un film retiré de la médiathèque après la passe, ou que seule une bibliothèque cachée à un compte possède, apparaît grisé **sans affiche** (la lettre du titre) tant que la personne n'est pas redemandée (30 jours, ou `force` dans la requête : l'interface n'en envoie pas). La page d'une personne enrichie une par une (`POST /api/enrich/person`) garde, elle, l'affiche de tous ses films.
- **Compte rendu** : `result` = `{"people": <à parcourir>, "asked": <interrogées>, "fresh": <déjà connues>, "failed": <refusées par TMDB>, "films": <retenus>}`. Une personne que TMDB refuse est comptée en échec sans arrêter la passe ; une clé refusée l'arrête (`status: "failed"`). Le journal garde un résumé et une ligne toutes les 100 personnes, plus au plus 40 lignes d'échec ou d'affiche manquante (les suivantes sont comptées sans être écrites). **TMDB injoignable** : après 15 échecs de suite la tâche s'arrête en échec (`TMDB injoignable : …`), plutôt que de rester des heures sur un réseau coupé ; relancée, elle reprend où elle en était.
- Le résultat se lit sur la page de chaque personne (`GET /api/person`, `missing`) ; il n'y a pas de liste générale.

### `POST /api/library/<id>/label` — renommer l'affichage d'une bibliothèque

Corps : un **objet JSON**, `{"label": "<texte>"}`. Administrateur seulement. Un corps vide, illisible (JSON ou UTF-8 invalide) ou qui n'est pas un objet (liste, nombre, texte, `null`, formulaire) est refusé en **400** et ne rétablit rien ; seul un objet valide sans `label` (`{}`), `{"label": null}` ou `{"label": ""}` rétablit le nom d'origine. Ne change **que le nom affiché** : l'identifiant (`films`, `series`…), les droits, les éléments, les scans, la progression et les préférences ne bougent pas. On ne peut ni ajouter ni supprimer une bibliothèque par l'API (les dossiers se montent côté serveur : voir `HUB_SRV` et `HUB_LIBRARIES`).

```json
POST /api/library/series/label     {"label": "Feuilletons"}
200 {"id": "series", "label": "Feuilletons", "default_label": "Séries", "kind": "shows"}

POST /api/library/series/label     {"label": ""}            (ou {"label": null}, ou {})
200 {"id": "series", "label": "Séries", "default_label": "Séries", "kind": "shows"}
```

- **Texte** : le texte est mis en forme composée (NFC : « é » écrit en deux caractères devient un seul, et ne compte que pour un), puis les espaces de début et de fin sont retirés (ceux du milieu sont gardés) ; les accents et tous les alphabets sont permis, 1 à 40 caractères. Un texte vide (ou fait d'espaces) ou absent (`null`, ou pas de `label` dans un objet valide) **rétablit le nom d'origine** et efface la surcharge ; retaper le nom d'origine (même en deux caractères) fait de même. La réponse rend le libellé retenu (`label`, donc sans les espaces de bout) : l'afficher tel quel.
- **Doublon** : un nom déjà porté par **une autre** bibliothèque est refusé, à la casse et aux accents près (`séries`, `SERIES` valent `Séries`), aux formes de compatibilité Unicode près (`ＦＩＬＭＳ` en pleine largeur, ligature `ﬃ`) et aux espaces près (espace insécable, doubles espaces), qu'il soit son nom affiché ou son nom d'origine (on ne peut donc pas prendre le nom d'origine d'une bibliothèque renommée : elle pourrait vouloir y revenir). Le nom que s'est choisi une bibliothèque **absente de la configuration** (dossier non monté) est tout aussi réservé : elle le retrouvera à son retour. **Limite** : les homoglyphes d'un autre alphabet (un « Ѕ » cyrillique pour un « S ») ne sont pas détectés. Changer seulement la casse du nom d'une bibliothèque pour elle-même (`FILMS`) est permis.
- **Codes** :

| Code | `error` |
| --- | --- |
| 200 | la bibliothèque, à jour |
| 400 | `« <nom> » est déjà le nom d'une autre bibliothèque` (`<nom>` : le nom de l'autre, tel qu'il s'affiche ou d'origine) |
| 400 | `le libellé fait 40 caractères au plus` (plus de 40 caractères, après nettoyage) |
| 400 | `le libellé ne doit contenir ni caractère de contrôle ni caractère de mise en forme` (tabulation, saut de ligne, caractère nul, espace de largeur nulle, marque de sens d'écriture, séparateur de ligne ou de paragraphe, `U+FEFF`…) |
| 400 | `le libellé doit être un texte` (nombre, liste, objet, booléen) |
| 400 | `le corps doit être un objet JSON` (corps vide, illisible, ou qui n'est pas un objet) |
| 413 | `corps trop gros` (plus de 1 Mo) |
| 401 | `connexion requise` |
| 403 | `réservé à l'administrateur` |
| 404 | `bibliothèque inconnue` |

- **Conservation** : le nom choisi est gardé dans la base du hub (clé `library-labels` de `kv`) ; il survit aux redémarrages et aux scans. Un nom gardé qui n'est pas sûr à afficher (écrit à la main : trop long, avec un caractère de contrôle, espace de bout, non composé) est ignoré : le nom d'origine s'affiche. Si la bibliothèque disparaît de la configuration (dossier démonté, `HUB_LIBRARIES` changé) puis revient, son nom choisi revient avec elle. Les tâches déjà lancées ou terminées gardent l'intitulé qu'elles avaient.
- Les autres comptes voient le nouveau nom à leur prochaine requête (rien n'est poussé) : un client relit `GET /api/state`.

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
| `part` | nombre, optionnel | films, seulement dans une liste de saga (`group=…&sort=part`, `saga`, `sagas`) : le rang du film dans la saga. Dans une saga du fichier `sagas.json`, sa place dans la liste (les films absents de la médiathèque comptent : 1 et 3) ; sinon, dans une saga de l'étiquette que l'enrichissement TMDB a décrite (voir `missing`), son rang dans la collection TMDB (1 et 3 quand le 2 manque), et à défaut sa place de 1 à N dans l'ordre de sortie |
| `duration` | nombre, optionnel | durée de l'album (albums) |
| `artist_id` | chaîne, optionnel | identifiant d'artiste (albums), pour `/api/artist/<lib>/<id>` |
| `missing` | entier, optionnel | **séries de livres seulement** (`type: "series"`) : nombre de tomes manquants ; absent s'il n'en manque aucun. **Attention : dans la fiche (`GET /api/item/<id>`), le même nom porte une liste** (voir « Même nom, deux types »). Les albums n'ont pas de `missing` : leurs albums manquants sont dans `wanted` (`/api/artist`). |
| `cover_urls` | liste, optionnelle | **séries de livres de 2 tomes ou plus** (`type: "series"`) : jusqu'à 3 URLs de couvertures à empiler sur la carte, comme une saga de films. La première est `cover` (la couverture de la série), les suivantes `/img/entry/<id>` des tomes 2 et 3, dans l'ordre de la fiche. Absente d'une série d'un seul tome, et d'une série que le dernier scan n'a pas encore relue : `cover` seule suffit alors. Même nom que dans la vignette d'une saga de films. |
| `seen`, `started` | entiers, optionnels | présents si au moins un fichier a une progression (hors musique) : fichiers **vus**, fichiers **entamés** non finis. Un film est vu quand `seen >= max(1, n)`. |
| `quality`, `hdr` | chaîne, booléen, optionnels | **films** : la qualité d'image de leur premier fichier sondé (dans l'ordre de la fiche), mêmes valeurs que `quality` et `hdr` d'un `<fichier>` (`"4k"`, `"fullhd"`, `"hd"`, `"sd"`). Présents ensemble, dans les vignettes de `GET /api/library/<id>`, `GET /api/home` (`recent`, `unseen`), `GET /api/search`, `GET /api/person` et dans `saga` / `sagas` de la fiche ; **absents** tant qu'aucun fichier du film n'est sondé, et toujours pour une série, un album, un livre ou une vignette de saga. Les langues ne sont que dans la fiche. |
| `rating` | chaîne, optionnelle | **films et séries** : l'avis **du compte qui demande** sur cet élément, `"up"` (J'aime), `"mid"` (Bof) ou `"down"` (Je n'aime pas). **Absent** quand il n'a pas d'avis (« Bof » est un avis, pas l'absence d'avis). Posé par `POST /api/item/<id>/rating` ; présent dans la fiche (`GET /api/item/<id>`) et sur les vignettes de la bibliothèque, de l'accueil (`recent`, `unseen`, `item` des cartes de `continue`), de la recherche, de `GET /api/person` et des sagas de la fiche. Jamais sur une vignette de saga, d'artiste, d'album ni de livre. Privé : aucune route ne rend l'avis d'un autre compte |
| `tmdb_rating`, `tmdb_votes` | nombre, entier, optionnels | films et séries : la note moyenne des votants TMDB, sur 10, à une décimale (`7.8`), et leur nombre. Présents ensemble, seulement quand l'enrichissement TMDB l'a demandée et qu'au moins 25 personnes ont voté ; aussi dans la fiche (`GET /api/item/<id>`). Rafraîchie par l'enrichissement quand elle a plus d'un mois. |

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
| `artist` | chaîne, optionnel | artiste de la piste (audio ; peut être `""`), tel que le tag `ARTIST` l'écrit, liaisons comprises (« BABYMETAL x Poppy ») |
| `artists` | liste, optionnel | artistes de la piste, sans liaison, lus dans le tag `ARTISTS` (audio) : `[{"name": "BABYMETAL", "id": "<MusicBrainz>"}, …]`. `id` est l'identifiant MusicBrainz de l'artiste, ou `""` quand le fichier n'en donne pas autant que de noms (le hub n'associe un nom à son identifiant que si les deux listes ont la même longueur). **Absent** tant que le fichier n'a pas `ARTISTS` ou que le scan ne l'a pas relu : retomber sur `artist`. |
| `pages` | entier, optionnel | nombre de pages (livre, quand l'étiquette la donne) |
| `progress` | objet, optionnel | `{"position", "duration", "done", "updated"}` ; présent dans les `entries` d'un élément quand il y a une progression. `position` : secondes, ou page / chapitre pour un livre. |
| `quality` | chaîne, optionnel | vidéo **sondée**, dans les `entries` de `GET /api/item/<id>` seulement : `"4k"` (largeur ≥ 3200 ou hauteur ≥ 1800), `"fullhd"` (largeur ≥ 1800 ou hauteur ≥ 1000), `"hd"` (largeur ≥ 1200 ou hauteur ≥ 700), sinon `"sd"`. La largeur **ou** la hauteur suffit : un film en 2.39:1 fait 1920x800. |
| `hdr` | booléen, optionnel | vidéo sondée : image HDR (PQ ou HLG) |
| `audio` | liste de chaînes, optionnel | vidéo sondée : les langues du son, en codes ISO 639-1 minuscules (`["fr", "en"]`), une fois chacune, dans l'ordre des pistes, sans les pistes de commentaire. Une piste sans langue (ou `und`, ou d'une langue sans code à deux lettres) n'y est pas : la liste peut être vide. |
| `subs` | liste de chaînes, optionnel | vidéo sondée : les langues des sous-titres, même forme, sans les pistes forcées ; celles du fichier d'abord, puis celles des fichiers posés à côté de la vidéo (`Film.en.srt`). |

`quality`, `hdr`, `audio` et `subs` viennent ensemble ou pas du tout : ils sont **absents tant que le fichier n'a pas été sondé** par ffprobe (à sa première lecture, ou par la tâche « Analyse des vidéos » qui suit chaque mise à jour ; jamais sans ffprobe), et pour un fichier sans image. Un client les affiche en badges quand ils sont là et n'affiche rien sinon.

### Piste (artiste, playlist)

Objet plus léger : `id`, `title`, `artist`, `album` (titre), `item` (id de l'album), `duration`, **`cover`** (URL de la pochette de l'album), **`file`**. Les pistes d'une playlist ont en plus `number`. Une piste n'a pas d'image propre : sa pochette est `cover`.

## Une bibliothèque

### `GET /api/library/<id>`

Paramètres (tous optionnels) :

| Paramètre | Effet |
| --- | --- |
| `q` | filtre sur le titre / le groupe |
| `genre` | un genre |
| `sort` | `title` (défaut ; par artiste en musique, par collection en livres) \| `added` \| `year` \| `year-asc` \| `part` \| `my-rating` (« Mon avis » : **films et séries seulement** ; ce que le compte a noté `up`, puis `mid`, puis ce qu'il n'a pas noté, puis `down` ; à égalité par titre, puis par identifiant, donc un ordre toujours le même ; une vignette de saga n'a pas d'avis et vient avec ce qui n'est pas noté ; sur la musique et les livres, un tri inconnu) \| `rating` (« Mieux notés » : la note TMDB montrée, voir `tmdb_rating`, de la meilleure à la moins bonne, puis le plus de votes, puis le titre ; ce qui n'a pas de note montrée vient ensuite, par titre ; une vignette de saga prend la note de son meilleur film) |
| `filter` | `todo` (« À voir » : aucun fichier entamé, comme `unseen` de `GET /api/home`) \| `progress` (« En cours » : au moins un fichier entamé et pas tous vus) \| `done` (« Terminés » : tous les fichiers vus). Une série se juge sur ses épisodes (3 vus sur 10 : en cours ; tous vus : terminée) ; une vignette de saga sur ses films un par un (tous vus : terminée ; aucun entamé : à voir ; sinon en cours). `unseen`, l'ancien filtre « pas entièrement vu », est un alias de `todo` (anciens favoris) ; vide : pas de filtre ; toute autre valeur : **400** `filtre inconnu : <valeur>` (elle ne filtre plus « rien » en silence). Sur une bibliothèque de **musique**, `todo`, `progress`, `done` et `unseen` : **400** `ce filtre n'existe pas pour la musique` ; les livres gardent le filtre |
| `offset`, `limit` | pagination (`limit` : 240 par défaut, 600 au plus) |
| `sagas=1` | films : regroupe les sagas (au moins deux films) en une vignette `type: "saga"` |
| `view=artists` | musique : une vignette par **artiste**, sans ses albums, au lieu des albums. Chaque vignette : `id` (celui de sa fiche, `/api/artist/<lib>/<id>`), `lib`, `type: "artist"`, `title` (le nom ; « Various Artists » se dit « Artistes divers »), `n` (nombre d'albums), `letter`, `album` (son plus ancien album : sa pochette, faute de photo ; la photo est `/img/artist/<lib>/<id>`). Les filtres `genre`, `q` (un titre d'album trouve son artiste) et la pagination s'appliquent ; les artistes sans nom connu (« Artiste inconnu », « Artistes divers ») viennent en dernier ; `genres` compte des artistes |
| `group` | films : les films d'une saga, par son nom (`grp`, ou le nom d'une saga du fichier `sagas.json`) ; désactive `sagas` |
| `section` | livres : une section (Manga, BD…) |

Réponse :

```json
{"library": {"id": "…", "label": "…", "kind": "…"}, "total": 120, "offset": 0, "items": [<élément>],
 "genres": [["Action", 30]]}
```

- `total` : éléments après filtres (avant pagination). `genres` est compté sur **toute** la bibliothèque (hors filtres de genre / texte) : les 60 plus fréquents, rangés **par ordre alphabétique** sans tenir compte des accents ni de la casse (c'est l'ordre du menu de l'interface).
- Musique : `artists` et `albums`, le nombre d'artistes et d'albums de tout ce que les filtres laissent (pas seulement de la page). Avec `view=artists`, `artists` égale `total` et `albums` additionne les `n` des artistes ; sans, `albums` égale `total` et `artists` compte les artistes distincts de ces albums.
- Livres : `sections` : `[["Manga", 40], …]`. Avec `sagas=1` : `sagas` (nombre de vignettes de saga) et `films` (nombre de films en tout).
- Une vignette de **saga** : `id`, `lib`, `type: "saga"`, `title`, `year`, `years` (`[première, dernière]` ou `[]`), `grp: ""`, `n`, `letter`, `added`, `genres`, `covers` (ids des trois premiers films), **`cover_urls`** (leurs URLs), `seen` / `started` optionnels. **Elle n'a pas de `cover`** : l'identifiant d'une saga n'est pas celui d'un élément, `/img/item/<saga>` répondrait 404. Pour ses films : `GET /api/library/<id>?group=<title>&sort=part` ; la réponse porte alors `missing` (absent s'il est vide) : les films qui manquent à la saga, comme dans `sagas` de la fiche d'un film (`total` ne les compte pas). **Ambigu** : `lib` n'est présent que sur les sagas.
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
| `links` | liste, optionnel | `[{"label": "TMDB", "url": "https://…"}]` (films et séries), `[{"label": "MusicBrainz", "url": "https://…"}]` (albums dont les fichiers portent l'identifiant de l'édition) |
| `seasons` | liste, optionnel | séries ; **Ambigu** : forme tirée de l'étiquetage, non figée par le serveur |
| `season_badges` | objet, optionnel | séries : par numéro de saison (une chaîne, comme toute clé JSON, à rapprocher de `entries[].season`), ce que disent ses épisodes **sondés** : `{"1": {"quality": "fullhd", "hdr": false, "audio": ["ja", "fr"], "subs": ["fr"]}}`. `quality` : celle du plus grand nombre d'épisodes (la meilleure en cas d'égalité) ; `hdr` : vrai si tous le sont ; `audio`, `subs` : les langues que **tous** ont, dans l'ordre du premier épisode. Une saison sans épisode sondé n'y est pas ; **absent** si aucune n'en a. Mêmes valeurs que `quality`, `hdr`, `audio`, `subs` d'un `<fichier>`. |
| `saga` | liste d'`<élément>`, optionnel | films d'une saga : **tous** les films de la saga, **y compris le film consulté** (le client le reconnaît à son `id`), dans l'ordre de la saga (date de sortie), avec leur `part`. Seulement la saga que dit l'étiquette du fichier : voir `sagas` pour les autres |
| `sagas` | liste, optionnel | films : chaque saga à laquelle le film appartient et qui compte au moins deux films, manquants compris, celle de l'étiquette d'abord, puis celles de `sagas.json` : `[{"name": "Trilogie Cornetto", "films": [<élément>]}]`. Les films sont dans l'ordre de la saga, y compris le film consulté ; `name` s'utilise dans `group`. Une saga de `sagas.json` qui porte le nom d'une saga de l'étiquette la remplace ici, dans la grille et dans `group` : un nom ne désigne qu'une saga. Chaque saga porte en plus `missing` (absent s'il est vide) : les films qui lui manquent et qui sont **déjà sortis** (un film annoncé, ou sans date, d'une collection TMDB n'y figure pas : il apparaît le jour de sa sortie, sans rien relancer ; un film qu'une saga de `sagas.json` liste elle-même y figure toujours, avec `coming: true` en plus quand il n'est pas encore sorti : l'interface le grise avec « À venir » et ne le compte pas comme manquant), `[{"tmdb", "title", "year", "rank", "cover"}]` (`rank` : leur place, au milieu des `part` des films qu'on a ; `cover` : `/img/film/<tmdb>`, ou `""` sans affiche ; `ignorable: true` en plus, pour ceux que seule la collection TMDB d'une saga de l'étiquette compte : `POST /api/sagas/ignore` les cache). Ce ne sont pas des éléments : à afficher grisés, pas à ouvrir. Absent si le film n'est dans aucune saga |
| `missing` | liste, optionnel | séries de livres : `[{"label": "Naruto", "numbers": [3], "total": 3}]` : par série (ou arc), les numéros de tomes manquants et le total attendu. Absent s'il ne manque rien. **Remplace le compte** que portent les listes. |
| `tracks_total` | entier, optionnel | albums : nombre de pistes |
| `disc_titles` | objet, optionnel | albums de **plusieurs disques** : le titre que MusicBrainz donne à un disque (tag `DISCSUBTITLE`), par numéro de disque (une chaîne, comme toute clé JSON) : `{"2": "Bonus"}`, à rapprocher de `entries[].disc`. Seuls les disques titrés y sont ; un titre qui ne fait que répéter le numéro (« CD 2 », « Disc 02 ») en est écarté ; si les pistes d'un disque ne portent pas toutes le même titre, c'est le plus répandu. **Absent** pour un album d'un seul disque, sans disque titré, ou tant que le scan n'a pas relu les fichiers : l'intertitre est alors « Disque N » seul. |
| `release_types` | liste de chaînes, optionnel | albums : les types de sortie MusicBrainz (tag `RELEASETYPE`), en minuscules, le type principal d'abord : `["album", "live"]`. Valeurs courantes : `album`, `single`, `ep`, `compilation`, `live`, `soundtrack`, `remix` (d'autres existent : le client doit accepter une valeur inconnue). Si les pistes ne portent pas toutes les mêmes types, ceux que la plupart portent. **Absent** quand les fichiers n'en donnent pas (album pas encore repassé par `media_library`, ou scan pas relancé). `COMPILATION=1` n'est pas lu : MusicBrainz le met aussi sur le best-of d'un seul artiste, ce n'est pas « Artistes divers ». |
| `doubts` | liste de textes, optionnel | séries enrichies : ce que la recherche TMDB sur le nom du dossier n'a pas pu trancher (« titre partagé avec Hercule (1995) », « titre trouvé éloigné de la recherche »). Absent si la recherche est sûre, ou si la fiche est épinglée dans le nom du dossier ou choisie à la main |
| autres | | selon ce que l'étiquetage ou l'enrichissement a fourni, quand ils sont non vides : `directors`, `writers`, `actors`, `artist`, `artist_id`, `artist_mbids`, `label`, `catalog`, `formats`, `authors`, `publisher`, `section`, `collection`, `status`, `episodes_total`, `seasons_total`, `tmdb`, `unit`, `units`, `years`, `discs`, `disc_titles`, `release_types`, `season_summaries`. `artist_mbids` (albums) : liste d'identifiants MusicBrainz, un par artiste crédité de l'album (« A & B » en a deux), à comparer à ceux de `entries[].artists` ; vide ou absente tant que les fichiers n'ont pas été relus. **Ambigu** : leurs types ne sont pas contrôlés par le serveur (listes de chaînes pour les personnes, structures libres pour `discs`, `season_summaries`). |

404 : élément inconnu.

#### Même nom, deux types : `missing`

| Où | Type | Sens |
| --- | --- | --- |
| `<élément>` dans une liste (`/api/library`, `/api/home`, `/api/search`) | entier | nombre de tomes manquants (séries de livres) |
| `GET /api/item/<id>` (série de livres) | liste de `{"label", "numbers", "total"}` | le détail des manques |
| `GET /api/playlist/<id>` | entier | pistes de la playlist dont le fichier a disparu |
| `GET /api/library/<id>?group=…` (clé de premier niveau de la réponse) et `sagas[]` de la fiche d'un film | liste de `{"tmdb", "title", "year", "rank", "cover"}` | les films qui manquent à une saga (voir `sagas`) : ceux de la collection TMDB déjà sortis, et ceux qu'une saga de `sagas.json` liste elle-même |
| `GET /api/person` (clé de premier niveau de la réponse) | liste de `{"tmdb", "title", "year", "rank", "cover", "role"}` | les films qui manquent à une personne (voir `GET /api/person`) |

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
- Administration (usage interne de l'interface, **administrateur seulement**, 403 sinon) : `POST /api/artist/<lib>/<id>/photo[?crop=x,y,l,h]` (corps = image JPEG / PNG / WebP, 10 Mo au plus → `{"ok": true}`, 415 sinon) ; `crop` est un cadre en fractions de l'image (`0 ≤ x,y < 1`, `0 < l,h ≤ 1`, dans l'image, sinon 400) : ce qui est hors du cadre disparaît. La photo est convertie en WebP (1280 px au plus) et posée dans le dossier de l'artiste à la racine de la bibliothèque, `artist.webp` (sous `HUB_DATA/artists/` s'il n'en a pas, ou si ni Pillow ni ffmpeg ne convertissent : 501 alors si `crop` est demandé, 500 si le dossier n'est pas modifiable). `POST …/photo/crop` (corps JSON `{"x", "y", "w", "h"}`, mêmes fractions) recadre la photo gardée (404 s'il n'y en a pas).
- Liens de la fiche (administrateur seulement, 404 si l'artiste n'a pas de fiche `info`) : `POST /api/artist/<lib>/<id>/links` (corps `{"url", "label"?}` ; adresse `http` ou `https` avec un nom d'hôte, 2000 caractères au plus, 400 sinon ou si elle est déjà sur la fiche ; `label` de 80 caractères au plus, le nom du site par défaut ; aucune requête n'est faite vers l'adresse) ajoute le lien juste avant Wikipédia et MusicBrainz ; `POST /api/artist/<lib>/<id>/links/remove` (corps `{"url"}`, 404 si l'adresse n'est pas sur la fiche) retire n'importe quel lien, Wikipédia et MusicBrainz compris. Les deux rendent `{"links": [{"label", "url"}]}`, la liste à jour (celle de `info.links`). Ajouter une adresse retirée plus tôt annule le retrait. Ces choix survivent à « Fiches artistes ».
- `GET /img/artist/<lib>/<id>?w=` : la photo (404 s'il n'y en a pas). `GET /img/wanted/<lib>/<artist_id>/<clé>?w=` : pochette d'un album manquant. `GET /img/film/<tmdb>?w=` : l'affiche d'un film qui manque à une saga (404 s'il n'en a pas).

## Comptes

Chaque compte a un nom (1 à 40 caractères, unique sans tenir compte de la casse), un mot de passe (12 à 200 caractères à la création et au changement ; la connexion accepte un mot de passe plus ancien et plus court) et un rôle : `admin` ou `user`. Le premier compte est toujours administrateur. Serveur ouvert (aucun compte) : `GET /api/users` répond une liste vide et les autres routes ci-dessous répondent 400 (`le serveur est ouvert, sans compte…`) ; les comptes se créent alors sur le serveur (`HUB_PASSWORD` au premier démarrage, ou `python -m hub user add`).

| Route | Corps | Réponse |
| --- | --- | --- |
| `GET /api/users` | | `{"users": [<compte>]}` (du plus ancien au plus récent). Chaque `<compte>` porte `rights` : `{"<bibliothèque>": {"view": bool, "play": bool, "download": bool}}` pour chaque bibliothèque configurée (tout à `true` tant que rien n'est réglé, et pour un administrateur). Administrateur seulement. |
| `POST /api/users` | `{"name": "…", "password": "…", "role": "user"}` (`role` facultatif, `user` par défaut) | **201**, `<compte>`. 400 (nom, mot de passe ou rôle invalide), 409 `ce nom est déjà pris`. Administrateur seulement. |
| `POST /api/user/<id>/rights` | `{"rights": {"films": {"play": true, "download": false}, "livres": {"view": false}}}` | `<compte>` avec ses droits. Seules les bibliothèques citées changent, et un droit omis ne change pas ; ce sont des booléens. Retirer `view` retire aussi `play` et `download` ; rendre `view` ne les rend pas. 400 (bibliothèque ou droit inconnu, pas un booléen, **compte administrateur** : `un administrateur a tous les droits`), 404 `compte inconnu`. Valable tout de suite, sans reconnexion. Administrateur seulement. |
| `POST /api/user/<id>/upload` | `{"upload": true}` | `<compte>` avec son droit à jour. Donne (`true`) ou retire (`false`) le droit **Envoyer** : déposer des fichiers dans la [boîte de réception](#boîte-de-réception). Un compte neuf ne l'a pas ; les comptes d'avant la migration non plus. Valable à la requête suivante. 400 (`upload` n'est pas un booléen, **compte administrateur** : `un administrateur peut toujours envoyer`), 404 `compte inconnu`. Administrateur seulement. |
| `POST /api/user/<id>/request` | `{"request": false}` | `<compte>` avec son droit à jour. Retire (`false`) ou rend (`true`) le droit **Peut demander** des films et des séries (voir [Demandes](#demandes)) ; un compte neuf l'a, les comptes d'avant la migration aussi. Les demandes déjà faites restent. Valable à la requête suivante. 400 (`« request » vaut vrai ou faux`, **compte administrateur** : `un administrateur peut toujours demander`), 404 `compte inconnu`. Administrateur seulement. |
| `POST /api/user/<id>/password` | `{"password": "…"}` ; pour **son propre** compte : `{"password": "…", "current": "…"}` | `{"ok": true}`. L'administrateur donne un nouveau mot de passe à un autre compte, sans connaître l'ancien ; les sessions de ce compte sont fermées. Pour son propre compte (`<id>` = celui de l'appelant), `current` (le mot de passe actuel) est **obligatoire** comme pour `POST /api/me/password` : 400 `mot de passe actuel incorrect` (réponse lente, 0,4 s) s'il manque ou s'il est faux, et rien n'est changé ; la réponse porte alors le nouveau jeton (`{"ok": true, "token": "…"}`, plus un `Set-Cookie`). 404 `compte inconnu`. Administrateur seulement. |
| `POST /api/user/<id>/delete` | `{}` | `{"ok": true}`. Supprime le compte, sa progression, ses playlists et ses entrées « retirer de Reprendre ». **Ses fichiers de la [boîte de réception](#boîte-de-réception) restent** (l'administrateur les voit sous l'identifiant du compte) : à lui de les ranger ou de les supprimer. 400 `impossible de supprimer le dernier administrateur` ; 404 `compte inconnu`. Administrateur seulement. Un administrateur peut supprimer son propre compte tant qu'il en reste un autre. **Le jeton de média est global** : le compte supprimé perd son jeton de session, mais s'il a gardé des URLs `?token=<media_token>`, elles continuent de donner la lecture des fichiers (`/media/`, `/img/`, `/book/`, `/api/subs/`) tant que le secret du serveur n'a pas changé. |
| `GET /api/me/settings` | | `{"settings": {"music_view": "albums", "movie_view": "sagas", "home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random", "home_requests": "show"}}` : les réglages d'affichage du compte de la requête, gardés sur le serveur (ils le suivent d'un appareil à l'autre). `music_view` : `albums` (par défaut) ou `artists`, la façon dont s'ouvre la bibliothèque de musique ; `movie_view` : `sagas` (par défaut : les films d'une saga sur une seule carte, c'est `sagas=1` de `GET /api/library/<id>`) ou `films` (chaque film sur sa carte) ; `home_latest` et `home_unseen` : `show` (par défaut) ou `hide`, le rayon « Nouveautés » et les rayons « Pas encore vu » (un par bibliothèque vidéo) de la page d'accueil (l'API de l'accueil rend toujours les listes : c'est l'interface qui masque) ; `home_requests` : `show` (par défaut) ou `hide`, le rayon des demandes arrivées (voir [Demandes](#demandes)) : caché, `requests_ready` de `GET /api/home` vaut `[]` sans être calculé (rien n'est acquitté pour autant, tout revient quand il est remontré) ; `home_unseen_count` : `"6"`, `"12"` (par défaut), `"18"` ou `"24"`, le nombre de cartes de chaque rayon « Pas encore vu » (le serveur en rend toujours 48 au plus ; c'est l'interface qui retire ceux de « Nouveautés » puis coupe) ; `home_unseen_sort` : `random` (par défaut : un ordre qui ne dépend que du compte et du jour), `rating` (la note TMDB montrée, de la meilleure à la moins bonne ; les éléments sans note montrée, moins de 25 votes, à la fin) ou `added` (le plus récemment ajouté d'abord), l'ordre de `unseen` dans `GET /api/home` — décidé par le serveur **avant** la coupe à 48, pour le compte de la requête |
| `POST /api/me/settings` | `{"music_view": "artists"}` (un ou plusieurs réglages) | `{"settings": {…}}` : tous les réglages. 400 pour un réglage ou une valeur inconnus, sans rien changer. Supprimer un compte supprime ses réglages |
| `POST /api/me/signout-others` | `{"current": "…"}` | `{"ok": true, "token": "<nouveau jeton de session>"}`, avec un `Set-Cookie` qui renouvelle le cookie : tous les **autres** jetons du compte (autres appareils, autres navigateurs) répondent désormais 401, celui de l'appelant est remplacé par le nouveau. Le mot de passe ne change pas ; le jeton de média, lié à la même version du compte, est renouvelé (le client le redemande par `POST /api/token`). Tout compte. 400 `mot de passe actuel incorrect` (réponse lente, 0,4 s) s'il manque ou s'il est faux : sans lui, un jeton volé suffirait à mettre dehors son propriétaire. 400 `le serveur est ouvert…` sans compte. |
| `POST /api/me/password` | `{"current": "…", "new": "…"}` | `{"ok": true, "token": "<nouveau jeton de session>"}`, avec un `Set-Cookie` qui renouvelle le cookie du navigateur. Tout compte. 400 `mot de passe actuel incorrect` (réponse lente, 0,4 s) ou mot de passe neuf invalide. |

- `<compte>` : `{"id", "name", "role", "can_upload", "can_request", "created"}` (`created` : horodatage Unix ; `can_request` : le droit de demander des films et des séries, voir [Demandes](#demandes), donné par défaut et toujours `true` pour un administrateur ; `can_upload` : le droit d'envoyer dans la boîte de réception, toujours `true` pour un administrateur). Le haché du mot de passe ne sort jamais. L'`id` est opaque ; celui du premier compte est `default`.
- **Changer un mot de passe ferme toutes les sessions du compte** (le jeton est lié à une version du mot de passe) : un jeton `Bearer` ou un cookie détenu par un autre appareil répond alors 401. `POST /api/me/password` (et `POST /api/user/<son id>/password`, avec `current`) renvoie le nouveau jeton, qu'un client natif doit garder ; le jeton de média ne change pas.
- Pas de changement de rôle par l'API pour l'instant.

## Boîte de réception

Des comptes qui ont le droit **Envoyer** (`can_upload`, voir `POST /api/user/<id>/upload`) déposent des fichiers (films, séries, musique, livres, sous-titres, archives) sur le serveur. **Aucun n'entre dans une bibliothèque** : ils sont rangés dans la boîte (`HUB_INBOX`, par défaut `HUB_DATA/inbox`), dans un dossier nommé d'après le compte (nom nettoyé, `(2)` en cas de collision), éventuellement dans des sous-dossiers (4 niveaux au plus, voir **Sous-dossiers** plus bas), et l'administrateur les range lui-même. Un fichier déposé n'est jamais analysé ni indexé. Un client natif n'a pas de jeton de média pour ces routes : seul le jeton de session les ouvre.

| Route | Qui | Réponse |
| --- | --- | --- |
| `POST /api/inbox?name=<nom du fichier>[&dir=<chemin relatif>]` | un compte qui a le droit d'envoyer (l'administrateur toujours) | **201** `{"name": "Film (2).mkv", "size": 1048576, "account": "<dossier>", "dir": "Série/Saison 1"}` : `name` est le nom retenu, `account` le nom du dossier du compte, `dir` le sous-dossier retenu (`""` à la racine). `dir` est facultatif (voir **Sous-dossiers**). Le corps est **le fichier lui-même, en octets bruts** (pas de JSON, pas de `multipart`), `Content-Length` **obligatoire** |
| `GET /api/inbox` | administrateur | `{"files": [{"account", "user", "deleted", "dir", "name", "size", "date"}]}` : tous les envois, sous-dossiers compris, du plus récent au plus ancien ; `account` : le nom du dossier (ce que les URL ci-dessous attendent, encodé), `user` : le nom du compte (le nom du dossier si le compte a été supprimé : `deleted` vaut alors `true`), `dir` : le sous-dossier du fichier, chemin relatif en segments séparés par `/` (`""` à la racine du dossier du compte), `date` : horodatage Unix de la fin de l'envoi |
| `GET /api/inbox?mine=1` | un compte qui a le droit d'envoyer | `{"files": […], "used": 52428800, "extensions": [".aac", ".ape", …]}` : ses envois seulement (avec `dir`), l'espace qu'ils occupent (octets, sous-dossiers compris) et la liste triée des extensions acceptées (celle de **Le nom**, en minuscules avec le point), pour qu'un client écarte d'avance les fichiers qui seraient refusés (415) ; aucun quota ni taille maximale. `extensions` n'est que dans cette réponse |
| `GET /api/inbox/<dossier>/<nom>[?dir=<chemin relatif>]` | administrateur | le fichier en pièce jointe (`Content-Disposition: attachment`, `application/octet-stream`, `Range` accepté) |
| `DELETE /api/inbox/<dossier>/<nom>[?dir=<chemin relatif>]` | administrateur ; un compte qui a le droit, pour ses propres fichiers | `{"ok": true}`. 403 `ce fichier n'est pas à toi` pour le fichier d'un autre (qu'il existe ou non, avec ou sans `dir`). Supprimer le dernier fichier d'un sous-dossier retire le sous-dossier devenu vide (et ses parents devenus vides), jamais le dossier du compte |

**Le champ `account` désigne le dossier, depuis la 0.7.0.** Dans `GET /api/inbox`, dans la réponse de `POST /api/inbox` et dans les chemins `/api/inbox/<dossier>/<nom>`, `account` est le **nom du dossier** du compte : son nom nettoyé (accents gardés, caractères interdits par Windows remplacés), suivi de « (2) », « (3)… » en cas de collision à la casse près, et non plus son **identifiant** (`default`, ou hexadécimal) comme avant. Le lien compte → dossier est gardé en base et ne change plus ; un compte supprimé laisse son dossier, que la liste de l'administrateur montre sous le nom du dossier avec `deleted: true`. Un client qui gardait l'ancien identifiant doit relire la liste. Ce changement de sens est **assumé sans monter `API_VERSION`** (décision de l'utilisateur : le contrat reste en v1 pour le moment ; seul le client d'administration de l'interface web utilise ces routes). Le segment d'URL se code en pourcentage (`encodeURIComponent`) et accepte les accents ; il ne peut pas contenir `/`, `\`, `..`, un octet nul, un nom caché ou un nom réservé Windows (404).

**Envoyer.** Exemple : `curl -X POST -H "Authorization: Bearer <jeton>" --data-binary @Film.mkv "https://hub/api/inbox?name=Film.mkv"`. Le serveur contrôle tout **avant de lire le corps** et coupe alors la connexion (le client n'a pas à finir d'envoyer) ; il laisse d'abord arriver la réponse : une fois écrite, il ferme l'écriture, lit et jette ce que le client envoie encore (au plus 64 Mo et 5 secondes, puis coupe quoi qu'il arrive), pour qu'un navigateur en plein envoi lise le 403, 415, 429 ou 507 au lieu d'une erreur réseau. Dans l'ordre :

| Code | Cas |
| --- | --- |
| 401 | pas connecté |
| 403 | serveur ouvert, sans compte (`pas d'envoi sans compte…`) ; ou compte sans le droit (`tu n'as pas le droit d'envoyer des fichiers`) |
| 411 | `Content-Length` absent, ou corps en morceaux (`Transfer-Encoding`) |
| 400 | `Content-Length` illisible (autre chose que des chiffres : `+10`, `1_0`, `-1`…), répété deux fois, ou nul (`fichier vide`) ; nom absent, vide ou illisible ; `dir` refusé (voir **Sous-dossiers**) |
| 415 | extension refusée (voir ci-dessous) |
| 507 | l'espace libre sur le volume de la boîte (`HUB_INBOX`) tomberait sous `HUB_INBOX_KEEP_FREE` (10 Go) |
| 409 | `trop de sous-dossiers dans ton dossier (1000 au plus)` (plafond, voir **Sous-dossiers**) ; ou un fichier, un lien ou une jonction porte déjà le nom d'un des sous-dossiers demandés (`« X » : un fichier ou un lien porte déjà ce nom`) |
| 503 | la boîte est inutilisable (dossier introuvable ou non inscriptible, volume illisible) ; le message ne dit pas où, le journal du serveur oui |
| 429 | déjà `HUB_INBOX_MAX_UPLOADS` envois en cours (2) : c'est la seule réponse qui passe en réessayant plus tard |

Puis le corps est lu par morceaux de 1 Mo dans `<nom>.part`, renommé quand la longueur reçue vaut `Content-Length`. Une coupure, un délai de lecture dépassé (celui de la connexion, 120 s), un corps plus court que `Content-Length` (400 `envoi interrompu…`) ou un disque plein (507) effacent le `.part` ; la connexion est alors fermée. Les `.part` oubliés (arrêt brutal du serveur, en toute casse : `.PART` posé à la main compris, mais jamais un dossier ni un lien qui porterait ce nom) sont supprimés après 24 h, au démarrage puis chaque jour. Il n'y a pas de reprise d'un envoi interrompu.

**Le nom** (`name`, décodé de l'URL) est nettoyé : seul le dernier élément du chemin est gardé (`/` et `\` séparent ; `../../x.mkv` donne `x.mkv`), les caractères de contrôle et `<>:"|?*` deviennent `_`, aucun point ni espace au début ou à la fin, un nom réservé de Windows (`CON`, `NUL`, `COM1`…) est précédé de `_`, au plus 200 octets en UTF-8 (l'extension est gardée). Un fichier n'en écrase jamais un autre, un envoi en cours compris : `Film.mkv` devient `Film (2).mkv`, puis `Film (3).mkv`. Extensions acceptées (la casse ne compte pas, seule la dernière compte) : `.mkv .mp4 .m4v .avi .mov .wmv .webm .mpg .mpeg .m2ts .flv .ogv` ; `.flac .mp3 .m4a .aac .ogg .opus .wav .wma .ape .wv` ; `.epub .cbz .cbr .cb7 .pdf .mobi .azw3 .fb2` ; `.srt .ass .ssa .vtt .sub .idx .sup` ; `.zip`. Tout le reste (exécutables, scripts, pages web, images) : 415.

**Sous-dossiers** (additif, `API_VERSION` reste 1 : un client qui n'envoie pas `dir` ne change pas). `dir` est un chemin relatif, des segments séparés par `/` (`Série/Saison 1`), **4 niveaux au plus** sous le dossier du compte. Chaque segment est nettoyé comme un nom de dossier de compte : les caractères de contrôle et `<>:"|?*` deviennent `_`, aucun point ni espace au début ou à la fin (donc jamais de nom caché : `.cache` devient `cache`), un nom réservé de Windows (`CON`, `NUL`…) est précédé de `_`, au plus 120 octets en UTF-8, accents gardés (forme NFC). Le texte est décodé une fois par l'URL, jamais deux : `dir=%252e%252e` est un dossier nommé `%2e%2e`. Sont refusés en **400** (avant de lire le corps, comme les autres refus) : un segment vide (`a//b`, `/a`, `a/`), `.` ou `..` (jamais ignorés en silence), un segment fait seulement de points et d'espaces, une barre oblique inverse `\` (`« \ » n'est pas un séparateur de dossiers : sépare-les par « / » (?dir=…)`), plus de 4 niveaux (`dossiers trop profonds : 4 niveaux au plus`), un `dir` qui n'est pas un texte. Messages : `segment de dossier vide, « . » ou « .. » refusé`, `chemin de dossier attendu sous forme de texte (?dir=…)`.
Le sous-dossier est **créé au moment où un fichier y arrive** : jamais vide, jamais en cas de refus (le nom, l'extension, l'espace libre, le nombre d'envois et le plafond sont vérifiés avant), et retiré s'il est vide quand l'envoi échoue (corps trop court, coupure, disque plein), avec ses parents devenus vides, comme à la suppression d'un fichier ; un dossier qui contient quelque chose n'est jamais touché. Un dossier **vide** vers lequel un envoi échoue est retiré même s'il était déjà là (même un dossier vide préparé à la main), avec ses parents devenus vides ; le dossier du compte, lui, ne l'est jamais. Deux écritures qui ne diffèrent que par la casse ou la forme Unicode (`Dossier` / `dossier`, `Café` écrit de deux façons) ou qui se confondent après nettoyage (`a:b` / `a?b`) sont **le même dossier** : on garde l'écriture du premier, il n'y a jamais de `(2)` pour un dossier. Envoyer deux fois le même dossier le **fusionne** ; seuls les fichiers de même nom prennent un numéro (`Film (2).mkv`). Un fichier, un lien symbolique ou une jonction qui porte déjà le nom d'un sous-dossier demandé : 409, rien n'est écrit à travers. **Plafond** : 1000 sous-dossiers par compte, tous niveaux confondus ; un envoi qui en créerait au-delà est refusé en 409 sans rien créer, un fichier qui va dans un dossier existant n'est pas concerné (chaque dossier est une entrée de plus à lire à chaque liste, calcul d'espace et nettoyage).
La liste, `used`, le nettoyage des `.part` et la suppression parcourent les sous-dossiers (4 niveaux ; un dossier plus profond, déposé à la main, n'est pas listé) sans suivre un lien symbolique ni une jonction et sans sortir du dossier du compte. Pour **lire ou supprimer** un fichier d'un sous-dossier, l'URL garde sa forme et prend `?dir=` (le même chemin que `dir` dans la liste, encodé) : `GET /api/inbox/Léa/Film.mkv?dir=Série%2FSaison%201`. Ici le chemin n'est pas nettoyé : chaque segment doit déjà être un nom de dossier valable tel que la liste le donne ; tout le reste (`..`, `.`, vide, `\`, octet nul, nom caché ou réservé, plus de 4 niveaux, dossier absent, lien, jonction, chemin qui sort du dossier du compte) répond **404** `fichier introuvable`. Le droit se juge sur `<dossier>` (403 pour le dossier d'un autre compte), pas sur `dir`.

**Lire et supprimer.** `<dossier>` est le nom du dossier du compte (`account`, par exemple `Léa Dupont`), `<nom>` le nom du fichier tel que la liste le donne ; l'un et l'autre sont encodés dans l'URL (`L%C3%A9a%20Dupont`). Le dossier est celui de la base (`users.inbox_dir`) : il garde son nom si le compte est renommé, et celui d'un compte supprimé reste lisible par l'administrateur. Un chemin qui sort de la boîte (`..`, `%2e%2e`, séparateur, octet nul, nom caché, nom réservé de Windows, `.part`, lien symbolique, jonction Windows) répond 404 `fichier introuvable`. Un compte qui supprime ne peut viser que son dossier, par son nom exact. Le téléchargement et la liste complète sont réservés à l'administrateur (403 `réservé à l'administrateur`).

## Demandes

Un compte demande un film ou une série qui n'est pas dans la médiathèque ; l'administrateur lit la liste et l'ajoute lui-même (ou refuse). **Le hub ne télécharge rien** et n'écrit pas dans la médiathèque : une demande n'est qu'une liste d'attente. Additif : `API_VERSION` reste 1. Une demande désigne son élément par l'identifiant TMDB (`kind` : `movie` ou `show`), une seule demande par élément ; chaque compte qui la fait s'y joint. Pas de notification hors du hub.

**Qui voit quoi.** Chacun voit ses demandes (avec son message). D'une demande faite par un **autre** compte, il ne sait que « déjà demandé » (`request.mine: false` sur une case grisée, ou le code 200 au lieu de 201 quand il la rejoint) : ni nom, ni nombre, ni message. Seul l'administrateur voit les demandeurs et leurs messages.

**Le droit « Peut demander »** (`can_request`) est **donné par défaut** à tout compte, y compris ceux d'avant la migration ; l'administrateur le retire (`POST /api/user/<id>/request`) et demande toujours lui-même.

| Route | Qui | Corps | Réponse |
| --- | --- | --- | --- |
| `POST /api/requests` | un compte qui peut demander | `{"kind": "movie" \| "show", "tmdb": <entier>, "note": "…", "title": "…", "year": "AAAA"}` ; `note`, `title`, `year` facultatifs | **201** (nouvelle demande) ou **200** (le compte rejoint une demande existante, ou l'avait déjà faite : rien n'est doublé ni changé, son premier message reste) : `{"id": <entier>, "kind", "tmdb", "status", "mine": true}`. Voir les codes plus bas |
| `GET /api/requests?mine=1` | tout compte | | `{"requests": [<ma demande>]}` : les demandes du compte, de la plus récente à la plus ancienne |
| `GET /api/requests[?status=pending\|added\|declined]` | administrateur | | `{"requests": [<demande>]}` : toutes les demandes, de la plus récente à la plus ancienne, `status` en garde une sorte. 403 `réservé à l'administrateur` pour un autre compte (même avec `?mine=0`) ; 400 `statut inconnu (pending, added ou declined)` |
| `POST /api/requests/<id>/delete` | tout compte (la sienne) ; administrateur (toutes) | `{}` | `{"ok": true}`. Un compte **quitte** la liste des demandeurs (le dernier parti, la demande disparaît) ; l'administrateur **supprime la demande entière**, quels qu'en soient les demandeurs. 404 `demande inconnue` pour une demande inconnue ou qui n'est pas celle du compte (les deux répondent pareil) |
| `POST /api/requests/<id>/status` | administrateur | `{"status": "added" \| "declined" \| "pending", "reason": "…"}` (`reason` : motif d'un refus, facultatif, 200 caractères au plus, ignoré pour un autre statut) | `<demande>` à jour. Remet `ack` à 0 pour tous ses demandeurs. 400 `statut inconnu (pending, added ou declined)`, 400 `motif trop long (200 caractères au plus)`, 400 `le corps doit être un objet JSON`, 404 `demande inconnue`, 403 `réservé à l'administrateur` |
| `POST /api/requests/<id>/ack` | tout compte (la sienne) | `{}` | `{"ok": true}` : le compte a vu que sa demande est arrivée, elle quitte `requests_ready` de son accueil. 404 `demande inconnue` (inconnue, ou pas la sienne) |
| `POST /api/user/<id>/request` | administrateur | `{"request": true}` | `<compte>` avec son droit à jour : donne (`true`) ou retire (`false`) le droit de demander. Les demandes déjà faites restent. Valable à la requête suivante. 400 `« request » vaut vrai ou faux`, 400 `un administrateur peut toujours demander`, 404 `compte inconnu`, 403 `réservé à l'administrateur` |

**Codes de `POST /api/requests`**, dans l'ordre où ils sont vérifiés :

| Code | Message (`error`) | Cas |
| --- | --- | --- |
| 401 | `connexion requise` | pas connecté |
| 403 | `tu n'as pas le droit de demander des films ni des séries` | l'administrateur a retiré ce droit au compte |
| 413 | `corps trop gros` | corps de plus de 1 Mo, comme partout |
| 400 | `le corps doit être un objet JSON` | corps vide, illisible ou qui n'est pas un objet |
| 400 | `sorte de demande inconnue (movie ou show)` | `kind` absent ou autre |
| 400 | `identifiant TMDB attendu : un entier positif` | `tmdb` absent, chaîne, booléen, décimal, nul, négatif, ou 2⁵³ et plus |
| 400 | `message trop long (200 caractères au plus)`, `message : un texte est attendu` | `note` (espaces de bord retirés) |
| 400 | `titre trop long (200 caractères au plus)`, `titre : un texte est attendu`, `année attendue : 4 chiffres` | `title`, `year` (un nombre de 4 chiffres est accepté) |
| 409 | `déjà dans la médiathèque`, avec `"item": "<id de l'élément>"` | l'élément est dans une bibliothèque **que le compte voit** : `{"error": "déjà dans la médiathèque", "item": "ab12…"}` |
| 409 | `cette demande a été refusée : <motif>` (ou `cette demande a été refusée` sans motif) | l'administrateur l'a refusée : personne ne peut la refaire ni la rejoindre tant qu'il ne l'a pas remise en attente ou supprimée |
| 409 | `trop de demandes en attente (N)` | le compte a déjà `HUB_REQUEST_MAX` demandes `pending` (N, 20 par défaut) ; rejoindre une demande en attente compte aussi. L'administrateur n'a pas de plafond ; redemander ce qu'on a déjà demandé répond 200 |

**Titre et année.** Le serveur prend ce qu'il sait déjà d'un film (les films décrits pour une saga, `sagainfo`, ou pour la filmographie d'une personne, `personinfo:*`) ; à défaut, ceux du corps. Une série ne se trouve que par le corps.

**Objets.**

- `<ma demande>` : `{"id", "kind", "tmdb", "title", "year", "status", "reason", "created", "updated", "cover", "note", "ack", "asked", "mine": true}` et, quand elle est arrivée dans une bibliothèque **que le compte voit**, `item` (l'identifiant de l'élément à ouvrir). `status` : `pending` (en attente), `added` (ajoutée), `declined` (refusée) ; `reason` : le motif d'un refus (`""` sinon) ; `note` : son message (`""` sans message) ; `ack` : booléen ; `asked` : quand **ce compte** l'a demandée (horodatage Unix), `created` / `updated` : la demande elle-même ; `cover` : l'affiche de l'élément quand il est là et visible (`/img/item/<id>`), sinon celle que l'enrichissement a gardée d'un film absent (`/img/film/<tmdb>`), sinon `""`.
- `<demande>` (administrateur) : les mêmes champs sans `note`, `ack` ni `asked`, avec `users` : `[{"id", "name", "note", "created"}]` (les demandeurs, dans l'ordre où ils sont arrivés ; `name` vide pour un compte que la base n'a plus) et `mine` (l'administrateur en est-il). `item` est donné dès que l'élément est dans une bibliothèque, quelle qu'elle soit.

**L'état « ajouté » se calcule à la lecture**, sans tâche de fond : une demande `pending` dont l'élément est dans **n'importe quelle bibliothèque** passe à `added` (et y reste, même si le fichier repart) à la première lecture qui le voit (`GET /api/requests`, l'accueil, une case grisée, `/api/state` de l'administrateur). Un film est reconnu par l'étiquette TMDB de son fichier (`movie/<n>`, comme les films manquants des sagas ; un film rangé dans une bibliothèque de séries compte aussi), une série par la fiche TMDB que l'enrichissement lui a trouvée (ou le marqueur `{tmdb-…}` de son dossier). Un film **sans étiquette TMDB** n'est pas reconnu : l'administrateur marque la demande « ajoutée » à la main. Un refus n'est jamais défait par la médiathèque. Une demande arrivée dans une bibliothèque que le demandeur **ne voit pas** est `added` pour lui aussi, sans `item` et sans entrée dans son accueil.

**Ailleurs dans l'API** (champs ajoutés, rien de retiré) :

- `GET /api/state` : `can_request` (booléen : le compte peut-il demander), aussi dans `user` ; `requests_pending` (le nombre de demandes en attente) pour l'administrateur **seulement** (absent pour les autres comptes).
- `GET /api/home` : `requests_ready`, toujours présent (`[]` le plus souvent) : `[{"id", "kind", "tmdb", "title", "year", "item", "card": <élément>}]`, les demandes du compte arrivées dans une bibliothèque qu'il voit et pas encore acquittées (`POST /api/requests/<id>/ack`), la plus récemment arrivée d'abord ; `card` est la vignette de l'élément, comme celles des rayons. Quand le compte a caché ce rayon (réglage `home_requests` à `hide`), la liste est vide et rien n'est calculé.
- `missing` d'une saga (`GET /api/item/<id>`, `sagas[].missing` ; `GET /api/library/<id>?group=`) et d'une personne (`GET /api/person`) : un film absent qu'un compte a demandé porte `request: {"status": "pending" | "added" | "declined", "mine": true | false, "id": <entier>}` (absent sinon). `id` (l'identifiant de la demande, pour `POST /api/requests/<id>/delete`) n'est là **que si `mine` vaut `true`**. `mine: false` : un autre compte l'a demandé, sans dire lequel ni combien, et sans l'identifiant de sa demande.
- `<compte>` (`GET /api/users`, `POST /api/users`, `POST /api/user/<id>/…`, `POST /api/token`, `user` de `/api/state`) : `can_request`, toujours `true` pour un administrateur.
- `POST /api/user/<id>/delete` : le compte supprimé quitte ses demandes ; une demande qui n'a plus de demandeur disparaît.

## Sagas composées à la main

Des listes ordonnées de films, gardées dans `sagas.json` sous `HUB_DATA` (voir le README) ; elles s'ajoutent à la saga de l'étiquette (`saga` et `sagas` d'un film, `group=` d'une bibliothèque). Un film y est désigné par son identifiant TMDB. **Toutes ces routes sont réservées à l'administrateur** (403 `réservé à l'administrateur` sinon).

Un **film de saga** est `{"tmdb": 209112, "date": "2016-03-23", "item": <élément>}` ; `item` vaut `null` quand le film n'est pas dans la médiathèque : il garde sa place dans la liste et son numéro ; `date`, `title`, `year` et `cover` y sont alors ceux que l'enrichissement TMDB a trouvés (`date` vaut `""` et les trois autres sont absents tant qu'il n'a pas tourné).

| Appel | Corps | Réponse |
| --- | --- | --- |
| `GET /api/sagas` | | `{"version": "…", "sagas": [{"name": "Trilogie Cornetto", "films": [<film de saga>]}], "ignored": [{"tmdb": 1771, "title": "…", "year": "…"}]}` ; `version` est vide tant qu'il n'y a pas de fichier ; `damaged: true` (absent sinon) quand le fichier existe mais n'est pas lisible : les écritures sont alors refusées (409) pour ne pas l'écraser ; `ignored` : les films qu'on a demandé de ne pas voir comme manquants (`title` et `year` quand l'enrichissement les connaît) |
| `POST /api/sagas` | `{"sagas": [{"name": "…", "films": [747, 4638]}], "version": "…"}` : remplace **toutes** les sagas (l'ordre des listes est l'ordre des sagas). `version` (celle du dernier `GET`) est facultative | comme `GET`. 400 (nom vide, trop long ou en double sans tenir compte de la casse, identifiant illisible : rien n'est écarté en silence ; 200 sagas et 2000 films au plus), 409 `les sagas ont changé…` si le fichier a changé depuis cette `version` |
| `POST /api/sagas/add` | `{"name": "…", "films": [747], "items": ["<id d'élément>"]}` : ajoute à la fin de la saga (créée au besoin ; la casse du nom ne compte pas), sans doublon, `films` d'abord puis `items` | comme `GET`, plus `name` (le nom de la saga, tel qu'elle s'écrit). 400 (aucun film, nom vide, film sans identifiant TMDB), 404 (élément inconnu ou qui n'est pas un film) |
| `POST /api/sagas/ignore` | `{"tmdb": 1771, "ignore": true}` : cache un film des manquants des sagas de l'étiquette (`ignore` faux : le rend visible). Sans effet s'il l'est déjà. `ignore` doit être un booléen. Les films qu'une saga du fichier liste elle-même ne se cachent pas ; 5000 films cachés au plus | comme `GET`. 400 (identifiant illisible, `ignore` qui n'est pas un booléen, trop de films cachés), 409 (fichier illisible) |
| `GET /api/sagas/films?q=&group=&limit=` | | `{"films": [<film de saga>], "total": 3}` : les films de la médiathèque qui portent un identifiant TMDB (les seuls qu'une saga puisse recevoir), ceux dont le titre ou la saga contient `q`, ou ceux de la saga de l'étiquette `group`, dans l'ordre de sortie. `limit` : 40 par défaut, 600 au plus |

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
| `POST /api/item/<id>/rating` | `{"rating": "up"}` ; `"mid"`, `"down"`, ou `null` (ou pas de clé `rating`) pour effacer | `{"id": "<id>", "rating": "up"}` (`"rating": null` une fois effacé) : l'**avis privé** du compte sur un film ou une série entière (J'aime, Bof, Je n'aime pas), jamais un épisode, une saga, un album ni un livre. Un compte ne lit que les siens (voir `rating` d'un `<élément>`). **404** `élément inconnu` pour un élément qui n'existe pas **ou** d'une bibliothèque que le compte ne voit pas (on ne distingue pas les deux) ; **400** `le corps doit être un objet JSON` (corps vide, illisible ou qui n'est pas un objet : l'avis n'est jamais effacé en silence ; `{}` ou un objet sans clé `rating` efface), `avis inconnu : <valeur>` (`up`, `mid`, `down` ou `null` seulement), `cette bibliothèque ne se note pas` (musique, livres ; effacer aussi) ; **413** corps trop gros. Une **saga** calculée par le serveur (vignette `type: "saga"`) n'a pas d'identifiant d'élément : son identifiant répond 404 `élément inconnu`, comme celui d'un épisode. L'avis suit l'identifiant de l'élément : un dossier renommé perd le sien, un scan ne le touche pas. Supprimer le compte supprime ses avis |
| `POST /api/item/<id>/dismiss` | `{}` | `{"ok": true}` : retire l'élément de `continue` sans toucher à la progression ; il y revient dès qu'on le relance. La carte de « Reprendre » propose aussi *Remettre à zéro* : `POST /api/item/<id>/seen` avec `{"done": false}` efface la progression de tout l'élément |
| `POST /api/item/<id>/poster` | l'image elle-même (JPEG, PNG ou WebP, 10 Mo au plus) | `{"ok": true}` : l'affiche d'un film ou d'une série, **administrateur seulement**. Elle passe avant toutes les autres sources et reste dans `HUB_DATA/posters` : la médiathèque n'est pas touchée. Un WebP est converti en JPEG (le WebP n'est jamais gardé tel quel) ; il n'y a pas de retour à l'affiche d'avant, la nouvelle devient l'affiche. 400 pour un autre type d'élément, 413 trop gros, 415 ce n'est pas une image (ou un WebP illisible), 501 ni Pillow ni ffmpeg pour convertir un WebP |
| `GET /api/item/<id>/tmdb?q=…` | | `{"results": [{"tmdb": 2567, "name": "Hercule", "year": "1998", "overview": "…"}]}` : les séries que TMDB propose pour `q`, dix au plus, dans l'ordre de TMDB. **Administrateur seulement**, pour une série. 400 (`q` vide, autre type d'élément, pas de clé `TMDB_KEY`), 502 (TMDB ne répond pas ou refuse la clé) |
| `POST /api/item/<id>/tmdb` | `{"tmdb": 4615}` ou l'adresse de la fiche : `{"tmdb": "https://www.themoviedb.org/tv/4615-hercules-the-legendary-journeys"}` | `{"ok": true}` : fixe la fiche TMDB de la série et l'enrichit à nouveau (synopsis, saisons, casting, affiche), tout de suite. Le choix reste dans la base du hub et passe avant l'identifiant épinglé dans le nom du dossier (`{tmdb-4615}`) et avant la recherche sur le nom ; la médiathèque n'est pas touchée. **Administrateur seulement**. 400 (identifiant illisible, autre type d'élément, pas de clé), 502 (TMDB ne connaît pas cet identifiant : le choix d'avant est gardé) |

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
| `subs` | liste | `{"id", "label", "lang", "forced", "default", "text", "image", "where", "url"}` ; `where` : `mkv` (dans le fichier) ou `fichier` (fichier voisin) ; `text` : piste de texte, `url` en WebVTT ; `image` : `"pgs"` pour un sous-titre en image de Blu-ray, dont `url` est le `.sup` brut (à dessiner côté client) ; `""` sinon. **`url` vaut `""` si `text` et `image` sont faux** (VobSub, DVD : non pris en charge) |
| `video` | objet ou `null` | `{"codec", "width", "height", "hdr"}` |
| `next` | objet ou `null` | `{"id", "title", "season", "number"}` : le fichier vidéo suivant du même élément (épisode, partie) |

#### Les trois modes

L'ordre de préférence est toujours **direct, puis remux, puis transcode**.

- **`direct`** — `url` = `/media/<id>/file` : le fichier tel quel, avec `Range`. Aucun calcul côté serveur. Choisi si l'image est lisible (codec dans `caps.video`, 8 bits ou 10 bits annoncé, pas de HDR non géré, pas plus haut que la limite, pas plus lourd que `max_kbps`), si le conteneur est dans `caps.containers`, si le son est lisible (codec dans `caps.audio` et pas plus de voies que `caps.channels`) **et** si la piste est celle par défaut.
- **`remux`** — `url` = `/media/<id>/stream?m=remux&…` : l'image est recopiée sans calcul ; le son est recopié s'il est lisible (`ca=1`), sinon converti en AAC (`ca=0`). Choisi quand seul le conteneur, le son ou le choix d'une autre piste audio empêche le mode direct.
- **`transcode`** — `url` = `/media/<id>/stream?m=transcode&…` : l'image est ré-encodée en H.264 (logiciel, pas de carte vidéo) et le son en AAC. Choisi quand le codec / 10 bits / HDR n'est pas lisible, que l'image dépasse la hauteur voulue ou que le débit dépasse `max_kbps`. Une image de plus de 1080 p convertie sans `max_height` est réduite à 1080 p ; un 4K HEVC / AV1 produit un `warning`.

Paramètres de `…/stream` (déjà dans `url`, ne pas les recalculer) : `m` (`remux` \| `transcode`), `a` (indice de piste audio), `ca` (`1` : recopier le son, seulement en remux), `k` (débit en kb/s), `h` (hauteur), `c` (voies audio, 1 à 6), `start` (secondes, décimal).

### `GET /api/play/<entry>/start` — où part vraiment un flux `remux`

Paramètre : `t` (secondes, décimal ; 0 par défaut). Réponse : `{"start": 40.0}`.

Dans un flux `remux` l'image est recopiée : elle ne peut commencer que sur une image clé. `start` est l'image clé sur laquelle le fichier se place pour `t` (à la milliseconde, souvent un peu avant `t`, parfois plusieurs secondes). C'est la valeur à passer à `…/stream?…&start=` **et** celle à partir de laquelle le client compte sa position. `0` pour un `t` nul ou négatif ; `t` lui-même si ffprobe ne répond pas (le client part alors de ce qu'il a demandé, avec le décalage possible). Le serveur ajoute lui-même une marge de 0,25 s à l'instant donné à ffmpeg pour une image recopiée, pour retomber sur cette image clé et pas sur la précédente (voir `KEYFRAME_MARGIN`).

Droit : lire la bibliothèque. 400 : le fichier n'est pas une vidéo, ou `t` n'est pas un nombre ; 404 : fichier inconnu.

### `GET /media/<entry>/file`

Le fichier d'origine : `Range` (206, `Accept-Ranges: bytes`, 416 hors limites), `Content-Length`, `Content-Type` d'après l'extension (`.mkv` → `video/x-matroska`…). `?download=1` ajoute `Content-Disposition: attachment` et demande le droit de **télécharger** la bibliothèque (403 `téléchargement non autorisé` sinon) ; lire le fichier sans `download` (lecture directe, `Range`) demande celui de la **lire** (403 `lecture non autorisée`) : le droit de télécharger ne peut pas empêcher un lecteur qui reçoit le fichier de le garder. 404 si le fichier est introuvable. Sert aussi la musique et les livres.

### `GET /media/<entry>/stream` — le flux converti

Un MP4 fragmenté produit à la volée par ffmpeg.

- **Pas de `Content-Length`, pas de `Range`** : la réponse porte `Accept-Ranges: none` et `Connection: close`, et le corps est envoyé au fil de l'eau. On ne peut pas demander un octet précis.
- **Avancer** : redemander la même URL avec `&start=<secondes>` (la conversion repart de là). La durée totale et la position absolue viennent du plan (`duration`), pas du lecteur : en flux converti, la position absolue est `start + position du lecteur`. L'interface web fait ainsi (`base + currentTime`) ; en `direct` (`seekable: true`), on se déplace dans le fichier normalement.
- **Avancer en `remux`** : l'image est recopiée, le flux ne peut commencer que sur une **image clé**, parfois plusieurs secondes avant l'instant voulu. Demander `start=<t>` tel quel fait donc partir le flux plus tôt que `t` sans le dire, et la position `start + position du lecteur` (horloge, sous-titres, progression) glisse d'autant. Avant de redemander le flux, appeler `GET /api/play/<entry>/start` (plus bas) et utiliser son `start`. En `transcode`, l'image est ré-encodée : le flux part exactement de `start`, aucun appel n'est nécessaire.
- Un flux converti occupe un emplacement (`max_streams`) jusqu'à ce que le client coupe la connexion. Au-delà : **503** (`N conversions en cours : attends la fin de l'une d'elles.`). Pas de ffmpeg : **501**. ffmpeg qui ne produit rien : 500 avec sa dernière ligne d'erreur. Une erreur **après** le début du flux ne peut plus être signalée : la connexion se ferme.
- 400 `paramètre <nom> invalide` pour un nombre mal formé.
- Les sous-titres ne sont jamais incrustés : le client les affiche lui-même (texte, ou image PGS dessinée sur un canvas).

### `GET /api/subs/<entry>/<e|s><n>.<vtt|sup>`

Le sous-titre en WebVTT (`.vtt`, `text/vtt`) ou, pour une piste en image PGS, tel quel au format `.sup` (`.sup`, `application/octet-stream` : la suite de segments PGS, horodatés comme dans le film, que le client décode et dessine ; l'interface le fait avec `hub/web/js/pgs.js`). `e<n>` : piste n° `n` du fichier ; `s<n>` : fichier voisin n° `n` (WebVTT seulement). Les valeurs viennent de `subs[].url` : ne pas les construire. 404 `sous-titres introuvables` (dont un `.sup` demandé pour une piste qui n'est pas un PGS, ou pour un fichier voisin), 500 si la conversion ou l'extraction échoue. Le cache est gardé côté serveur ; la première extraction d'un `.sup` lit tout le fichier vidéo, ce qui peut durer plusieurs minutes pour un remux de 30 Go (délai de 15 minutes), les suivantes sont instantanées. Comme `/media/`, cette route accepte `?token=<media_token>` : un `<track src>` ou une piste de texte Cast peut donc charger `subs[].url` + `?token=…` sans en-tête.

## Images

`GET /img/item/<id>?w=` (affiche, pochette, couverture d'un élément) et `GET /img/entry/<id>?w=` (image d'un fichier). Voir « Conventions » pour `w`. Réponse : `image/jpeg` (réduite) ou le type d'origine, avec `ETag` et `Cache-Control: private, no-cache` ; envoyer `If-None-Match` pour obtenir un **304**. 404 `pas d'image` : fréquent et normal (un épisode sans vignette, un album sans `cover.jpg`).

## Livres

- `GET /api/book/<entry>` : `{"id", "item", "title", "format", "position", "file", "next": {"id", "title"} ou null}` plus, selon le format :
  - `cbz` : `pages` (entier) et `page_url` : un gabarit `/book/<id>/page/{n}` où `{n}` est le numéro de page **à partir de 0** ;
  - `cbz` aussi : `rtl` (booléen), le sens de lecture par défaut du tome, `true` pour de droite à gauche. Le champ `Manga` de sa fiche `ComicInfo.xml` le dit (`YesAndRightToLeft` : `true` ; `Yes` et `No` : `false`, sans tenir compte de la casse) ; sans champ (ou `Unknown`, ou tome sans fiche), `true` si la section de la série est « Manga », sinon `false`. Absent pour un epub ou un format illisible ;
  - `epub` : `chapters` : liste d'URLs `/book/<id>/epub/<chemin>` dans l'ordre de lecture ;
  - autre format : `readable: false` (seul `file` est utilisable).
  - `position` : 0 si le livre est fini ou jamais ouvert, sinon la dernière position envoyée (page ou chapitre, **Ambigu** : le serveur la stocke telle quelle).
  - 400 si ce n'est pas un livre, 422 si l'archive est illisible.
- `GET /book/<entry>/page/<n>` : l'image de la page `n` (404 `page inexistante`).
- `GET /book/<entry>/epub/<chemin>` : un fichier de l'epub (XHTML, CSS, images), servi avec une politique `Content-Security-Policy` sans script. **Attention (jeton)** : les chapitres contiennent des liens relatifs (feuilles de style, images) ; une URL relative ne reprend pas le `?token=` de la page qui la contient, ces sous-ressources reçoivent donc un 302 vers `/login`. Un client natif qui affiche un chapitre doit ajouter lui-même `?token=<media_token>` à chaque requête (interception, ou réécriture des liens).

## Tâches de fond (gestion)

Une seule tâche tourne à la fois, les autres attendent. `POST /api/scan`, `/api/enrich`, `/api/enrich/person`, `/api/enrich/people` et `/api/cleanup` sont **réservés à l'administrateur** (403 `réservé à l'administrateur` sinon) ; `GET /api/jobs?log=1` (avec le journal des tâches) l'est aussi ; `GET /api/jobs` sans `log` reste ouvert à tout compte (c'est lui qui dit si le serveur est occupé). Redemander une tâche déjà en attente ou en cours renvoie celle-ci.

| Route | Corps | Réponse |
| --- | --- | --- |
| `GET /api/jobs` | | `{"jobs": [<tâche>], "busy": bool}` (30 dernières, sans journal). Tout compte. |
| `GET /api/jobs?log=1` | | Idem, avec le journal de chaque tâche (`log`). Administrateur seulement : 403 sinon. |
| `POST /api/scan` | `{"lib": "<id>"}` (sans `lib` : tout) | **202** `{"job": <tâche>}` |
| `POST /api/enrich` | `{"lib": "<id>", "force": false}` | **202** `{"job": <tâche>}`. Séries : TMDB (400 `pas de clé TMDB…` sans clé) ; films : les films qui manquent aux sagas, décrits par TMDB (400 sans clé ; une collection déjà connue n'est redemandée qu'après un mois, ou avec `force`) ; films et séries : ensuite, la note TMDB de chaque élément qui a un identifiant TMDB (voir `tmdb_rating`), redemandée après un mois, ou avec `force` ; musique : fiches artistes ; autre genre : 400 |
| `POST /api/enrich/person` | `{"name": "<nom>", "force": false}` | **202** `{"job": <tâche>}` : les films d'une personne d'après TMDB, pour `missing` de `GET /api/person` (voir cette route : erreurs, filtres, cache d'un mois) |
| `POST /api/enrich/people` | `{"lib"?: "<id>", "force": false}` | **202** `{"job": <tâche>}` : les films des réalisateurs et des acteurs d'après TMDB (voir ci-dessous) : sans `lib` ceux des bibliothèques de films, avec `lib` ceux de cette bibliothèque (films, séries), pour `missing` de `GET /api/person`. 400 sans clé TMDB (`pas de clé TMDB…`) ; une clé refusée par TMDB fait échouer la tâche |
| `POST /api/images/refresh` | `{"lib": "<id>"}` | `{"ok": true, "removed": 12}` : vide le cache des vignettes des éléments et des fichiers de cette bibliothèque (à chaque largeur, marques « pas d'image » comprises) ; chaque image se refait depuis le disque (`folder.jpg`, jaquette du `.mkv`…) à la prochaine demande, utile quand un fichier image a changé sans changer de nom. Les affiches choisies à la main et celles de TMDB sont gardées. Administrateur seulement ; 404 bibliothèque inconnue |
| `POST /api/cleanup` | `{"apply": false}` | **202** `{"job": <tâche>}` ; sans `apply`, simule. Le `result` de la tâche terminée : `{"found": 12, "removed": 0, "sample": ["Musique/Daft Punk/Thumbs.db", …]}` : `sample` nomme les 20 premiers fichiers (dans l'ordre alphabétique, depuis le dossier de leur bibliothèque), `found` les compte tous ; une simulation les écrit aussi dans le journal |

`<tâche>` : `{"id", "label", "status": "waiting" \| "running" \| "done" \| "failed", "queued", "started", "ended", "result", "log"?}`. `started` / `ended` : `null` tant qu'ils n'ont pas eu lieu. **Ambigu** : la forme de `result` dépend de la tâche (compte rendu du scan, liste des fichiers du nettoyage…) et n'est pas figée ; ne pas en dépendre. Une mise à jour (`POST /api/scan`, scan automatique) est suivie d'une tâche **« Analyse des vidéos »**, que rien d'autre ne lance, quand ffprobe est là et qu'il reste des vidéos jamais sondées : elle les sonde une par une (d'où `quality`, `hdr`, `audio`, `subs` d'un `<fichier>`), dit au journal celles qu'elle n'a pas su lire sans échouer pour autant, et, dès qu'une autre tâche attend, s'arrête entre deux fichiers pour reprendre après elle (une seconde tâche du même intitulé apparaît alors dans la liste). Son `result` : `{"probed", "failed", "left", "interrupted"}`. Ces routes **modifient l'index et, pour `cleanup` avec `apply`, suppriment des fichiers parasites** de la médiathèque : un client de lecture n'a pas à les appeler.
