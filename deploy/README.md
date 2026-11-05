# Déployer media_hub sur TrueNAS Scale

Deux conteneurs qui partagent le même réseau :

```text
tailscale  ── HTTPS ──►  https://nas.tail59c227.ts.net   (partout, via ton tailnet)
   │ (même réseau)
media_hub  ── :8080 ──►  http://192.168.0.42:8080         (télé, à la maison)
```

Le `media_hub` remplace l'ancien `updater` (page d'accueil + boutons « Mettre à jour » qui générait des pages HTML) : l'interface a son propre bouton de mise à jour. Tu peux garder le dossier Tailscale (`tailscale/`) tel quel : l'état de connexion est réutilisé, `serve.json` est le même.

## 1. Préparer les dossiers

Sur un dataset d'applications (ici `/mnt/tank/apps/media`, adapte) :

```text
/mnt/tank/apps/media/
  media_hub/          le dossier media_hub de cette archive
  media_library/      le dossier media_library de cette archive
  media_hub-data/     vide : base SQLite, cache d'images, Pillow (pylib/, installé au premier démarrage ; le supprimer pour le réinstaller)
  ffmpeg/             ffmpeg et ffprobe (étape 2)
  tailscale/          l'état Tailscale (vide au premier lancement)
  tailscale-config/   y copier deploy/serve.json
```

Les bibliothèques gardent leur place (`/mnt/tank/media/Films`, `Series`, `Animations`, `Musique`, `Livres`). Si le conteneur tourne avec un utilisateur non root, `media_hub-data` doit lui être accessible en écriture.

## 2. ffmpeg

Dans le shell du NAS :

```bash
sh /mnt/tank/apps/media/media_hub/deploy/fetch-ffmpeg.sh /mnt/tank/apps/media/ffmpeg
```

Le script télécharge une version fixe (7.0.2) et refuse l'archive si sa somme SHA-256 n'est pas celle qu'il attend. Pour une autre version, changer `VERSION` et `SHA256` ensemble dans `fetch-ffmpeg.sh`.

Sans ffmpeg, le serveur ne sait que lire directement (fichiers compatibles) ; il l'écrit au démarrage. Le remux du son (E-AC-3, DTS), le transcodage et les sous-titres du `.mkv` en ont besoin.

## 3. Installer l'application

