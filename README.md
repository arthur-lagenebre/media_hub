# media_hub

[![Tests](https://github.com/arthur-lagenebre/media_hub/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/arthur-lagenebre/media_hub/actions/workflows/tests.yml)
[![Version](https://img.shields.io/github/v/tag/arthur-lagenebre/media_hub?label=version)](https://github.com/arthur-lagenebre/media_hub/tags)
[![Image](https://github.com/arthur-lagenebre/media_hub/actions/workflows/image.yml/badge.svg)](https://github.com/arthur-lagenebre/media_hub/actions/workflows/image.yml)
![Python](https://img.shields.io/badge/python-3.13%20%7C%203.14-blue)
![Dépendances](https://img.shields.io/badge/dépendances-aucune-brightgreen)

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
| Musique | `.flac`, `.mp3`, … regroupés en albums, pochette = `cover.{ext}` du dossier ; une image `1997 - OK Computer.jpg` dans le dossier de l'artiste déclare un album manquant (grisé, sur sa fiche seulement) ; la photo de l'artiste est `artist.webp` dans son dossier (voir [Photos d'artistes](#photos-dartistes)) | barre audio qui continue pendant la navigation |
| Livres | `.cbz` (pages), `.epub` (chapitres) ; `.cbr`, `.mobi`, `.azw3` en téléchargement | liseuse, sens de lecture par série, double page, thèmes |

- **Accueil** : « Reprendre » (films, épisodes, livres, albums en cours) et les ajouts récents de chaque bibliothèque.
- **Bibliothèque** : recherche, genres, tri (titre, ajout, année, note TMDB pour les films et les séries), liste « Statut » (Tous / À voir / En cours / Terminés, accordée selon le genre de la bibliothèque).
- **Lecture adaptée au lecteur** : le navigateur annonce ce qu'il sait lire, le serveur choisit.
  1. *direct* : le fichier tel quel (requêtes Range), aucun calcul sur le NAS ;
  2. *remux* : l'image est recopiée, seul le son (E-AC-3, DTS…) passe en AAC : presque gratuit ;
  3. *transcodage* : l'image est ré-encodée en H.264 (1080p max par défaut) quand le codec n'est pas lisible.
  Si une lecture échoue côté navigateur, le lecteur retombe tout seul sur le mode suivant.
- **Sous-titres** : fichiers `.srt` / `.vtt` / `.ass` à côté de la vidéo et pistes texte du `.mkv`, affichés par le lecteur ; les sous-titres en image des Blu-ray (PGS) sont extraits tels quels et dessinés par le navigateur (VobSub : listés, grisés).
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
| `HUB_INBOX` | dossier de la boîte de réception (les envois des comptes). Il peut être hors de `HUB_DATA`, par exemple le point de montage d'un dataset dédié (« HubReception »). Créé s'il manque ; s'il n'est pas inscriptible, ou s'il recoupe `HUB_DATA` (égal ou parent : `img/` et `posters/` seraient pris pour des dossiers de compte) ou une bibliothèque (égal, parent ou enfant : dans ce cas le hub n'écrit rien pour le vérifier), ou si, donné hors de `HUB_DATA`, ce n'est pas un point de montage (les envois iraient dans le conteneur et seraient perdus au redéploiement ; Linux seulement), le démarrage le dit (`ATTENTION`, sans bloquer). **Se règle ici seulement** : aucun réglage dans l'interface (voir [decisions.md](docs/decisions.md)) | `HUB_DATA/inbox` |
| `HUB_INBOX_KEEP_FREE` | espace libre à garder **sur le volume de la boîte** (`HUB_INBOX`), en Mo : un envoi qui le ferait passer en dessous est refusé (507) | `10240` |
| `HUB_INBOX_MAX_UPLOADS` | envois simultanés (un envoi occupe un fil du serveur) ; au-delà : HTTP 429 | `2` |
| `HUB_AUTOSCAN_HOURS` | rescan périodique (0 = jamais) ; un volume démonté (donc vide) ne fait perdre aucune progression de lecture, et un `.mkv` illisible n'arrête pas le scan | `0` |
| `TMDB_KEY` | enrichit les séries (affiche, synopsis, casting), décrit les films qui manquent aux sagas et ceux qui manquent à une personne ; lue aussi dans le `.env` de `media_library` | aucune |

## Invités d'une piste

Sous le titre d'une piste, dans la fiche d'un album, le hub montre les **invités** : les artistes de la piste qui ne sont pas l'artiste de l'album, séparés par une virgule et sans liaison (« Poppy », « Poppy, Bloodywood »). Il les tire du tag **`ARTISTS`** (un nom par artiste) que `media_library` écrit dans chaque `.flac` et `.mp3`, et compare les artistes par leur identifiant MusicBrainz (`MUSICBRAINZ_ARTISTID` contre `MUSICBRAINZ_ALBUMARTISTID`), à défaut par leur nom. Un fichier sans `ARTISTS` (album pas encore repassé par `media_library`) garde l'ancienne règle sur le tag `ARTIST` (« feat. Poppy »). **Après un déploiement de cette version, lance un scan** (bouton *Mettre à jour* de la bibliothèque *Musique*, ou `POST /api/scan`) : les pistes déjà indexées sont relues une fois pour remplir ces champs. La file d'attente et les playlists montrent toujours le tag `ARTIST` en entier.

## Titre des disques

Dans la fiche d'un album de **plusieurs disques**, l'intertitre d'un disque porte son titre MusicBrainz quand il en a un : « Disque 2 · Bonus ». Le hub le tire du tag **`DISCSUBTITLE`** que `media_library` écrit dans chaque `.flac` et `.mp3`. Sans titre, ou si le titre ne fait que répéter le numéro (« CD 2 »), l'intertitre reste « Disque 2 » ; un album d'un seul disque n'en affiche pas. Si les pistes d'un disque ne portent pas toutes le même titre, le plus répandu l'emporte. **Après un déploiement, lance un scan** (bouton *Mettre à jour* de la bibliothèque *Musique*, ou `POST /api/scan`) : les pistes déjà indexées sont relues une fois.

## Type d'album

La ligne grise sous le titre d'un album se termine par son type de sortie : « 2025 · 10 pistes · 36 min · MP3 · Album ». Le hub le tire du tag **`RELEASETYPE`** (`album`, `single`, `ep`, `compilation`, `live`, `soundtrack`, `remix`...) que `media_library` écrit dans chaque fichier ; plusieurs types se suivent, le principal d'abord (« Album, Live »). Sans ce tag, la ligne reste comme avant. Comme pour les titres de disque, **lance un scan** après un déploiement.

## Photos d'artistes

La photo d'un artiste est le fichier `artist.webp` de **son dossier, à la racine de la bibliothèque** (`Musique/Daft Punk/artist.webp`) : elle suit la musique si on la déplace ou la sauvegarde, et le nettoyage la laisse. Le dossier de l'artiste est celui, à la racine, où il a la majorité des albums ; un album posé directement à la racine, ou un dossier de genre qui range plusieurs artistes, n'en donne pas : la photo reste alors sous `HUB_DATA/artists/`. Le dossier est connu après un scan.

Sur la fiche de l'artiste, l'administrateur choisit une image (*Changer la photo*) : une fenêtre la montre sous un cadre carré, la vignette. On la déplace (glisser, flèches) et on l'agrandit (molette, curseur, `+` / `-`), puis *Valider* **retire tout ce qui est hors du cadre** et enregistre le résultat en WebP (1280 px au plus). *Recadrer* reprend la photo gardée. Aucune photo n'est téléchargée : celle d'un artiste est celle que l'administrateur envoie. La conversion demande Pillow (avec libwebp) ou ffmpeg ; sans eux, une photo est gardée telle quelle sous `HUB_DATA/artists/` et ne se recadre pas. Le volume de la musique doit être monté en écriture (voir [deploy/README.md](deploy/README.md)).

## Sagas

Un film porte la saga que dit son étiquette (la collection TMDB posée par `Metadata.py`). On peut en composer d'autres (une trilogie que TMDB ne connaît pas, l'ordre du MCU) dans `sagas.json`, sous `HUB_DATA` : des listes ordonnées de films, désignés par leur identifiant TMDB (celui de l'étiquette `TMDB` du fichier, qui survit aux renommages et à une base refaite).

```json
{"sagas": [{"name": "Trilogie Cornetto", "films": [747, 4638, 49051]}]}
```

Les films qui **manquent** à une saga (le 2 d'une trilogie dont on n'a que le 1 et le 3) et qui sont **déjà sortis** apparaissent grisés à leur place, avec leur affiche, dans la page de la saga et sur la fiche d'un film ; le film 3 garde son numéro. Ils viennent de TMDB : *Paramètres* > *Gestion* > *Détail par bibliothèque* > *Films* > *Sagas (TMDB)* (avec `TMDB_KEY`) interroge la collection de chaque saga de l'étiquette et décrit les films que ta saga du fichier réclame sans que tu les aies. Ce ne sont pas des éléments : aucun compteur ne les compte. Le même bouton (et *Enrichir (TMDB)* pour les séries) demande aussi la **note TMDB** de chaque film étiqueté : dans le style de TMDB (un disque avec le pourcentage, anneau vert, jaune ou rouge) sur sa vignette et sous le titre de sa fiche, quand au moins 25 personnes ont voté ; elle est redemandée quand elle a plus d'un mois, et un rescan la garde. Un film annoncé, ou sans date de sortie, n'est pas montré : il apparaît le jour de sa sortie, sans rien relancer (les films qu'une saga de `sagas.json` liste elle-même s'affichent toujours). Une collection TMDB trop large (un spin-off, un court-métrage) se règle en cachant le film : *Ne pas afficher* sous sa case grisée dans la page de la saga ; *Paramètres* > *Gestion* > *Sagas* > *Films cachés* le rétablit (`"ignore": [identifiant TMDB]` dans `sagas.json`). Si `sagas.json` devient illisible (une virgule en trop après une édition à la main), le hub n'y écrit plus rien et le dit : corrige-le ou supprime-le.

L'ordre de la liste est l'ordre de la saga ; la place d'un film dans la liste est son numéro, même quand on n'a pas ceux qui le précèdent. Un film peut être dans plusieurs sagas : la grille ne lui donne qu'une vignette (celle de la première saga du fichier, avant celle de l'étiquette), et sa fiche les liste toutes. Le fichier est relu dès qu'il change ; absent ou illisible, il ne donne aucune saga. Pas besoin de l'écrire à la main : l'administrateur compose ses sagas dans l'interface, *Paramètres* > *Gestion* > *Sagas* (créer, renommer, monter ou descendre une saga ou un film, ajouter des films en les cherchant ou toute une saga d'un coup, trier par date de sortie), ou depuis la fiche d'un film avec « Ajouter à une saga ». Le serveur écrit le fichier d'un bloc et refuse ce qu'il ne comprend pas (`/api/sagas`, voir `docs/api.md`). Seuls les films étiquetés avec un identifiant TMDB peuvent y entrer.

## Langue de l'interface

L'interface existe en **français** (par défaut) et en **anglais**. Le choix est dans *Paramètres > Préférences > Interface > Langue*, **par navigateur** (comme la taille du texte) : la page se recharge dans l'autre langue, et la page de connexion suit (elle a aussi un lien *English* / *Français*). Rien ne change pour qui ne touche à rien.

- **Traduit** : tous les textes de l'interface, les messages d'erreur du serveur, les noms des bibliothèques par défaut (*Films* devient *Movies*, *Musique* devient *Music*), les noms des tâches et leur journal, les raisons d'une conversion, les libellés des pistes audio et des sous-titres, le pays et la durée de vie d'un artiste.
- **Pas traduit** : ce que disent les fichiers et les services (titres, synopsis, genres, résumé Wikipédia en français, noms de dossiers comme `Films` ou `Series`), et le nom d'une bibliothèque que l'on a donné soi-même dans `HUB_LIBRARIES`.
- **Où c'est écrit** : le texte français du code sert de clé (`H.t('Paramètres')`) ; l'anglais est dans [hub/web/js/lang-en.js](hub/web/js/lang-en.js), avec les mêmes `{noms}` pour les valeurs. Une clé sans traduction reste en français (en anglais, `Hub.missing` la liste, et un test vérifie qu'il n'y en a pas). Les messages du serveur y sont traduits d'après un modèle (« fichier trop gros (au plus {limit} Mo) ») : un test vérifie que chaque modèle est bien le texte du code Python, donc **quand tu changes un message du serveur, change aussi sa clé dans `lang-en.js`**.
- **Une langue de plus** : un fichier `lang-xx.js` sur le modèle de `lang-en.js` (`H.dict.xx`, `H.srv.xx`), son entrée dans `H.langs` ([i18n.js](hub/web/js/i18n.js)) et `LOGIN_LANGS` ([app.py](hub/server/app.py)).

## Comptes

Chaque compte a sa progression, ses playlists et son « Reprendre » ; les bibliothèques sont communes, sauf celles que l'administrateur ferme à un compte (voir [Droits par bibliothèque](#droits-par-bibliothèque)). Au premier démarrage, `HUB_PASSWORD` crée l'administrateur (`HUB_ADMIN`, « admin » par défaut) et reprend la progression d'avant les comptes ; ensuite on gère les comptes depuis l'interface (administrateur) ou en ligne de commande, serveur arrêté ou non : Les essais de connexion ratés d'une même adresse (pour l'IPv6, d'un même réseau /64) sont ralentis (jusqu'à 5 s), puis plus rien n'est vérifié pendant 5 minutes après 10 échecs (réponse 429) ; un essai réussi remet le compteur à zéro. Derrière un proxy local (`tailscale serve` / Funnel), l'adresse comptée est la dernière entrée de `X-Forwarded-For`, crue seulement d'une connexion venue de la machine elle-même (127.0.0.1, `::1`) ; sur le réseau local, l'en-tête est ignoré. De même, le cookie de session reçoit `Secure` quand le proxy annonce `X-Forwarded-Proto: https` ; en `http://` direct (la télé sur le réseau local) il reste sans. Un mot de passe fait de 12 à 200 caractères à la création et au changement ; la connexion, elle, accepte un mot de passe plus ancien et plus court (ou celui de `HUB_PASSWORD`).

```bash
python -m hub user list
python -m hub user add <nom> [--admin]     # mot de passe demandé (12 caractères au moins) ; le premier compte est toujours administrateur
python -m hub user passwd <nom>
python -m hub user rm <nom>                # supprime aussi sa progression et ses playlists ; jamais le dernier administrateur
```

Seul l'administrateur lance les mises à jour (scan, enrichissement, nettoyage), change les photos d'artistes, gère les comptes et lit la boîte de réception. Sans compte ni `HUB_PASSWORD`, le serveur reste ouvert à tous. Pas d'inscription publique.

### Droits par bibliothèque

Pour chaque compte (sauf l'administrateur, qui a tout) et chaque bibliothèque (Films, Séries, Animations, Musique, Livres…), l'administrateur dit si la personne peut **consulter** (`view`), la **lire** en ligne (« Lecture », `play`) et la **télécharger** (`download`), dans *Paramètres* > *Comptes* > *Droits*, ou en ligne de commande :

```bash
python -m hub user rights <nom>                                       # ce que le compte voit et télécharge
python -m hub user rights <nom> --set Livres=none --set Films=view    # all : voir, lire et télécharger ; play : voir et lire ; view : voir seulement ; none : rien
```

- Par défaut tout est permis : un compte d'avant les droits n'a rien perdu, et une bibliothèque ajoutée plus tard s'ouvre à tous.
- Une bibliothèque qu'on ne voit pas **disparaît** pour ce compte : onglet, accueil, recherche, « Reprendre », fiches, images, lecture. Ses pistes restent dans les playlists du compte mais n'y apparaissent plus ; elles reviennent si le droit est rendu. Télécharger suppose voir.
- **Voir** : la liste, les fiches, les images ; **lire** : la lecture en ligne (vidéo, musique, livres, sous-titres) ; **télécharger** : les fichiers. Lire et télécharger supposent voir, mais ne dépendent pas l'un de l'autre. Sans le droit de lire, la fiche reste visible mais les boutons de lecture disparaissent, la lecture répond 403 et « Reprendre » ne propose plus rien de cette bibliothèque.
- Sans le droit de télécharger, les boutons *Télécharger* disparaissent et `?download=1` répond 403. **Lire envoie le fichier au navigateur** : le droit de télécharger retire le bouton et la pièce jointe, pas la possibilité technique d'enregistrer ce qu'un lecteur reçoit. Pour qu'on ne puisse que voir la fiche, retire aussi « lire ».
- Les droits valent à la requête suivante, sans que la personne se reconnecte. Le jeton de média (`?token=`) d'un client natif est celui de son compte : il porte les mêmes droits.

## Boîte de réception (les envois)

Un compte à qui l'administrateur a donné le droit **Envoyer** (`POST /api/user/<id>/upload`, `{"upload": true}` ; l'administrateur l'a toujours, un compte neuf non) dépose des fichiers sur le NAS : films, séries, musique, livres, sous-titres, archives `.zip`. **Rien n'entre dans une bibliothèque** : les fichiers vont dans la boîte (`HUB_INBOX`, par défaut `HUB_DATA/inbox`), un dossier par compte nommé d'après le **nom** du compte (le hub n'écrit pas dans la médiathèque, ses volumes restent en `:ro`) et l'administrateur les range lui-même. Un fichier déposé n'est jamais analysé (ni ffprobe ni scan) ; seul l'administrateur le télécharge, en pièce jointe.

- **Pas d'envoi sans compte** : un serveur ouvert (sans compte) refuse tout envoi (403), il est visible d'Internet par Funnel.
- **Réception** : `POST /api/inbox?name=<nom>[&dir=<chemin>]`, le fichier en corps brut, `Content-Length` obligatoire (411 sinon ; pas de corps en morceaux). Le corps est lu par morceaux de 1 Mo dans `<nom>.part`, renommé quand tout est arrivé ; un envoi coupé ou trop court n'en laisse rien. Délai de lecture : celui de la connexion (120 s), un envoi bloqué se coupe.
- **Limites**, vérifiées avant de lire le corps (la connexion est alors coupée) : espace libre à garder sur le volume de la boîte (`HUB_INBOX_KEEP_FREE`, 507), envois simultanés (`HUB_INBOX_MAX_UPLOADS`, 429). **Aucune limite de taille**, ni par fichier ni par compte : seul l'espace à garder refuse un envoi, pour que le disque ne se remplisse pas et ne coupe pas la base.
- **Noms** : seul le dernier élément du chemin est gardé (`../x.mkv` donne `x.mkv`), les caractères de contrôle et `<>:"|?*` deviennent `_`, pas de point au début, les noms réservés de Windows sont préfixés, longueur bornée (200 octets). Extensions acceptées : vidéo (`.mkv .mp4 .avi…`), audio (`.flac .mp3 .m4a…`), livres (`.epub .cbz .pdf…`), sous-titres (`.srt .ass .vtt…`) et `.zip` ; tout le reste (exécutables, scripts, pages web) : 415. Un fichier n'en écrase jamais un autre : `Film.mkv` devient `Film (2).mkv`.
- **Dossier d'un compte** : son nom est celui du compte, nettoyé comme un nom de fichier (pas de `/ \ : * ? " < > |`, pas de point ni d'espace au début ou à la fin, pas de nom réservé de Windows ; `compte` si rien ne reste), avec `(2)`, `(3)`… si un autre porte déjà ce nom, **à la casse près** (`Léa` et `léa` sont le même dossier sur un partage SMB). Le lien compte -> dossier est gardé dans la base (`users.inbox_dir`) et fixé au premier envoi : un renommage de compte ou un nom qui se confond avec un autre ne donne jamais un dossier à un autre compte. Au démarrage, les dossiers d'avant (nommés d'après l'identifiant : `default`, ou hexadécimal) sont renommés d'après le compte, sans rien écraser (relancer ne change rien ; un dossier qui résiste garde son ancien nom et le journal le dit).
- **Sous-dossiers** : `dir` range le fichier dans un sous-dossier du dossier du compte (`Série/Saison 1`, 4 niveaux au plus, chaque nom nettoyé comme un nom de dossier de compte). Le hub crée le dossier au moment où un fichier y arrive, jamais vide, jamais en cas de refus ; `Dossier` et `dossier` sont le même dossier (envoyer deux fois un dossier le fusionne, les fichiers de même nom prennent `(2)`) ; au plus 1000 sous-dossiers par compte. Un segment vide, `.`, `..` ou une barre oblique inverse est refusé (400). La liste donne `dir` pour chaque fichier ; `GET`/`DELETE /api/inbox/<dossier>/<nom>?dir=…` lisent et suppriment, et supprimer le dernier fichier d'un sous-dossier le retire. `GET /api/inbox?mine=1` rend aussi `extensions` (les extensions acceptées). Ni lien symbolique ni jonction n'est jamais suivi. Côté interface, la page « Envoyer » envoie un dossier entier (bouton « Envoyer un dossier », ou glisser-déposer) : elle écarte avant le départ les fichiers de type refusé, cachés (`.`, `Thumbs.db`, `desktop.ini`), vides ou trop profonds, demande confirmation, puis envoie les fichiers un à un avec leur `dir` ; une ligne par dossier, dépliable.
- **Compte supprimé** : son dossier et ses fichiers restent dans la boîte ; l'administrateur les voit sous le nom du dossier, marqués « compte supprimé ».
- **Nettoyage** : au démarrage puis chaque jour, les `.part` de plus de 24 h (un envoi abandonné par un arrêt brutal du serveur) sont supprimés.
- **Lecture** : l'administrateur liste (`GET /api/inbox`), télécharge (`GET /api/inbox/<dossier>/<nom>`, le dossier encodé dans l'URL) et supprime (`DELETE` du même) ; un compte qui a le droit ne voit et ne supprime que les siens (`GET /api/inbox?mine=1`).

## Interface HTTP

Le contrat complet pour un client tiers (champs, codes d'erreur, authentification, plan de lecture) est dans [docs/api.md](docs/api.md). Résumé :

| Route | Rôle |
| --- | --- |
| `GET /api/home`, `/api/state`, `/api/search?q=` | accueil (`unseen` de chaque bibliothèque vidéo : 48 éléments au plus que le compte n'a pas touchés, dans l'ordre de son réglage `home_unseen_sort`, appliqué avant la coupe ; l'interface retire ceux de « Nouveautés » puis en garde 24 au plus), bibliothèques, recherche (`results` : les titres ; `people` : 8 personnes au plus dont le nom contient le texte, avec leur nombre de titres) |
| `GET /api/person?name=<nom>` | ce qu'une personne a réalisé ou joué (films et séries des bibliothèques que le compte voit) : `name` (graphie la plus fréquente), `roles` (`director`, `actor`), `results` (par bibliothèque, du plus récent au plus ancien). Nom comparé tel quel, sans accents ni casse (« Chris Evans » ne ramène pas « Chris Evans Jr »), inconnu : `results` vide ; absent ou de plus de 200 caractères : 400. `enrichable` (le nom désigne une seule personne, qui a un identifiant TMDB, et il y a une clé TMDB) et `missing` (ses films, d'après TMDB, qu'aucune bibliothèque visible n'a ; absent s'il est vide). **Homonymes** : l'index se fait par le nom, deux personnes de même nom sont confondues (et ne sont pas enrichissables) |
| `POST /api/enrich/person` | (administrateur) `{"name", "force"?}` : demande à TMDB les films d'une personne (réalisés, ou joués parmi les premiers rôles), gardés un mois ; tâche de fond. 400 sans clé TMDB ou sans identifiant unique pour ce nom, 404 nom inconnu |
| `POST /api/enrich/people` | (administrateur) `{"lib"?, "force"?}` : la même demande à TMDB pour les réalisateurs et les acteurs (réalisateurs et acteurs de premier plan, une seule personne par nom), en tâche de fond ; sans `lib` les bibliothèques de films (bouton « Films manquants (tout le monde) » dans *Paramètres* > *Gestion*), avec `lib` une catégorie : films, séries ou animations (bouton « Films des personnes (TMDB) » de chaque bibliothèque, dans « Détail par bibliothèque »). Pour les séries, les identifiants des acteurs viennent de « Enrichir (TMDB) ». Une personne connue depuis moins d'un mois n'est pas redemandée. 400 sans clé TMDB |
| `GET/POST /api/me/settings` | réglages d'affichage du compte, gardés sur le serveur (corps `{"réglage": "valeur"}`, 400 si inconnu) ; `home_unseen_sort` : `random` (défaut), `rating` (note TMDB montrée ; sans note à la fin) ou `added` (plus récent d'abord), l'ordre du rayon « Pas encore vu » |
| `GET /api/library/<id>?q&genre&sort&filter&offset&limit` | liste d'une bibliothèque (`filter=todo\|progress\|done` : à voir = aucun fichier entamé, en cours, terminés = tous vus ; `unseen`, ancien nom de `todo` ; une autre valeur : 400 « filtre inconnu » ; sur la musique : 400 « ce filtre n'existe pas pour la musique »), avec les genres (les 60 plus fréquents, par ordre alphabétique) ; musique : `artists` et `albums`, le décompte de ce que les filtres laissent (en vue artistes, un artiste trouvé garde tous ses albums) ; `sagas=1` (films) regroupe les sagas, `group=` en liste les films (`sort=part`), `section=` (livres) filtre par section et renvoie les `sections` avec leur compte |
| `GET/POST /api/playlists`, `GET /api/playlist/<id>`, `POST /api/playlist/<id>/add\|remove\|move\|rename\|delete` | playlists musicales (stockées dans la base ; une piste déplacée disparaît sans casser la liste) |
| `GET /api/item/<id>` | fiche : épisodes, pistes, tomes, sagas, casting, progression |
| `POST /api/play/<entry>` | plan de lecture (url, pistes audio, sous-titres, reprise, suivant) |
| `GET /api/play/<entry>/start?t=` | où part vraiment un flux remux repris à `t` (l'image clé), pour compter l'horloge et les sous-titres depuis là |
| `GET /media/<entry>/file`, `/media/<entry>/stream` | fichier (Range, `?download=1`) ; flux MP4 fragmenté (remux / transcodage, `?start=`) |
| `GET /api/subs/<entry>/<e\|s><n>.<vtt\|sup>` | sous-titres en WebVTT, ou piste PGS brute (`.sup`) |
| `POST /api/progress`, `/api/item/<id>/seen`, `/api/entry/<id>/seen` | progression, vu / pas vu |
| `GET /api/book/<id>`, `/book/<id>/page/<n>`, `/book/<id>/epub/<chemin>` | pages d'un `.cbz`, fichiers d'un `.epub` (CSP sans script) |
| `GET /img/item/<id>?w=`, `/img/entry/<id>?w=` | affiches, pochettes, couvertures (réduites et gardées en cache) |
| `POST /api/token` | pour un client natif (Android TV, iOS, Chromecast) : corps `{"user": "…", "password": "…"}` (`user` facultatif s'il n'y a qu'un compte), réponse `{"token": "…", "media_token": "…", "user": {…}}` (401 `{"error": "mot de passe incorrect"}` sinon ; jetons vides si le serveur est ouvert, sans compte). Le jeton est celui du compte, et le jeton de média aussi (il porte ses droits) |
| `POST /api/artist/<bibliothèque>/<id>/links`, `POST /api/artist/<bibliothèque>/<id>/links/remove` | liens de la fiche d'un artiste (administrateur seulement ; 404 s'il n'a pas de fiche) : ajouter `{"url", "label"?}` (adresse http ou https, 2000 caractères au plus, sans doublon ; le libellé par défaut est le nom du site ; aucune requête n'est faite vers elle), retirer `{"url"}` (n'importe lequel, Wikipédia et MusicBrainz compris). Réponse : `{"links": [...]}`, les liens à jour. Les retraits et ajouts sont gardés à part (`artistlinks:<mbid>` dans `kv`) : « Fiches artistes » les respecte. Rajouter une adresse retirée annule le retrait |
| `GET /api/sagas`, `POST /api/sagas`, `POST /api/sagas/add`, `GET /api/sagas/films` | sagas composées à la main (`sagas.json`, administrateur seulement) : lire, tout remplacer (avec la `version` lue, pour ne pas écraser un autre), ajouter des films, cacher un film des manquants (`POST /api/sagas/ignore`), chercher des films à y mettre ; `GET /img/film/<tmdb>` : l'affiche d'un film manquant |
| `GET /api/users`, `POST /api/users`, `POST /api/user/<id>/password\|delete\|rights` | comptes (administrateur seulement) : lister (avec leurs droits par bibliothèque), créer, régler ce qu'un compte voit et télécharge, donner un nouveau mot de passe (à un autre compte ; pour le sien, l'administrateur redonne l'actuel, `current`), supprimer (avec sa progression et ses playlists ; jamais le dernier administrateur) |
| `POST /api/user/<id>/upload` | (administrateur) `{"upload": true}` : donne ou retire le droit d'envoyer dans la boîte de réception (400 pour un administrateur, qui envoie toujours) |
| `POST /api/inbox?name=&dir=`, `GET /api/inbox`, `GET /api/inbox/<dossier>/<nom>?dir=`, `DELETE /api/inbox/<dossier>/<nom>?dir=` | boîte de réception (voir [Boîte de réception](#boîte-de-réception-les-envois)) : envoyer un fichier en corps brut (`Content-Length` obligatoire), lister (`?mine=1` : seulement les siens), télécharger (administrateur) et supprimer (administrateur, ou son propre fichier) |
| `POST /api/me/password` | chacun change son propre mot de passe (`{"current": "…", "new": "…"}`) ; les autres sessions du compte sont fermées |
| `POST /api/me/signout-others` | chacun déconnecte ses autres appareils, sans changer de mot de passe (`{"current": "…"}`) ; la session de l'appelant est renouvelée |
| Authentification (dès qu'un compte existe) | trois façons de se présenter : cookie `hub=<compte>.<mac>` (navigateur, posé par `/api/login`) ; en-tête `Authorization: Bearer <token>` (toutes les routes) ; `?token=<media_token>` dans l'URL, **uniquement** sur `/media/`, `/img/`, `/book/` et `/api/subs/` (balises `<img>` / `<video>` / `<track>`, Cast), nulle part ailleurs sur `/api/`. Le jeton de média est distinct du jeton de session et propre au compte : une URL de pochette qui fuit n'ouvre ni le scan ni le nettoyage, et ne donne que ce que ce compte peut voir. Le jeton est masqué dans le journal. Changer un mot de passe ferme les sessions de ce compte et révoque aussi son jeton de média |
| `POST /api/scan`, `/api/enrich`, `/api/cleanup`, `GET /api/jobs` | (les trois `POST`, comme les photos d'artistes et `GET /api/jobs?log=1` (le journal des tâches), sont réservés à l'administrateur : 403 sinon ; `GET /api/jobs` sans `log` reste ouvert à tout compte) mise à jour de l'index, enrichissement TMDB, nettoyage des fichiers parasites (`Thumbs.db`, `AlbumArt_{…}_Large.jpg` / `_Small.jpg`…, et en musique toute image autre que `cover.{ext}`, que les pochettes d'albums manquants et que `artist.webp`) |

## Limites connues

- **Un envoi très lent occupe son emplacement** : le délai de lecture (120 s) s'applique à chaque lecture, pas à la durée totale. Un compte qui a le droit d'envoyer et n'envoie qu'un peu d'octets toutes les deux minutes garde un des `HUB_INBOX_MAX_UPLOADS` emplacements (et l'espace annoncé de son fichier) tant qu'il tient la connexion ; les autres envois reçoivent alors 429. Retirer son droit (`POST /api/user/<id>/upload`) ne coupe pas l'envoi en cours.
- **Pas de reprise d'un envoi interrompu** dans la boîte de réception (pas de `Content-Range`) : un envoi coupé est à refaire entièrement. Les fichiers déposés ne sont pas rangés par le hub : l'administrateur les déplace à la main dans la médiathèque.
- **Pas d'inscription publique** : seul un administrateur crée les comptes. Chaque compte a sa progression, ses playlists et son « Reprendre » ; les bibliothèques, les fiches enrichies et les albums manquants sont communs. Sans compte ni `HUB_PASSWORD`, le serveur reste ouvert (profil unique `default`).
- **Homonymes** : `GET /api/person` retrouve une personne par son nom ; deux personnes qui s'appellent pareil sont confondues, un même nom écrit de deux façons (« Chris » / « Christopher ») fait deux personnes. Les étiquettes des films portent l'identifiant TMDB de chaque personne : un nom n'est « enrichissable » (films manquants) que s'il n'en a qu'un ; avec deux, aucun. Un film dont les tags n'ont pas encore ces identifiants n'en a pas, tant que `media_library` ne l'a pas réétiqueté. Après la mise à jour qui les lit, le premier scan **relit une fois tous les films**, même inchangés (les séries ne sont pas relues).
- Les films ne sont indexés qu'en **`.mkv`** (c'est ce que `media_library` produit : `Mp4_To_Mkv.py`, `Avi_To_Mkv.py`).
- **Pas de carte vidéo capable d'encoder** (GT 730) : le transcodage est logiciel. Un 1080p H.264 passe sans peine sur un i3-13100F ; un 4K HEVC en transcodage est à éviter (le plan de lecture le signale). Le mieux reste la lecture directe : un Chromecast / Fire TV 4K lit H.264, HEVC et AV1 selon le modèle.
- Google TV n'a pas Chrome : installer un navigateur compatible télécommande (TV Bro, Puffin TV, Silk sur Fire TV). Une appli Android TV native (WebView plein écran) est la suite logique si la télécommande dans un navigateur ne convient pas.
- Les sous-titres VobSub (DVD) ne sont pas affichés. Les PGS (Blu-ray) le sont, mais ne se choisissent jamais d'office, et leur première extraction lit tout le fichier vidéo (plusieurs minutes pour un remux de 30 Go) ; elle est ensuite gardée.

## Développer avec Claude Code

Six agents spécialisés (architecture, serveur, interface, tests, relecture, déploiement) sont dans `.claude/agents/` : voir [docs/agents.md](docs/agents.md). Ce qu'il reste à faire : [docs/TODO.md](docs/TODO.md) ; ce qui a été décidé, dont les refus : [docs/decisions.md](docs/decisions.md) ; ce qui reste à essayer sur place (NAS, téléphone, tablette, télé) : [docs/essais-sur-place.md](docs/essais-sur-place.md). Les pop-up de l'interface (quatre déclinaisons, à utiliser pour tout nouveau) : [docs/popups.md](docs/popups.md).

## Tests

```bash
python -m unittest discover -s tests          # 2188 tests : 1838 serveur + 350 interface
```

`tests/test_ui.py` (et un test de `tests/test_popups.py`) pilote l'interface dans Chromium (Playwright, 350 tests) ; ils sont ignorés si Playwright ou ffmpeg manquent.

### Intégration continue (GitHub Actions)

Les workflows sont dans `.github/workflows/` ; l'environnement commun (media_library à côté, ffmpeg, `.venv`) est dans l'action `.github/actions/setup`. Le badge « Tests » en tête de ce fichier reflète `tests.yml` sur `main`.

| Workflow | Quand | Ce qu'il fait |
| --- | --- | --- |
| `tests.yml` | chaque PR, chaque push sur `main` | le serveur sur Python 3.13 et 3.14 (l'interface dans Chromium n'y passe plus : à lancer à la main) |
| `version.yml` | chaque PR | relit la règle de `/ship` : `__version__`, `API_VERSION`, `SCHEMA_VERSION` (échec si un numéro baisse, avertissement si la version ne suit pas `hub/`) |
| `quality.yml` | PR qui touche du Python de `hub/` | `dryer` (aucune paire de score 0,8 ou plus) et `crapper` (CRAP 10 au plus sur les fichiers modifiés) |
| `mutator.yml` | à la main, ou étiquette `mutator` sur une PR | la passe de mutation, sur les chemins choisis ou les fichiers Python de la PR |
| `image.yml` | étiquette `vX.Y.Z` | construit l'image (`deploy/Dockerfile`), l'essaie, la publie sur `ghcr.io` ; l'étiquette doit être celle de `__version__` |

`dependabot.yml` propose chaque semaine la mise à jour des actions et de l'image de base. Les outils `mutator`, `crapper` et `dryer` sont épinglés à un commit dans les workflows : pour les mettre à jour, change ce commit.
