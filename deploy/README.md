# Déployer media_hub sur TrueNAS Scale

Deux conteneurs qui partagent le même réseau :

```text
tailscale  ── HTTPS ──►  https://nas.tail59c227.ts.net   (partout, tout navigateur, sans Tailscale : Funnel)
   │ (même réseau)
media_hub  ── :8080 ──►  http://192.168.0.42:8080         (télé, à la maison)
```

**L'adresse `https://nas…` est ouverte à tout Internet** (Tailscale Funnel) : voir [Sécurité](#sécurité) et [Ouvrir à Internet (Funnel)](#ouvrir-à-internet-funnel) avant de la donner à quelqu'un.

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
4. Au premier démarrage, le serveur indexe les bibliothèques (quelques minutes pour des milliers de fichiers) ; l'avancement est dans le journal du conteneur et dans les *Tâches* de *Paramètres* > *Gestion* (en bas de la page).

Vérification : `http://192.168.0.42:8080` demande le nom et le mot de passe (`HUB_ADMIN` / `HUB_PASSWORD`), puis affiche les bibliothèques.

## Comptes

Chaque personne a son compte (progression, playlists, « Reprendre » à elle) ; les bibliothèques sont communes. `HUB_ADMIN` et `HUB_PASSWORD` ne servent qu'**une fois** : au démarrage, si la base n'a aucun compte, ils créent le premier (administrateur, identifiant `default`). Ensuite ils sont ignorés : changer `HUB_PASSWORD` dans le `.env` ne change plus le mot de passe (utilise `passwd` ci-dessous).

**Mise à jour depuis une version sans comptes.** Remplace le dossier `media_hub`, ajoute `HUB_ADMIN` au `.env` (et à `docker-compose.yml` si tu le colles à la main), puis redémarre l'application. Le premier compte créé reprend la progression et les playlists existantes, rien n'est perdu. Chaque appareil (navigateur, télé, clients natifs) doit se reconnecter **une fois** avec le nom et le mot de passe ; les anciens jetons ne valent plus.

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

**Sauvegarde.** Les comptes (noms, hachés de mots de passe), la progression et les playlists sont dans `hub.db`, sous `HUB_DATA` (ici `/mnt/tank/apps/media/media_hub-data`). Sauvegarde ce dossier (un snapshot du dataset suffit) ; sans lui, il faudrait recréer les comptes. Aucun mot de passe en clair n'y figure.

**Hachage des mots de passe.** Le serveur utilise `scrypt` si l'OpenSSL de Python le fournit (c'est le cas de l'image `python:3.13-slim`). Vérifie-le :

```bash
docker exec media-hub python -c "import hashlib; print(hasattr(hashlib,'scrypt'))"
```

