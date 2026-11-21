# Déployer media_hub sur TrueNAS Scale

Deux conteneurs qui partagent le même réseau :

```text
tailscale  ── HTTPS ──►  https://nas.tail59c227.ts.net   (partout, tout navigateur, sans Tailscale : Funnel)
   │ (même réseau)
media_hub  ── :8080 ──►  http://192.168.0.42:8080         (télé, à la maison)
```

**L'adresse `https://nas…` est ouverte à tout Internet** (Tailscale Funnel) : voir [Sécurité](#sécurité) et [Ouvrir à Internet (Funnel)](#ouvrir-à-internet-funnel) avant de la donner à quelqu'un.

Le conteneur `media_hub` est l'**image publiée** à chaque version par le workflow `Image` : `ghcr.io/arthur-lagenebre/media_hub:<version>` (avec `media_library`, Pillow et un ffmpeg de Debian dedans), à un **numéro précis**, jamais `latest`. Mettre à jour, c'est changer ce numéro : voir [Mettre à jour](#mettre-à-jour) et [Revenir à la version d'avant](#revenir-à-la-version-davant). L'ancienne façon (sources montées dans une image Python nue) reste possible pour le développement ou un dépannage : voir [Variante : sources montées](#variante--sources-montées-dans-python315-slim).

Le `media_hub` remplace l'ancien `updater` (page d'accueil + boutons « Mettre à jour » qui générait des pages HTML) : l'interface a son propre bouton de mise à jour. Tu peux garder le dossier Tailscale (`tailscale/`) tel quel : l'état de connexion est réutilisé, `serve.json` est le même.

## 1. Préparer les dossiers

Sur un dataset d'applications (ici `/mnt/tank/apps/media`, adapte) :

```text
/mnt/tank/apps/media/
  media_hub-data/     vide : base SQLite, cache d'images, boîte de réception (inbox/, voir plus bas)
  ffmpeg/             ffmpeg et ffprobe (étape 2)
  tailscale/          l'état Tailscale (vide au premier lancement)
  tailscale-config/   y copier deploy/serve.json
```

Le code n'est plus copié sur le NAS : `media_hub` et `media_library` sont dans l'image, Pillow aussi. Les dossiers `media_hub/` et `media_library/` (et `media_hub-data/pylib/`) ne servent qu'à la [variante à sources montées](#variante--sources-montées-dans-python315-slim) : sans elle, tu peux les supprimer une fois la bascule vérifiée.