1. Tailscale > Settings > Keys : crée une clé d'authentification (inutile si `tailscale/` contient déjà un état connecté).
2. Copie `.env.example` en `.env`, à côté de `docker-compose.yml`, et remplis `TS_AUTHKEY`, `HUB_ADMIN` (nom du premier compte, `admin` par défaut), `HUB_PASSWORD` (son mot de passe, **obligatoire** : sans lui `docker compose up` refuse de démarrer, plutôt que de servir une médiathèque ouverte à tout le réseau local ; il ne sert qu'à créer ce premier compte), `TMDB_KEY` (facultative), `APPS`, `MEDIA`.
3. TrueNAS : Apps > Discover Apps > Custom App > *Install via YAML* : colle `docker-compose.yml` (en remplaçant les `${...}` par leurs valeurs si l'éditeur ne lit pas le `.env`).
4. Au premier démarrage, le serveur indexe les bibliothèques (quelques minutes pour des milliers de fichiers) ; l'avancement est dans le journal du conteneur et dans l'onglet *Tâches* des *Paramètres* de l'interface.

Vérification : `http://192.168.0.42:8080` demande le nom et le mot de passe (`HUB_ADMIN` / `HUB_PASSWORD`), puis affiche les bibliothèques.

## Comptes

Chaque personne a son compte (progression, playlists, « Reprendre » à elle) ; les bibliothèques sont communes. `HUB_ADMIN` et `HUB_PASSWORD` ne servent qu'**une fois** : au démarrage, si la base n'a aucun compte, ils créent le premier (administrateur, identifiant `default`). Ensuite ils sont ignorés : changer `HUB_PASSWORD` dans le `.env` ne change plus le mot de passe (utilise `passwd` ci-dessous).

**Mise à jour depuis une version sans comptes.** Remplace le dossier `media_hub`, ajoute `HUB_ADMIN` au `.env` (et à `docker-compose.yml` si tu le colles à la main), puis redémarre l'application. Le premier compte créé reprend la progression et les playlists existantes, rien n'est perdu. Chaque appareil (navigateur, télé, clients natifs) doit se reconnecter **une fois** avec le nom et le mot de passe ; les anciens jetons ne valent plus.

**Ajouter ou gérer des comptes.** Dans l'interface (*Paramètres* > *Comptes*, en administrateur), ou dans le shell du NAS (le conteneur s'appelle `media-hub` ; `-it` est nécessaire pour saisir le mot de passe sans l'afficher) :

```bash
docker exec -it media-hub python -m hub user list
docker exec -it media-hub python -m hub user add alice            # mot de passe demandé, 8 caractères au moins
docker exec -it media-hub python -m hub user add bob --admin      # un deuxième administrateur
docker exec -it media-hub python -m hub user passwd alice
docker exec -it media-hub python -m hub user rm alice             # supprime aussi sa progression et ses playlists
docker exec -it media-hub python -m hub user rights alice                                  # ce qu'alice voit et télécharge, par bibliothèque
docker exec -it media-hub python -m hub user rights alice --set Livres=none --set Films=view   # all : voir, lire et télécharger ; play : voir et lire ; view : voir seulement ; none : rien
```

**Droits.** Par défaut un compte voit et télécharge toutes les bibliothèques ; l'administrateur peut en fermer une à un compte, ou en retirer seulement la lecture ou le téléchargement (*Comptes* > *Droits*, ou `user rights`). Les droits valent tout de suite, sans reconnexion. Un client natif (Android TV, Chromecast) qui gardait un jeton de média d'avant les droits doit le redemander par `POST /api/token` : ce jeton est maintenant celui du compte.

Depuis le shell web de TrueNAS (Apps > media-hub > Shell), lance seulement `python -m hub user …`. Le serveur peut tourner pendant ce temps. Il n'y a pas d'inscription publique.

**Sauvegarde.** Les comptes (noms, hachés de mots de passe), la progression et les playlists sont dans `hub.db`, sous `HUB_DATA` (ici `/mnt/tank/apps/media/media_hub-data`). Sauvegarde ce dossier (un snapshot du dataset suffit) ; sans lui, il faudrait recréer les comptes. Aucun mot de passe en clair n'y figure.

**Hachage des mots de passe.** Le serveur utilise `scrypt` si l'OpenSSL de Python le fournit (c'est le cas de l'image `python:3.13-slim`). Vérifie-le :

```bash
docker exec media-hub python -c "import hashlib; print(hasattr(hashlib,'scrypt'))"
```

`True` : rien à faire. `False` : repli sur `pbkdf2` (600 000 tours), correct mais plus lent, environ 0,3 s à chaque connexion.

## 4. Sur la télé

- **Fire TV** : navigateur Silk ou Firefox. **Google TV / Android TV** : TV Bro ou Puffin TV (Chrome n'est pas proposé).
- Ouvre `http://192.168.0.42:8080` à la maison (pas de HTTPS nécessaire) ; hors de la maison, `https://nas.tail59c227.ts.net` demande Tailscale sur l'appareil.
- Flèches pour naviguer, OK pour ouvrir, Retour pour revenir. Ajoute la page en favori / écran d'accueil du navigateur.

## 5. Au quotidien

| Besoin | Où |
| --- | --- |
| Nouveaux fichiers visibles | *Paramètres* > *Gestion* > Mettre à jour (ou `HUB_AUTOSCAN_HOURS`) |
| Affiches / synopsis des séries | *Paramètres* > *Gestion* > Enrichir (TMDB) (avec `TMDB_KEY`) |
| Supprimer les `Thumbs.db` | *Paramètres* > *Gestion* > Fichiers parasites > Supprimer ; retirer `:ro` des volumes concernés. Commence par *Compter* : la fenêtre liste les 20 premiers fichiers. **En musique, toute image autre que `cover.{ext}` est effacée, `folder.jpg` compris** : compte-les d'abord (`find /mnt/tank/media/Musique -iname 'folder.*' \| wc -l`) |
| Étiqueter / renommer / convertir | scripts de `media_library`, depuis VS Code, ou dans le conteneur (volumes sans `:ro`) : `docker exec -it media-hub python /app/media_library/scripts/Video/Movies/Metadata.py --dir /srv/Films` |
| Les fiches HTML | retirées de `media_library` ; à régénérer ponctuellement avec un prompt dans VS Code si besoin |

## Après une mise à jour du hub

Lance un scan (*Paramètres* > *Gestion* > Mettre à jour) quand une version ajoute une information lue dans les fichiers : les éléments déjà indexés sont relus une fois, sans migration de base. Tant qu'il n'est pas fait, les fiches se comportent comme avant.

- **Musique** : les artistes de chaque piste (tag `ARTISTS`, pour les invités), le titre des disques (`DISCSUBTITLE`) et le type d'album (`RELEASETYPE`). Ces tags sont écrits par `Metadata.py --apply` de `media_library` : s'il n'est pas passé sur la musique, les intertitres restent « Disque N » et le type d'album n'apparaît pas.
- **Photos d'artistes** (après le tout premier déploiement) : le dossier de chaque artiste n'est connu qu'après un scan.

## Sécurité

- `HUB_BIND=0.0.0.0` expose le serveur sur le réseau local : `HUB_PASSWORD` est obligatoire dans le compose (sans compte, le serveur est ouvert à tous). Sur Internet, seul Tailscale (tailnet privé) est exposé ; n'active pas Funnel.
- Le conteneur `media_hub` n'a pas de nouveaux privilèges (`no-new-privileges`) et ne garde que deux capacités Linux : `DAC_OVERRIDE` et `FOWNER`, que « Supprimer » les fichiers parasites et les scripts de `media_library` demandent quand les fichiers appartiennent à un autre compte que root. Après un redéploiement, essaie *Gestion* > *Entretien* > *Compter* puis *Supprimer* (avec un volume sans `:ro`) : si une erreur de droits apparaît, retire `cap_drop` du compose.
- Les bibliothèques sont montées en lecture seule par défaut ; le serveur n'écrit que dans `media_hub-data`.
- `.env` contient des secrets : ne le publie pas.

## Dépannage

| Symptôme | Piste |
| --- | --- |
| « ffmpeg introuvable » dans le journal | le dossier monté en `/opt/ffmpeg` doit contenir `ffmpeg` **et** `ffprobe`, exécutables |
| Image qui saccade en transcodage | `HUB_MAX_STREAMS=1`, ou lire en direct (H.264) : le plan de lecture annonce la raison |
| 503 « trop de flux » | `HUB_MAX_STREAMS` atteint ; attendre ou l'augmenter |
| Bibliothèque vide | le dossier doit s'appeler `Films`, `Series`, `Animations`, `Musique` ou `Livres` sous `/srv` (sinon `HUB_LIBRARIES`) |
| Mot de passe refusé alors que `HUB_PASSWORD` est bon | `HUB_PASSWORD` n'amorce que le premier compte ; ensuite : `docker exec -it media-hub python -m hub user passwd <nom>` |
| `https://nas…` ne répond pas | `docker logs media-hub-tailscale` : clé expirée, HTTPS non activé dans la console Tailscale |