`True` : rien à faire. `False` : repli sur `pbkdf2` (600 000 tours), correct mais plus lent, environ 0,3 s à chaque connexion.

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
- **Films manquants des réalisateurs et acteurs de films** : *Paramètres* > *Gestion* > « Films manquants (bibliothèques de films) » (avec `TMDB_KEY`) demande à TMDB les films de chaque réalisateur et acteur de premier plan : un appel par personne, **une heure ou plus la première fois** pour quelques centaines de films (un appel par personne, puis les affiches des films qu'on n'a pas : plusieurs centaines de Mo sous `HUB_DATA/tmdb`, et les réponses de TMDB gardées 7 jours sous `HUB_DATA/tmdb-cache`), en tâche de fond. Si TMDB ne répond plus, la tâche s'arrête après quelques échecs de suite : relance-la. À lancer après le scan qui lit les identifiants ; une personne déjà faite depuis moins d'un mois n'est pas redemandée, et une passe interrompue reprend où elle en était. Le résultat se lit sur la page de chaque personne.
- **Séries et animations, films de leurs acteurs** : relance une fois *Gestion* > *Détail par bibliothèque* > « Enrichir (TMDB) » sur Séries et Animations : l'enrichissement garde désormais l'identifiant TMDB de chaque acteur du casting, et les séries déjà enrichies sont refaites une fois pour cela (un peu plus long que d'habitude). Ensuite « Films des personnes (TMDB) » sur la ligne de la bibliothèque cherche les films de ses acteurs ; une passe par catégorie, une heure ou plus chacune la première fois.
- **Photos d'artistes** (après le tout premier déploiement) : le dossier de chaque artiste n'est connu qu'après un scan.

## Sécurité

- `HUB_BIND=0.0.0.0` expose le serveur sur le réseau local : `HUB_PASSWORD` est obligatoire dans le compose (sans compte, le serveur est ouvert à tous).
- **Tailscale Funnel est activé** (`AllowFunnel` dans `serve.json`) : le hub est **visible de tout Internet**, à l'adresse `https://nas.tail59c227.ts.net`. Rien d'autre n'est publié : Funnel n'ouvre que le port 443 du `media_hub`, et le port 8080 de la télé reste sur le réseau local. Il n'y a **pas de double authentification** : la défense se limite au compte et à son mot de passe, avec `scrypt` pour le hachage, un ralentissement croissant à chaque échec, un blocage (429) après 10 échecs en 5 minutes, compté par adresse réelle du client, et des mots de passe de 12 caractères au moins (à la création et au changement). Un mot de passe faible ou réutilisé est donc la seule chose qui sépare un inconnu de la médiathèque : voir la procédure ci-dessous.
- Le débit d'un Funnel est limité par Tailscale (limites non configurables, sans chiffre publié) : un film lu depuis l'extérieur peut saccader ; à la maison, utilise l'adresse locale.
- Le conteneur `media_hub` n'a pas de nouveaux privilèges (`no-new-privileges`) et ne garde que deux capacités Linux : `DAC_OVERRIDE` et `FOWNER`, que « Supprimer » les fichiers parasites et les scripts de `media_library` demandent quand les fichiers appartiennent à un autre compte que root. Après un redéploiement, essaie *Gestion* > *Entretien* > *Supprimer*, puis *Compter* dans la fenêtre, puis *Supprimer définitivement* (avec un volume sans `:ro`) : si une erreur de droits apparaît, retire `cap_drop` du compose.
- Les bibliothèques sont montées en lecture seule par défaut ; le serveur n'écrit que dans `media_hub-data`.
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

## Image Docker (facultative)

Le compose ci-dessus monte les sources dans `python:3.13-slim`. À chaque étiquette de version (`vX.Y.Z`), `.github/workflows/image.yml` publie aussi une image toute faite, `ghcr.io/arthur-lagenebre/media_hub:<version>` (et `latest`), avec media_library, ffmpeg et Pillow dedans. Elle écoute `0.0.0.0:8080`, garde ses données dans `/data` et refuse de démarrer sans `HUB_PASSWORD` tant que la base n'a pas de compte. Les bibliothèques se montent sous `/srv`. Le compose n'a pas changé : l'image est une autre manière de déployer, pas un remplacement.

## Dépannage

| Symptôme | Piste |
| --- | --- |
| « ffmpeg introuvable » dans le journal | le dossier monté en `/opt/ffmpeg` doit contenir `ffmpeg` **et** `ffprobe`, exécutables |
| Image qui saccade en transcodage | `HUB_MAX_STREAMS=1`, ou lire en direct (H.264) : le plan de lecture annonce la raison |
| 503 « trop de flux » | `HUB_MAX_STREAMS` atteint ; attendre ou l'augmenter |
| Bibliothèque vide | le dossier doit s'appeler `Films`, `Series`, `Animations`, `Musique` ou `Livres` sous `/srv` (sinon `HUB_LIBRARIES`) |
| Mot de passe refusé alors que `HUB_PASSWORD` est bon | `HUB_PASSWORD` n'amorce que le premier compte ; ensuite : `docker exec -it media-hub python -m hub user passwd <nom>` |
| `https://nas…` ne répond pas | `docker logs media-hub-tailscale` : clé expirée, HTTPS non activé dans la console Tailscale, ou **Funnel non autorisé dans la politique du tailnet** (attribut `funnel` absent pour le nœud `nas` : ça marche alors avec Tailscale, pas sans) |
| `media-hub-tailscale` redémarre en boucle, le port 8080 refuse la connexion, et `docker logs media-hub-tailscale` dit « failed to read serve config: invalid character '{' after top-level value » | le `serve.json` copié dans `tailscale-config/` n'est pas un JSON valide (par exemple le nouveau contenu collé à la suite de l'ancien). Remplace **tout** le fichier par `deploy/serve.json`, vérifie-le avec `python3 -m json.tool <chemin>/serve.json`, puis redémarre l'application entière |
| Tout le monde est bloqué (« trop d'essais ») après des échecs d'une seule personne | le serveur ne voit pas l'adresse réelle du client (tout arrive de `127.0.0.1`) : voir l'étape 4 d'« Ouvrir à Internet » ; le blocage s'efface après 5 minutes ou au redémarrage de `media-hub` |