Facultatif : un dataset à part, `/mnt/tank/HubReception`, pour la boîte de réception (voir [Boîte de réception](#boîte-de-réception)) ; il n'est utilisé que si tu actives son montage dans le compose.

Les bibliothèques gardent leur place (`/mnt/tank/media/Films`, `Series`, `Musique`, `Livres`).

### Compte du conteneur

Le conteneur `media_hub` ne tourne **pas en root** : il prend le compte du NAS qui possède la médiathèque, `PUID:PGID` dans le `.env` (`1000:1000` par défaut ; `id <compte>` dans le shell du NAS les donne). Il n'a aucune capacité Linux (`cap_drop: ALL`) ni moyen d'en regagner (`no-new-privileges`) : une faille du serveur ne donnerait que les droits de ce compte.

Ce compte doit pouvoir :

- **écrire** dans `media_hub-data` (base, caches) et dans `HubReception` s'il est monté ;
- **écrire et supprimer** dans chaque bibliothèque montée sans `:ro` : « Supprimer » les fichiers parasites, et les photos d'artistes (`artist.webp`) dans la Musique ;
- **lire et exécuter** `ffmpeg/`.

Avec les datasets TrueNAS en ACL NFSv4 dont l'entrée `everyone@` (ou une entrée pour ce compte) donne *Modify* avec héritage, c'est déjà le cas, quel que soit le propriétaire des fichiers. Vérifie dans le shell du NAS :

```bash
id <compte>
nfs4xdr_getfacl /mnt/tank/media/Musique | head -12
nfs4xdr_getfacl /mnt/tank/apps/media/media_hub-data | head -12
```

**En passant d'un conteneur root à celui-ci** (une fois, application arrêtée) : ce que l'ancien conteneur a créé appartient à root. Donne-le au compte, puis vérifie que les trois comptes affichent `0` :

```bash
chown -R 1000:1000 /mnt/tank/apps/media/media_hub-data /mnt/tank/HubReception
find /mnt/tank/media/Musique -mindepth 1 -user root -exec chown 1000:1000 {} +     # les artist.webp écrits par le conteneur root

find /mnt/tank/apps/media/media_hub-data ! -user 1000 | wc -l
find /mnt/tank/HubReception ! -user 1000 | wc -l
find /mnt/tank/media/Musique -mindepth 1 -user root | wc -l
```

Après le redéploiement : `docker exec media-hub id` affiche `uid=1000 gid=1000`, `docker logs media-hub 2>&1 | grep ATTENTION` n'affiche rien, puis essaie un scan, « Supprimer » les fichiers parasites, une photo d'artiste, un envoi et une vidéo transcodée. Une erreur de droits (« Permission denied » dans `docker logs media-hub`) désigne le dossier que le compte ne peut pas écrire : corrige son ACL ou son propriétaire, ne remets pas le conteneur en root.

## 2. ffmpeg

Dans le shell du NAS :

```bash
curl -fsSL -o /tmp/fetch-ffmpeg.sh https://raw.githubusercontent.com/arthur-lagenebre/media_hub/v0.25.0/deploy/fetch-ffmpeg.sh   # le dépôt est public ; ou copie deploy/fetch-ffmpeg.sh depuis ton poste
sh /tmp/fetch-ffmpeg.sh /mnt/tank/apps/media/ffmpeg
```

Si `ffmpeg/` contient déjà `ffmpeg` et `ffprobe`, rien à refaire : la bascule vers l'image ne le touche pas.

L'image contient aussi le ffmpeg de Debian, mais le compose garde le ffmpeg figé : `HUB_FFMPEG: /opt/ffmpeg` l'emporte sur celui du `PATH` (voir `docs/decisions.md`). Attention : si `/opt/ffmpeg` ne contient pas l'outil (dossier vide, mauvais montage), le serveur se rabat **sans le dire** sur le ffmpeg de Debian ; `docker exec media-hub /opt/ffmpeg/ffmpeg -version` doit afficher `7.0.2`.

Le script télécharge une version fixe (7.0.2) et refuse l'archive si sa somme SHA-256 n'est pas celle qu'il attend. Pour une autre version, changer `VERSION` et `SHA256` ensemble dans `fetch-ffmpeg.sh`.

Sans ffmpeg, le serveur ne sait que lire directement (fichiers compatibles) ; il l'écrit au démarrage. Le remux du son (E-AC-3, DTS), le transcodage et les sous-titres du `.mkv` en ont besoin.

## 3. Installer l'application

1. Tailscale > Settings > Keys : crée une clé d'authentification (inutile si `tailscale/` contient déjà un état connecté).
2. Copie `.env.example` en `.env`, à côté de `docker-compose.yml`, et remplis `TS_AUTHKEY`, `HUB_ADMIN` (nom du premier compte, `admin` par défaut), `HUB_PASSWORD` (son mot de passe, **obligatoire** : sans lui `docker compose up` refuse de démarrer, plutôt que de servir une médiathèque ouverte à tout le réseau local ; il ne sert qu'à créer ce premier compte), `TMDB_KEY` (facultative), `APPS`, `MEDIA`.
3. TrueNAS : Apps > Discover Apps > Custom App > *Install via YAML* : colle `docker-compose.yml` (en remplaçant les `${...}` par leurs valeurs si l'éditeur ne lit pas le `.env`). TrueNAS télécharge l'image `ghcr.io/arthur-lagenebre/media_hub:<version>` du compose (voir [Si l'image ne se télécharge pas](#si-limage-ne-se-télécharge-pas)) : rien d'autre à installer, ni code, ni Pillow.
4. Au premier démarrage, le serveur indexe les bibliothèques (quelques minutes pour des milliers de fichiers) ; l'avancement est dans le journal du conteneur et dans les *Tâches* de *Paramètres* > *Gestion* (en bas de la page).

Vérification : `http://192.168.0.42:8080` demande le nom et le mot de passe (`HUB_ADMIN` / `HUB_PASSWORD`), puis affiche les bibliothèques.

### Passer des sources montées à l'image (une fois)

Pour une installation qui montait les sources dans `python:3.13-slim` (mises à jour par copie de `hub/` puis redémarrage) :

1. **Garde une copie de l'ancien YAML** : TrueNAS > Applications > l'app > *Edit*, copie tout le texte dans un fichier sur ton poste. C'est ton retour arrière.
2. Note la version qui tourne (*Paramètres* > *À propos*).
3. Remplace le service `media_hub` par celui de `deploy/docker-compose.yml` : `image: ghcr.io/arthur-lagenebre/media_hub:0.25.0` ; retire `working_dir`, `command`, `PYTHONPATH`, `PYTHONUNBUFFERED`, `HUB_REPO` et les deux montages `${APPS}/media_hub` et `${APPS}/media_library`. **Garde** le montage `${APPS}/ffmpeg:/opt/ffmpeg:ro` et `HUB_FFMPEG`, les autres `HUB_*`, `TMDB_KEY`, `/data` et les bibliothèques. Le service `tailscale` ne change pas.
4. *Save* : TrueNAS télécharge l'image et recrée le conteneur.
5. Fais les vérifications de [Mettre à jour](#mettre-à-jour).

**Les données ne bougent pas** : `media_hub-data` est monté au même endroit, la base, les comptes, la progression et la boîte de réception sont gardés. Si quelque chose ne va pas, recolle l'ancien YAML et enregistre. Une fois la bascule vérifiée, `media_hub/`, `media_library/` et `media_hub-data/pylib/` ne servent plus (garde-les tant que tu veux pouvoir revenir aux sources montées).

### Ce qui change avec l'image

- **Python 3.15** dans l'image (`python:3.15-slim`), au lieu de 3.13.
- **`media_library` est figé dans l'image**, à la version de sa branche `main` au moment où l'étiquette du hub a été posée. Une modification de `media_library` n'atteint le hub qu'avec une **nouvelle version du hub** (et ses scripts lancés par `docker exec … /app/media_library/…` sont ceux de l'image).
- **Pillow est inclus** : plus d'installation au premier démarrage ni de dossier `pylib/`.
- Le code n'est plus sur le NAS : on ne corrige plus un fichier à chaud, une correction passe par une nouvelle version.

## Mettre à jour

1. Trouve le numéro : les étiquettes `vX.Y.Z` du dépôt (GitHub > *Tags*, ou *Packages* > `media_hub`), posées par `/release`, qui publie l'image `ghcr.io/arthur-lagenebre/media_hub:X.Y.Z` (sans le `v`). Attends que le workflow `Image` de l'étiquette soit vert.
2. TrueNAS > Applications > l'app > *Edit* : dans le YAML, remplace le numéro de `image: ghcr.io/arthur-lagenebre/media_hub:<ancien>` par le nouveau, **jamais `latest`** (on ne saurait plus ce qui tourne ni à quoi revenir). Note l'ancien numéro.
3. *Save* : TrueNAS télécharge l'image et redémarre le conteneur. Rien à copier, rien d'autre à redémarrer.
4. Vérifie :
   - *Paramètres* > *À propos* affiche la nouvelle version ;
   - une lecture démarre (un film, et si possible un fichier qui passe en remux ou en transcodage, pour ffmpeg) ;
   - l'accueil s'affiche avec ses rayons.
5. Lis [Après une mise à jour du hub](#après-une-mise-à-jour-du-hub) : certaines versions demandent un scan.

## Revenir à la version d'avant

Remets l'ancien numéro dans le YAML de l'app (*Edit*), puis *Save*. Les données restent ; mais une version récente a pu modifier la base ou la boîte de réception d'une façon que l'ancienne ne connaît pas : lis d'abord [Retour arrière (version d'avant)](#retour-arrière-version-davant) de la boîte de réception. Pour revenir d'avant la bascule (sources montées), recolle l'ancien YAML gardé.

### Si l'image ne se télécharge pas

TrueNAS doit pouvoir télécharger l'image sans s'identifier : le **paquet ghcr.io doit être public**. Sur GitHub : profil > *Packages* > `media_hub` > *Package settings* > *Change visibility* > *Public*. Le dépôt public ne suffit pas toujours : un paquet publié par un workflow peut naître privé. Sinon TrueNAS devrait s'identifier à ghcr.io (un jeton GitHub enregistré sur le NAS), ce qu'on veut éviter. Vérifie depuis le shell du NAS : `docker pull ghcr.io/arthur-lagenebre/media_hub:0.25.0` (« unauthorized » ou « denied » : paquet privé ; « manifest unknown » : numéro inexistant, ou workflow `Image` pas encore fini).

## Comptes

Chaque personne a son compte (progression, playlists, « Reprendre » à elle) ; les bibliothèques sont communes. `HUB_ADMIN` et `HUB_PASSWORD` ne servent qu'**une fois** : au démarrage, si la base n'a aucun compte, ils créent le premier (administrateur, identifiant `default`). Ensuite ils sont ignorés : changer `HUB_PASSWORD` dans le `.env` ne change plus le mot de passe (utilise `passwd` ci-dessous).

**Mise à jour depuis une version sans comptes.** Passe à une version avec comptes (voir [Mettre à jour](#mettre-à-jour)), ajoute `HUB_ADMIN` au `.env` (et à `docker-compose.yml` si tu le colles à la main), puis redémarre l'application. Le premier compte créé reprend la progression et les playlists existantes, rien n'est perdu. Chaque appareil (navigateur, télé, clients natifs) doit se reconnecter **une fois** avec le nom et le mot de passe ; les anciens jetons ne valent plus.

**Ajouter ou gérer des comptes.** Dans l'interface (*Paramètres* > *Comptes*, en administrateur), ou dans le shell du NAS (le conteneur s'appelle `media-hub` ; `-it` est nécessaire pour saisir le mot de passe sans l'afficher) :

```bash
docker exec -it media-hub python -m hub user list
docker exec -it media-hub python -m hub user add alice            # mot de passe demandé, 12 caractères au moins
docker exec -it media-hub python -m hub user add bob --admin      # un deuxième administrateur
docker exec -it media-hub python -m hub user passwd alice
docker exec -it media-hub python -m hub user rm alice             # supprime aussi sa progression et ses playlists
docker exec -it media-hub python -m hub user rights alice                                  # ce qu'alice voit et télécharge, par bibliothèque
docker exec -it media-hub python -m hub user rights alice --set Livres=none --set Films=view   # all : voir, lire et télécharger ; play : voir et lire ; view : voir seulement ; none : rien
```

**Droits.** Par défaut un compte voit et télécharge toutes les bibliothèques ; l'administrateur peut en fermer une à un compte, ou en retirer seulement la lecture ou le téléchargement (*Comptes* > *Droits*, ou `user rights`). Les droits valent tout de suite, sans reconnexion. Un client natif (Android TV, Chromecast) qui gardait un jeton de média d'avant les droits doit le redemander par `POST /api/token` : ce jeton est maintenant celui du compte.

Depuis le shell web de TrueNAS (Apps > media-hub > Shell), lance seulement `python -m hub user …`. Le serveur peut tourner pendant ce temps. Il n'y a pas d'inscription publique.

**Sauvegarde.** Les comptes, la progression, les playlists et le reste de ce qui ne se refait pas sont dans `media_hub-data` : voir [Sauvegarder et restaurer](#sauvegarder-et-restaurer).

**Hachage des mots de passe.** Le serveur utilise `scrypt` si l'OpenSSL de Python le fournit (c'est le cas de l'image du hub, bâtie sur `python:3.15-slim`, comme de la variante à sources montées). Vérifie-le :

```bash
docker exec media-hub python -c "import hashlib; print(hasattr(hashlib,'scrypt'))"
```

`True` : rien à faire. `False` : repli sur `pbkdf2` (600 000 tours), correct mais plus lent, environ 0,3 s à chaque connexion.

## Sauvegarder et restaurer

Une sauvegarde du hub est une archive `.zip` de ce qui ne se refait pas :

| Dans l'archive | Contenu |
| --- | --- |
| `hub.db` | comptes (hachés des mots de passe), clé des sessions, droits, progressions, avis, playlists, demandes, suggestions, réglages des comptes, résultats de l'enrichissement |
| `sagas.json` | les sagas composées à la main |
| `posters/` | les affiches choisies à la main |
| `artists/` | les photos d'artistes gardées hors de la Musique |
| `people/` | les photos de personnes envoyées par l'administrateur (celles de TMDB, sous `tmdb/`, se refont) |
| `manifest.json` | format, version du hub, version de la base, taille et empreinte de chaque fichier |

Ni médias, ni caches (`img/`, `subs/`, `tmdb/`, `tmdb-cache/`, `musicbrainz-cache/` : ils se refont ; les affiches TMDB demandent plus d'une heure), ni boîte de réception (fichiers parfois de plusieurs Go : instantanés de leur dataset, voir [Sauvegarde et instantanés](#sauvegarde-et-instantanés)). Les photos `artist.webp` rangées dans la Musique suivent la Musique.

**L'archive n'est pas chiffrée** : elle contient les hachés des mots de passe et la clé des sessions (une restauration garde les appareils connectés). Garde-la comme un mot de passe : pas dans un partage ouvert, pas en pièce jointe d'un courriel.

### Sauvegarder

Serveur allumé, au choix :

- Depuis le navigateur, en administrateur : *Administration* > *Serveur* > « Télécharger une sauvegarde » (ou `GET /api/backup`) : l'archive arrive dans le navigateur, rien ne reste sur le NAS.
- Dans le shell du NAS : `docker exec media-hub python -m hub backup`. L'archive va dans `media_hub-data/sauvegardes/media_hub-<version>-<date>.zip` (ou à l'endroit donné : `python -m hub backup /data/ma-sauvegarde.zip`). Pour une sauvegarde régulière : TrueNAS > System > Advanced > *Cron Jobs* > *Add*, commande `docker exec media-hub python -m hub backup`, utilisateur `root`, par exemple chaque nuit ; supprime de temps en temps les plus anciennes.

La base est copiée par l'API de sauvegarde de SQLite : la copie est cohérente même si le serveur écrit pendant ce temps. Un instantané ZFS de `media_hub-data` reste une bonne sauvegarde de plus.

### Restaurer

**Serveur arrêté.** Tant que le serveur tourne, il garde un verrou sur `media_hub-data/hub.lock` : la restauration le voit et refuse (« le serveur tourne sur ces données »), sans rien modifier. Le système libère ce verrou dès que le conteneur s'arrête, même brutalement ; le fichier `hub.lock`, vide, reste et ne gêne rien. La restauration vérifie toute l'archive avant de toucher à quoi que ce soit, et refuse sans rien modifier une archive abîmée, d'un autre format, ou faite par un hub plus récent (mets d'abord le hub à la même version ou plus récente). Une archive d'un hub plus ancien est restaurée, puis la base est mise à jour au démarrage. Sur une installation qui a déjà une base, il faut `--replace` : l'installation actuelle (base, sagas, affiches, photos) est mise de côté dans `media_hub-data/avant-restauration-<date>/`, rien n'est effacé.

1. TrueNAS > Apps > l'application > *Stop*.
2. Copie l'archive sur le NAS, puis lance un conteneur temporaire de la même image, avec le dossier de données et l'archive :

   ```bash
   docker run --rm --user 1000:1000      -v /mnt/tank/apps/media/media_hub-data:/data      -v /mnt/tank/apps/media/sauvegarde.zip:/sauvegarde.zip:ro      ghcr.io/arthur-lagenebre/media_hub:<version> python -m hub restore /sauvegarde.zip --replace
   ```

   (`--user` : le même compte que le compose, voir [Compte du conteneur](#compte-du-conteneur).) Sur une installation neuve, sans base, `--replace` est inutile.
3. *Start* l'application, connecte-toi, vérifie comptes et progression. Supprime ensuite `avant-restauration-<date>/`.

**À la main, sans l'outil** (dépannage) : application arrêtée, décompresse l'archive, mets `hub.db`, `sagas.json`, `posters/`, `artists/` et `people/` dans `media_hub-data` (en gardant les anciens de côté), **supprime `hub.db-wal` et `hub.db-shm`** s'ils existent (ils appartiennent à l'ancienne base), donne les fichiers au compte du conteneur (`chown -R 1000:1000`), puis redémarre.

## 4. Sur la télé

- **Fire TV** : navigateur Silk ou Firefox. **Google TV / Android TV** : TV Bro ou Puffin TV (Chrome n'est pas proposé).
- Ouvre `http://192.168.0.42:8080` à la maison (pas de HTTPS nécessaire) ; ailleurs, `https://nas.tail59c227.ts.net` s'ouvre dans n'importe quel navigateur, sans Tailscale sur la télé ni sur le téléphone (Funnel), et demande le nom et le mot de passe du compte.
- Flèches pour naviguer, OK pour ouvrir, Retour pour revenir. Ajoute la page en favori / écran d'accueil du navigateur.

## 5. Au quotidien

| Besoin | Où |
| --- | --- |
| Nouveaux fichiers visibles | *Paramètres* > *Gestion* > Tout mettre à jour, ou *Détail par bibliothèque* > Mettre à jour (ou `HUB_AUTOSCAN_HOURS`) |
| Affiches / synopsis des séries | *Paramètres* > *Gestion* > *Détail par bibliothèque* > Enrichir (TMDB) (avec `TMDB_KEY`) |
| Supprimer les `Thumbs.db` | *Paramètres* > *Gestion* > Fichiers parasites > Supprimer ; retirer `:ro` des volumes concernés. La fenêtre qui s'ouvre compte les fichiers (bouton *Compter*) et liste les 20 premiers avant de supprimer. **En musique, toute image autre que `cover.{ext}` est effacée, `folder.jpg` compris** : compte-les d'abord (`find /mnt/tank/media/Musique -iname 'folder.*' \| wc -l`) |
| Étiqueter / renommer / convertir | scripts de `media_library`, depuis VS Code, ou dans le conteneur (volumes sans `:ro`) : `docker exec -it media-hub python /app/media_library/scripts/Video/Movies/Metadata.py --dir /srv/Films` |
| Les fiches HTML | retirées de `media_library` ; à régénérer ponctuellement avec un prompt dans VS Code si besoin |

## Après une mise à jour du hub

Lance un scan (*Paramètres* > *Gestion* > Tout mettre à jour) quand une version ajoute une information lue dans les fichiers : les éléments déjà indexés sont relus une fois, sans migration de base. Tant qu'il n'est pas fait, les fiches se comportent comme avant.

- **Musique** : les artistes de chaque piste (tag `ARTISTS`, pour les invités), le titre des disques (`DISCSUBTITLE`) et le type d'album (`RELEASETYPE`). Ces tags sont écrits par `Metadata.py --apply` de `media_library` : s'il n'est pas passé sur la musique, les intertitres restent « Disque N » et le type d'album n'apparaît pas.
- **Films** : les identifiants TMDB des personnes (réalisateurs, scénaristes, acteurs) pour la page d'une personne et « Chercher ses films manquants ». Le premier scan après cette mise à jour **relit tous les films**, même inchangés (quelques minutes sur le NAS pour des centaines de films ; les séries ne sont pas relues) ; les suivants reprennent le résultat. Ces identifiants sont écrits par `media_library` : un film qu'elle n'a pas encore repassé n'en a pas.
- **Films manquants des réalisateurs et acteurs de films** : *Paramètres* > *Gestion* > *Bibliothèques* > « Détail par bibliothèque » > « Films manquants » de chaque bibliothèque de films (avec `TMDB_KEY` ; une bibliothèque à la fois : avec plusieurs bibliothèques de films, lance chacune) demande à TMDB les films de chaque réalisateur et acteur de premier plan : un appel par personne, **une heure ou plus la première fois** pour quelques centaines de films (un appel par personne, puis les affiches des films qu'on n'a pas : plusieurs centaines de Mo sous `HUB_DATA/tmdb`, et les réponses de TMDB gardées 7 jours sous `HUB_DATA/tmdb-cache`), en tâche de fond. Si TMDB ne répond plus, la tâche s'arrête après quelques échecs de suite : relance-la. À lancer après le scan qui lit les identifiants ; une personne déjà faite depuis moins d'un mois n'est pas redemandée (sauf si elle l'a été avant la 0.51.0 : voir ci-dessous), et une passe interrompue reprend où elle en était. Le résultat se lit sur la page de chaque personne.
- **Fiche d'une personne** (0.51.0) : la page d'une personne commence par sa fiche (photo, biographie, dates), remplie par la même passe « Films manquants », sans appel TMDB de plus par personne. Relance-la une fois sur chaque bibliothèque de films : les personnes faites avant la 0.51.0 n'ont pas de fiche, et **toutes sont redemandées une fois**, même celles faites depuis moins d'un mois (plus long que d'habitude). Chaque personne ajoute sa photo sous `HUB_DATA/tmdb` (`person-<id>.jpg`), à côté des affiches. Sans cette passe, une fiche se complète quand on clique « Chercher ses films manquants » sur la page de la personne.
- **Séries et animations, films de leurs acteurs** : relance une fois *Gestion* > *Détail par bibliothèque* > « Enrichir (TMDB) » sur Séries : l'enrichissement garde désormais l'identifiant TMDB de chaque acteur du casting, et les séries déjà enrichies sont refaites une fois pour cela (un peu plus long que d'habitude). Ensuite « Films des personnes (TMDB) » sur la ligne de la bibliothèque cherche les films de ses acteurs ; une passe par catégorie, une heure ou plus chacune la première fois.
- **Photos d'artistes** (après le tout premier déploiement) : le dossier de chaque artiste n'est connu qu'après un scan.

## Sécurité

- `HUB_BIND=0.0.0.0` expose le serveur sur le réseau local : `HUB_PASSWORD` est obligatoire dans le compose (sans compte, le serveur est ouvert à tous).
- **Tailscale Funnel est activé** (`AllowFunnel` dans `serve.json`) : le hub est **visible de tout Internet**, à l'adresse `https://nas.tail59c227.ts.net`. Rien d'autre n'est publié : Funnel n'ouvre que le port 443 du `media_hub`, et le port 8080 de la télé reste sur le réseau local. Il n'y a **pas de double authentification** : la défense se limite au compte et à son mot de passe, avec `scrypt` pour le hachage, un ralentissement croissant à chaque échec, un blocage (429) après 10 échecs en 5 minutes, compté par adresse réelle du client, et des mots de passe de 12 caractères au moins (à la création et au changement). Un mot de passe faible ou réutilisé est donc la seule chose qui sépare un inconnu de la médiathèque : voir la procédure ci-dessous.
- Le débit d'un Funnel est limité par Tailscale (limites non configurables, sans chiffre publié) : un film lu depuis l'extérieur peut saccader ; à la maison, utilise l'adresse locale.
- Le conteneur `media_hub` tourne sous un compte ordinaire du NAS (`PUID:PGID`), sans aucune capacité Linux ni moyen d'en regagner (`no-new-privileges`) : voir [Compte du conteneur](#compte-du-conteneur). Une erreur de droits se corrige sur le dataset (ACL, propriétaire), jamais en remettant le conteneur en root.
- Les bibliothèques sont montées en lecture seule par défaut ; le serveur n'écrit que dans `media_hub-data` (et dans la boîte `HUB_INBOX`, si tu la montes à part). Un compte qui a le droit « Envoyer » peut y écrire depuis Internet (boîte de réception, plafonnée par l'espace libre à garder et par une limite de dataset) : ne donne ce droit qu'à des personnes de confiance.
- `.env` contient des secrets : ne le publie pas.

## Ouvrir à Internet (Funnel)

**Avant l'ouverture** (sur le NAS) :

1. Mot de passe de **chaque** compte existant à 12 caractères au moins (un compte plus ancien garde son mot de passe plus court tant qu'on ne le change pas) : `docker exec -it media-hub python -m hub user list`, puis pour chaque nom `docker exec -it media-hub python -m hub user passwd <nom>`. Un mot de passe qu'on utilise ailleurs ne convient pas.
2. Ne garde pas `admin` comme nom du premier compte : c'est le premier nom qu'un inconnu essaie. `HUB_ADMIN` ne vaut qu'au premier démarrage (base sans compte), le changer dans le `.env` ne renomme rien. Pour un compte déjà créé, crée un autre administrateur et supprime l'ancien :

   ```bash
   docker exec -it media-hub python -m hub user add arthur --admin    # un nom qui n'est pas « admin »
   docker exec -it media-hub python -m hub user rm admin              # supprime aussi sa progression et ses playlists
   ```

   Connecte-toi d'abord avec le nouveau compte pour t'assurer qu'il marche, et note que la progression et les playlists de `admin` disparaissent avec lui.

**Ouvrir** :

1. Console Tailscale > *Access controls* : la politique du tailnet doit contenir l'attribut `funnel` pour le nœud `nas` (cherche `nodeAttrs`). Le bouton *Add Funnel to policy* de la console d'administration ajoute l'entrée par défaut, pour tous les membres (`"target": ["autogroup:member"], "attr": ["funnel"]`) ; pour le limiter à ce nœud, mets un tag sur `nas` et ce tag comme `target`. HTTPS et MagicDNS doivent déjà être activés sur le tailnet (c'est le cas si `https://nas…` marche déjà).
2. **Remplace** le fichier `/mnt/tank/apps/media/tailscale-config/serve.json` par le nouveau `deploy/serve.json` (n'y colle pas le nouveau contenu à la suite de l'ancien : deux objets JSON de suite ne forment pas un JSON valide et `tailscale` refuse de démarrer). Vérifie-le : `python3 -m json.tool /mnt/tank/apps/media/tailscale-config/serve.json` doit afficher le JSON sans erreur. Puis redéploie (redémarre l'application entière) pour que `tailscale` relise le fichier.
3. Depuis un téléphone en 4G (Wi-Fi coupé, sans Tailscale), ouvre `https://nas.tail59c227.ts.net` : la page de connexion s'affiche. Un délai de quelques minutes est possible après la première ouverture (DNS public, certificat).
4. Vérifie que les échecs de connexion sont comptés **par la vraie adresse du client**, et pas par `127.0.0.1` (sinon un seul robot bloquerait tout le monde). Le journal ne garde que les erreurs et n'écrit pas les échecs de connexion : le test se fait par le comportement. Depuis le téléphone en 4G, rate dix connexions de suite avec un faux mot de passe : la page répond alors « trop d'essais ». Pendant ce temps, connecte-toi normalement depuis un autre appareil (ordinateur à la maison, par `http://192.168.0.42:8080` ou par l'adresse `https://nas…` depuis un autre réseau) : cela doit passer. Si cet autre appareil est bloqué aussi, le serveur ne voit pas l'adresse réelle : coupe le Funnel (ci-dessous) et regarde `docker logs media-hub` et `docker logs media-hub-tailscale`. Le blocage s'efface seul après 5 minutes, ou au redémarrage du conteneur `media-hub`.
5. Vérifie que le cookie de session est `Secure` en HTTPS : connecté à `https://nas…`, ouvre les outils du navigateur (*Application* > *Cookies*, ou *Stockage* dans Firefox) et regarde le cookie `hub` : la case `Secure` doit être cochée. Sinon `tailscale serve` n'envoie pas `X-Forwarded-Proto: https` ou le serveur ne le voit pas : coupe le Funnel et ne donne l'adresse à personne avant d'avoir trouvé pourquoi. Le cookie perd `Secure` sans aucun message d'erreur, rien d'autre ne le signale. Sur `http://192.168.0.42:8080` (la télé), le cookie n'a pas `Secure`, c'est voulu.
6. Regarde de temps en temps `docker logs media-hub` : une avalanche d'erreurs de connexion ou de requêtes inconnues est le signe d'une attaque.

**Couper en urgence** :

- Retirer `AllowFunnel` de `serve.json`, puis redémarrer l'application : c'est la coupure durable, car `serve.json` est relu à chaque démarrage et remettrait le Funnel.
- Tout de suite, sans redéployer : `docker exec media-hub-tailscale tailscale funnel reset` (coupe le Funnel du nœud ; il revient au prochain démarrage tant que `AllowFunnel` est dans `serve.json`). Cette commande n'a pas pu être vérifiée dans la documentation de Tailscale pour cette version : si elle échoue, retire `AllowFunnel` et redémarre, ou retire l'attribut `funnel` de la politique du tailnet (le Funnel cesse aussitôt).
- Si un compte est en cause : `docker exec -it media-hub python -m hub user passwd <nom>` (ses appareils sont déconnectés) ou `user rm <nom>`.

## Boîte de réception

Un compte à qui l'administrateur a donné le droit **Envoyer** (*Paramètres* > *Comptes*) dépose des fichiers par l'adresse Funnel, comme par l'adresse locale. **Rien n'entre dans une bibliothèque** : les volumes restent en `:ro`. Par défaut les fichiers vont dans le dossier de données, que le conteneur monte déjà en lecture et écriture ; tu peux aussi les mettre sur leur propre dataset (voir plus bas, **désactivé par défaut**). C'est à toi de les ranger. Le fonctionnement du côté serveur (noms, extensions acceptées, codes d'erreur) est dans le [README principal](../README.md#boîte-de-réception-les-envois).

### Où sont les fichiers

```text
/mnt/tank/apps/media/media_hub-data/inbox/<nom du compte>/Film.mkv     par défaut
/mnt/tank/HubReception/<nom du compte>/Film.mkv                        avec le dataset « HubReception » (HUB_INBOX=/inbox)
```

Le dossier porte le **nom du compte** (nettoyé : pas de `/ \ : * ? " < > |`, pas de point ni d'espace en bout, et `Léa (2)` si `léa` existe déjà, à la casse près). Le lien compte -> dossier est gardé dans la base (`hub.db`), fixé au premier envoi : renommer ou supprimer un compte ne déplace rien. Un compte supprimé laisse son dossier et ses fichiers ; l'onglet de la boîte de réception (*Paramètres* > *Gestion*) les montre sous le nom du dossier, marqués « compte supprimé ».

### Mise à jour depuis une version d'avant (migration automatique)

Avant, le dossier d'un compte portait son identifiant (`default` pour le premier compte, de l'hexadécimal pour les autres). **Au premier démarrage avec la nouvelle version**, le hub renomme chaque `inbox/<identifiant>/` d'après le nom du compte (`default` devient `admin`, par exemple), dans la boîte active :

- **Rien n'est écrasé ni perdu** : si le nom est déjà pris (un dossier posé à la main, ou un autre compte), le dossier devient `Nom (2)`. Les `.part` en cours partent avec leur dossier.
- **Relancer ne change rien.** Un dossier dont le compte n'existe plus reste tel quel (« compte supprimé »).
- **Aucune ligne dans le journal quand tout va bien** : vérifie avec `ls /mnt/tank/apps/media/media_hub-data/inbox/`. Un échec est journalisé et n'empêche pas le démarrage : `docker logs media-hub 2>&1 | grep "boîte de réception"` donne `le dossier du compte « Léa » garde son ancien nom (…)`. L'ancien dossier reste en place, sous son ancien nom, et sera repris au démarrage suivant.
- **Dossier ouvert par un partage SMB** : ferme les fenêtres du partage avant de mettre à jour. Sous Windows, un dossier ouvert ne se renomme pas (le renommage échoue, l'ancien nom reste, repris au démarrage suivant) ; sur le NAS (Linux), Samba ne devrait pas bloquer le renommage, mais la fenêtre ouverte chez le client affichera un dossier qui n'existe plus : je n'ai pas pu le vérifier sur place.

### Mettre la boîte sur son propre dataset (`HUB_INBOX`)

Le dataset « HubReception » a son quota, ses instantanés et son partage SMB, sans `hub.db` à côté. **C'est facultatif et désactivé par défaut** : tant que tu n'as pas fait ce qui suit, la boîte reste sous `media_hub-data/inbox`. Le montage et `HUB_INBOX` sont **commentés** dans `docker-compose.yml`, et **les deux lignes s'activent ensemble** : un montage vers un dossier qui n'existe pas empêche le conteneur de démarrer, et `HUB_INBOX=/inbox` sans montage rangerait les envois dans le conteneur lui-même (perdus à son prochain redéploiement ; le démarrage affiche alors « ATTENTION : … n'est pas un point de montage », sans bloquer).

1. **Créer le dataset** : TrueNAS > Datasets > sélectionne `tank` > *Add Dataset* ; *Name* : `HubReception` (soit `/mnt/tank/HubReception`) ; *Dataset Preset* : `SMB` (ACL de type NFSv4, utile pour le partage) ; *Save*. Valeurs d'exemple : remplace `tank` par ton pool.
2. **Quota** (conseillé) : *Edit* > *Advanced Options* > *Quota for this dataset*, par exemple **50 Go**. Il doit dépasser `HUB_INBOX_KEEP_FREE` (10 Go par défaut), sinon tous les envois sont refusés (507) : la place réellement disponible pour les envois est le quota moins cette réserve.
3. **Droits** : voir « Droits d'écriture du conteneur » ci-dessous, à régler **avant** l'étape suivante.
4. **Si la boîte contient déjà des dossiers** (`media_hub-data/inbox/…`), déplace-les maintenant (voir « Reprendre les anciens dossiers » ci-dessous), l'application arrêtée.
5. **Activer** : dans `docker-compose.yml` (ou dans l'éditeur YAML de TrueNAS), décommente **les deux lignes** :

   ```yaml
       environment:
         HUB_INBOX: /inbox
       volumes:
         - /mnt/tank/HubReception:/inbox     # lecture et écriture : pas de « :ro »
   ```

   Chemin absolu, comme les autres (`${APPS}` et `${MEDIA}` ne conviennent pas : le dataset n'est pas dans ces dossiers). Redéploie l'application entière (*Edit* > *Save*, ou `docker compose up -d` ; un simple `docker restart` ne relit pas le compose).
6. **Vérifier** : voir « Vérifications après déploiement ».

**Ce réglage n'existe qu'ici, jamais dans l'interface** : un compte administrateur compromis (le hub est visible d'Internet) pourrait sinon faire écrire le serveur n'importe où. Ce montage est le seul volume en lecture et écriture, avec `media_hub-data`.

### Droits d'écriture du conteneur

Le conteneur tourne sous le compte `PUID:PGID`, sans aucune capacité (voir [Compte du conteneur](#compte-du-conteneur)). Pour la boîte :

- **Écrire, créer un dossier, renommer** : le compte doit avoir ces droits sur le dataset, par son propriétaire ou par son ACL. Le hub n'a besoin de rien d'autre : il ne fait ni `chown` ni `chmod` ni `utime` dans la boîte. Les dossiers et fichiers créés appartiennent au compte, ou héritent de l'ACL du dataset.
- Un dataset en lecture seule, plein (quota atteint) ou ayant un attribut « immuable » refuse l'écriture quels que soient les droits.
- **Côté partage SMB** : pour que ton compte SMB puisse lire, renommer et supprimer ce que le hub dépose, le plus simple est que ce soit le même compte que `PUID`. Sinon, ouvre l'ACL du dataset : TrueNAS > Datasets > `HubReception` > *Permissions* > *Edit* : ajoute une entrée pour ton compte SMB (*Full Control*, avec les drapeaux *Inherit*), coche *Apply permissions recursively* si le dataset contient déjà des fichiers, puis *Save*.
- **Essai** (seul le NAS tranche) :

  ```bash
  docker exec media-hub sh -c 'touch /inbox/.essai && mv /inbox/.essai /inbox/.essai2 && rm /inbox/.essai2' && echo OK
  ```

  `OK` : le conteneur écrit et renomme. `Permission denied` : propriétaire, ACL ou mode du dataset (ouvre l'accès au compte `PUID` par l'ACL, ou donne-lui le dataset : `chown -R`) ; `Read-only file system` : le dataset ou le montage est en lecture seule. N'élargis pas les capacités du conteneur pour contourner cela.

### Reprendre les anciens dossiers (`media_hub-data/inbox/` vers le dataset)

Activer `HUB_INBOX=/inbox` avec un dataset vide **ne déplace pas** les anciens dossiers : ils restent dans `media_hub-data/inbox/`, que le hub ne regarde plus (leurs fichiers ne sont pas listés, mais ne sont pas perdus). Pour les reprendre, **application arrêtée** (pour qu'aucun envoi ne se glisse entre-temps) :

```bash
rsync -a /mnt/tank/apps/media/media_hub-data/inbox/ /mnt/tank/HubReception/
ls -l /mnt/tank/HubReception/         # vérifie : un dossier par compte, les fichiers y sont
```

Puis active les deux lignes du compose (étape 5) et redémarre. Le hub retrouve les dossiers par leur nom : `users.inbox_dir`, dans `hub.db`, garde le nom de chaque dossier, et un dossier de ce nom présent dans la boîte est repris tel quel ; un ancien dossier encore nommé d'après l'identifiant est renommé au démarrage, comme ci-dessus. Quand tout est bon, supprime les anciens dossiers de `media_hub-data/inbox/` (ou garde-les jusqu'à la prochaine sauvegarde). `rsync` conserve les propriétaires et les dates ; s'il manque : `cp -a`.

Pour revenir à la boîte dans `media_hub-data` : arrête l'application, recopie de la même façon `HubReception` vers `media_hub-data/inbox/`, recommente les deux lignes, redéploie.

### Les récupérer et les ranger

- **Partage SMB** : TrueNAS > Shares > SMB > *Add*, accessible à ton seul compte. Avec le dataset dédié, le chemin est **`/mnt/tank/HubReception`**, directement : tu vois un dossier par compte, lisible par nom (« Léa », « admin »…). Sans dataset dédié, le chemin est le sous-dossier `/mnt/tank/apps/media/media_hub-data/inbox`. **Ne monte jamais `hub.db` ni `media_hub-data` dans un partage** : `hub.db` contient les hachés des mots de passe. Copie ou déplace les fichiers depuis ton poste. Un fichier effacé par ce partage libère aussitôt son espace (hors instantanés).
- **Ne renomme ni ne supprime un dossier de compte depuis le partage**, et surtout pas pendant que le hub tourne : le lien compte -> dossier est dans la base, qui ne le sait pas. Le dossier renommé apparaît alors dans la liste de l'administrateur comme « compte supprimé » (ses fichiers y sont toujours), et le compte en recrée un neuf, vide, à son envoi suivant. Range les **fichiers** dans les dossiers, pas les dossiers eux-mêmes.
- **Téléchargement** : l'onglet de la boîte de réception dans *Gestion* (administrateur seulement), un fichier à la fois, ou `GET /api/inbox/<dossier>/<nom>`.
- **Ranger** à la main : déplace le fichier dans le bon dossier de `/mnt/tank/media/…` (renomme-le comme le fait `media_library`), puis *Gestion* > *Tout mettre à jour*. Le hub n'ouvre ni ne décompresse un fichier déposé, un `.zip` y compris.

### L'espace

Le hub ne limite ni la taille d'un fichier ni l'espace d'un compte : seul le disque limite, et sans réglage, des envois peuvent prendre la place de la médiathèque (le pool est partagé). Deux protections se complètent, mets les deux :

1. **Une limite sur le dataset de la boîte** : `media_hub-data` par défaut, `HubReception` s'il existe (TrueNAS > Datasets > le dataset > *Edit* > *Advanced Options* > *Quota for this dataset*). Choisis la place que la boîte peut prendre, plus `HUB_INBOX_KEEP_FREE`. Pour `media_hub-data`, ajoute la place des données du hub (base, cache d'images, TMDB : quelques Go, davantage avec beaucoup d'affiches). Exemple : 40 Go pour les envois, 10 Go à garder libres, 10 Go de données : **60 Go** (ou **50 Go** pour `HubReception`, sans données). Une limite plus petite que `HUB_INBOX_KEEP_FREE` fait refuser tous les envois (507).
2. **`HUB_INBOX_KEEP_FREE`** : le serveur refuse (507) un envoi qui laisserait moins de cet espace libre **sur le volume de la boîte** (`/data`, ou `/inbox` avec le dataset dédié), avant d'en lire un octet. Avec un quota, c'est l'espace libre *du dataset* que le serveur voit (à vérifier, voir l'essai ci-dessous) ; sans quota, c'est celui du pool, partagé avec la médiathèque.

Les instantanés gardent les fichiers effacés : si le dataset de la boîte en a, l'espace d'une boîte vidée ne revient qu'à l'expiration de l'instantané.

### Ajuster les limites

Deux variables sont dans le `.env` (en Mo, valeurs par défaut dans `.env.example`) :

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `HUB_INBOX_KEEP_FREE` | espace libre à garder sur le volume de la boîte (507) | `10240` |
| `HUB_INBOX_MAX_UPLOADS` | envois simultanés, tous comptes confondus (429) | `2` |

Il n'y a pas de réglage de taille par fichier ni par compte : la limite de dataset ci-dessus est le plafond. Le conteneur ne les relit qu'à sa création : modifie le `.env` (ou les valeurs collées dans l'éditeur YAML de TrueNAS) puis **redéploie l'application** (*Edit* > *Save*, ou `docker compose up -d`). Un simple `docker restart media-hub` garde les anciennes valeurs. Un envoi en cours est coupé par le redéploiement.

### Sauvegarde et instantanés

- **Le dataset « HubReception » ne contient que des fichiers déposés, pas de base** : programme-lui des **instantanés périodiques** (TrueNAS > Data Protection > *Periodic Snapshot Tasks* > *Add*, dataset `tank/HubReception`, par exemple chaque jour, conservés 2 semaines) et, si tu les gardes, une réplication ou une sauvegarde *Cloud Sync* vers un autre emplacement. Ce sont des fichiers à ranger, pas des données à garder longtemps : si la boîte est grosse, range-la avant ou exclus-la de la sauvegarde.
- **Le lien compte -> dossier est dans `hub.db`**, donc dans `media_hub-data` : sauvegarde `media_hub-data` aussi. Restaurer l'un sans l'autre (une vieille `hub.db` avec un dataset récent, ou l'inverse) ne perd aucun fichier : des dossiers apparaissent « compte supprimé » dans la liste de l'administrateur, et un compte peut recevoir un dossier neuf (`Léa (2)`). Range ensuite les fichiers.
- Sans dataset dédié, la boîte est dans `media_hub-data`, avec `hub.db` : un instantané de ce dataset l'emporte. Si la boîte est grosse, exclus `inbox/` de la sauvegarde de la base (la tâche de sauvegarde accepte un sous-dossier à exclure) ou range-la avant.
- Les `.part` d'envois abandonnés sont effacés par le serveur au bout de 24 h.

### Retour arrière (version d'avant)

Le retour lui-même se fait en remettant l'ancien numéro d'image (voir [Revenir à la version d'avant](#revenir-à-la-version-davant)) ; ce qui suit est à faire avant, si l'ancienne version date d'avant le renommage des dossiers de la boîte.

Une ancienne version du hub cherche `inbox/<identifiant>/` et ne voit plus les dossiers renommés d'après le compte : **les fichiers ne sont pas perdus**, ils ne sont simplement plus listés (et le compte recevrait un dossier neuf nommé d'après l'identifiant). Deux issues : **rester sur la nouvelle version** (le plus simple), ou revenir en arrière en renommant les dossiers à la main, `<nom du compte>` vers `<identifiant>` (relevé avec `docker exec -it media-hub python -m hub user list` **avant** le retour), dans `media_hub-data/inbox/`. Une ancienne version ne connaît pas `HUB_INBOX` : si tu as activé le dataset, recopie d'abord son contenu dans `media_hub-data/inbox/`.

### Couper en urgence

- Retire le droit « Envoyer » du compte (*Paramètres* > *Comptes*) : il vaut dès l'envoi suivant (403). Fais-le pour chaque compte pour tout couper. Un envoi déjà en cours va jusqu'au bout ; redémarre le conteneur `media-hub` pour l'interrompre.
- Un `.part` resté après une coupure brutale peut être supprimé à la main dans le dossier du compte (`inbox/<nom du compte>/`, ou `HubReception/<nom du compte>/`).

### Vérifications après déploiement

À faire une fois le dataset activé (les deux premières valent aussi sans lui) :

1. **Pas d'« ATTENTION » au démarrage** : `docker logs media-hub 2>&1 | grep -n "ATTENTION"` ne doit rien afficher qui parle de la boîte de réception (ni « n'est pas utilisable », ni « recoupe » HUB_DATA ou une bibliothèque, ni « n'est pas un point de montage »).
2. **Écriture et renommage** : la commande d'essai de « Droits d'écriture du conteneur » répond `OK`.
3. **Un envoi arrive au bon endroit** : depuis la page « Envoyer », avec un compte qui a ce droit, envoie un petit fichier (un `.srt`, par exemple), puis dans le shell du NAS `ls -l /mnt/tank/HubReception/` : un dossier au **nom du compte** contient le fichier, sans `.part`. Regarde aussi le partage SMB : le dossier y est, et ton compte SMB peut renommer puis supprimer ce fichier (sinon, l'ACL du dataset : voir plus haut).
4. **L'espace libre refuse un envoi trop gros sur ce dataset** : relève l'espace libre vu par le conteneur, `docker exec media-hub df -m /inbox` (à comparer à celui de TrueNAS pour `HubReception`). Puis soit envoie un fichier plus gros que cet espace moins `HUB_INBOX_KEEP_FREE`, soit, plus simple, mets temporairement `HUB_INBOX_KEEP_FREE` au-dessus de l'espace libre (par exemple 2 Mo de plus que le résultat de `df`), redéploie, et envoie le petit fichier : l'envoi est refusé (507) avec « plus assez de place sur le serveur pour ce fichier ». **Remets ensuite la valeur d'origine** (`10240`) et redéploie. Supprime le fichier d'essai de la boîte.

### Essai à faire sur place (Funnel, 1 à 2 Go)

Je n'ai pas pu lancer cet essai : il demande le NAS et Funnel. Il vérifie ce qu'on ne peut pas régler : le débit de Funnel, ses délais, et l'espace réel sous `/data` (ou `/inbox`). Note chaque résultat.

**Préparer** (sur ton poste, pas sur le NAS) :

```bash
head -c 1500000000 /dev/urandom > test-funnel.mkv     # 1,5 Go ; Windows : fsutil file createnew test-funnel.mkv 1500000000
read -rs -p "Mot de passe : " PW; echo                 # un compte qui a le droit « Envoyer »
TOKEN=$(curl -s -X POST -H "Content-Type: application/json" -d "{\"user\":\"alice\",\"password\":\"$PW\"}" https://nas.tail59c227.ts.net/api/token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
```

(Pas de `/dev/urandom` sous Windows ? Du contenu aléatoire est préférable à des zéros : ZFS compresserait des zéros et fausserait la mesure d'espace.)

**1. Espace avant** : `docker exec media-hub df -m /data` (`/inbox` avec le dataset dédié : c'est ce que le serveur lit pour `HUB_INBOX_KEEP_FREE`) et, dans TrueNAS, l'espace libre et le quota du dataset de la boîte (`media_hub-data`, ou `HubReception`). Les deux doivent concorder ; s'ils diffèrent, note de combien.

**2. Envoi en ligne de commande, par Funnel** (depuis un réseau qui n'est pas celui du NAS, par exemple en partage de connexion du téléphone) :

```bash
curl -X POST -T test-funnel.mkv -H "Authorization: Bearer $TOKEN" --progress-bar \
  -w '\nHTTP %{http_code} : %{size_upload} octets en %{time_total} s (%{speed_upload} octets/s)\n' \
  "https://nas.tail59c227.ts.net/api/inbox?name=test-funnel.mkv"
```

Note le débit (octets/s ÷ 1 000 000 pour des Mo/s), la durée, et si l'envoi s'interrompt (code d'erreur de curl, ou avancement figé). Le serveur coupe un envoi qui ne reçoit rien pendant 120 s ; Funnel peut couper avant, ce que les journaux de `media-hub` ne montreront pas : seul le message de curl le dit. Attendu : `HTTP 201` et le nom retenu (`{"name": "test-funnel.mkv", …}`).

**3. Envoi par le navigateur** : sur la page « Envoyer », même fichier, par l'adresse Funnel. Note la même chose, et si la barre de progression avance sans saccade.

**4. Pendant un envoi** (relance-le pour avoir le temps) : depuis un autre appareil, lis un film et navigue dans les bibliothèques. Rien ne doit saccader ni se bloquer : un envoi tient un fil du serveur, pas un des `HUB_MAX_STREAMS` flux de lecture. Si tu peux, lance en même temps deux lectures en transcodage : elles démarrent, la troisième seulement répond 503.

**5. Espace après** : de nouveau `docker exec media-hub df -m /data` (ou `/inbox`) et le dataset. L'espace libre a baissé de la taille du fichier (à quelques Mo près). Puis, dans le shell du NAS :

```bash
ls -l /mnt/tank/apps/media/media_hub-data/inbox/<nom du compte>/     # ou /mnt/tank/HubReception/<nom du compte>/
```

Le fichier doit avoir la **taille exacte** de l'original (`wc -c test-funnel.mkv`, ici 1 500 000 000), aucun `.part` à côté. Pour être sûr du contenu : `sha256sum` des deux.

**6. Envoi annulé** : relance l'envoi, interromps-le à mi-chemin (Ctrl+C, ou ferme l'onglet). Attends 3 minutes (le serveur ne voit la coupure qu'à la fin de son délai de lecture si Funnel n'a pas fermé proprement) puis relis le dossier : **aucun `.part` ne doit rester**, et l'espace libre doit être revenu. S'il en reste un, note-le : le serveur ne l'efface qu'après 24 h.

**7. Refus** (sans modifier de réglage) :

- Un compte **sans** le droit « Envoyer » (avec son propre jeton) lance la même commande `curl` : `HTTP 403`, en une seconde ou deux. Si curl met des minutes, le serveur lit le fichier avant de refuser : c'est un défaut à signaler.
- Faute de place : crée un fichier creux plus gros que l'espace libre du dataset de la boîte (vu à l'étape 1, plus `HUB_INBOX_KEEP_FREE` ; par exemple `truncate -s 2T gros.mkv`, qui n'occupe pas d'espace) et envoie-le avec la même commande : `HTTP 507` aussitôt, avant que le moindre octet ait été lu. curl peut afficher une erreur d'envoi en plus (la connexion est coupée) : ce qui compte est le code HTTP, quand il y en a un, et que la réponse arrive en quelques secondes. Vérifie ensuite qu'aucun fichier ni `.part` n'est apparu dans la boîte (`inbox/` ou `HubReception/`).

Efface ensuite `test-funnel.mkv` de la boîte (téléchargement/suppression dans *Gestion*, ou `rm` sur le NAS). Si l'un de ces points échoue, mets le résultat (code, durée, journal `docker logs media-hub`) dans une issue ou dans la page Notion des essais sur place (voir `docs/essais-sur-place.md`, qui y renvoie).

## L'image Docker

C'est la voie de déploiement. À chaque étiquette de version (`vX.Y.Z`, posée par `/release`), `.github/workflows/image.yml` construit l'image (`deploy/Dockerfile`), l'essaie et la publie : `ghcr.io/arthur-lagenebre/media_hub:<version>` (et `latest`, que le compose n'utilise **pas**). Elle contient `media_library` (dans `/app/media_library`, `HUB_REPO` déjà réglé), le ffmpeg de Debian et Pillow. Elle écoute `0.0.0.0:8080`, garde ses données dans `/data` et refuse de démarrer sans `HUB_PASSWORD` tant que la base n'a pas de compte. Les bibliothèques se montent sous `/srv`. Le compose y ajoute le ffmpeg figé (`/opt/ffmpeg`), qui l'emporte sur celui de l'image.

### Variante : sources montées dans `python:3.15-slim`

L'ancienne façon, pour le développement ou un dépannage (essayer un correctif sans publier de version) : le code est copié sur le NAS (`${APPS}/media_hub` et `${APPS}/media_library`, voisins) et monté dans une image Python nue, et Pillow s'installe dans `media_hub-data/pylib` au premier démarrage. Une mise à jour = recopier `hub/` puis redémarrer l'application ; on ne sait plus exactement quelle version tourne. Dans le service `media_hub`, remplace `image:` et ajoute :

```yaml
    image: python:3.15-slim
    working_dir: /app/media_hub
    command: ["sh", "-c", "python -c 'import PIL' 2>/dev/null || pip install -q --no-cache-dir --disable-pip-version-check --target /data/pylib pillow || echo 'media_hub : Pillow non installé (facultatif)'; exec python -m hub"]
    environment:            # à ajouter aux variables existantes
      PYTHONPATH: /data/pylib
      PYTHONUNBUFFERED: "1"
      HUB_REPO: /app/media_library
    volumes:                # à ajouter aux volumes existants
      - ${APPS}/media_hub:/app/media_hub:ro
      - ${APPS}/media_library:/app/media_library:ro
```

Pour revenir à l'image, remets la ligne `image: ghcr.io/arthur-lagenebre/media_hub:<version>` et retire ces lignes.

## Dépannage

| Symptôme | Piste |
| --- | --- |
| « ffmpeg introuvable » dans le journal | le dossier monté en `/opt/ffmpeg` doit contenir `ffmpeg` **et** `ffprobe`, exécutables (avec l'image, un `/opt/ffmpeg` vide fait plutôt prendre en silence le ffmpeg de Debian : `docker exec media-hub /opt/ffmpeg/ffmpeg -version`) |
| L'app ne démarre pas après un changement de version : « unauthorized », « denied » ou « manifest unknown » | voir [Si l'image ne se télécharge pas](#si-limage-ne-se-télécharge-pas) : paquet ghcr.io privé, ou numéro sans image publiée (workflow `Image` échoué ou pas fini). Remets l'ancien numéro en attendant |
| Image qui saccade en transcodage | `HUB_MAX_STREAMS=1`, ou lire en direct (H.264) : le plan de lecture annonce la raison |
| 503 « trop de flux » | `HUB_MAX_STREAMS` atteint ; attendre ou l'augmenter |
| Bibliothèque vide | le dossier doit s'appeler `Films`, `Series`, `Musique` ou `Livres` sous `/srv` (sinon `HUB_LIBRARIES`) |
| Mot de passe refusé alors que `HUB_PASSWORD` est bon | `HUB_PASSWORD` n'amorce que le premier compte ; ensuite : `docker exec -it media-hub python -m hub user passwd <nom>` |
| `https://nas…` ne répond pas | `docker logs media-hub-tailscale` : clé expirée, HTTPS non activé dans la console Tailscale, ou **Funnel non autorisé dans la politique du tailnet** (attribut `funnel` absent pour le nœud `nas` : ça marche alors avec Tailscale, pas sans) |
| `media-hub-tailscale` redémarre en boucle, le port 8080 refuse la connexion, et `docker logs media-hub-tailscale` dit « failed to read serve config: invalid character '{' after top-level value » | le `serve.json` copié dans `tailscale-config/` n'est pas un JSON valide (par exemple le nouveau contenu collé à la suite de l'ancien). Remplace **tout** le fichier par `deploy/serve.json`, vérifie-le avec `python3 -m json.tool <chemin>/serve.json`, puis redémarre l'application entière |
| Un envoi est refusé : 507 / 429 | 507 : le fichier laisserait moins de `HUB_INBOX_KEEP_FREE` d'espace libre sur le volume de la boîte (`/data`, ou `/inbox` avec le dataset dédié ; range ou supprime des fichiers de la boîte, ou agrandis la limite du dataset ; des instantanés peuvent retenir l'espace) ; 429 : `HUB_INBOX_MAX_UPLOADS` envois déjà en cours, réessaie plus tard |
| Un envoi est refusé : 503 « la boîte de réception du serveur est inutilisable », ou « ATTENTION : la boîte de réception … » au démarrage | `docker logs media-hub 2>&1 \| grep -e ATTENTION -e "boîte de réception"` donne le chemin et la cause. Dataset non monté ou inexistant : le montage `/mnt/tank/HubReception:/inbox` et `HUB_INBOX: /inbox` s'activent **ensemble** (un montage vers un dossier absent empêche le conteneur de démarrer) ; droits : lance l'essai `docker exec media-hub sh -c 'touch /inbox/.essai && mv /inbox/.essai /inbox/.essai2 && rm /inbox/.essai2'` (voir « Droits d'écriture du conteneur ») ; dataset en lecture seule ou plein ; « recoupe HUB_DATA ou une bibliothèque » : choisis un dossier dédié hors des données et des bibliothèques ; « n'est pas un point de montage » : le volume `/mnt/tank/HubReception:/inbox` n'est pas déclaré (ou pas sur ce chemin) dans le yaml |
| Tout le monde est bloqué (« trop d'essais ») après des échecs d'une seule personne | le serveur ne voit pas l'adresse réelle du client (tout arrive de `127.0.0.1`) : voir l'étape 4 d'« Ouvrir à Internet » ; le blocage s'efface après 5 minutes ou au redémarrage de `media-hub` |
