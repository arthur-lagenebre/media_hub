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
  media_hub-data/     vide : base SQLite, cache d'images, Pillow
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

Sans ffmpeg, le serveur ne sait que lire directement (fichiers compatibles) ; il l'écrit au démarrage. Le remux du son (E-AC-3, DTS), le transcodage et les sous-titres du `.mkv` en ont besoin.

## 3. Installer l'application

1. Tailscale > Settings > Keys : crée une clé d'authentification (inutile si `tailscale/` contient déjà un état connecté).
2. Copie `.env.example` en `.env`, à côté de `docker-compose.yml`, et remplis `TS_AUTHKEY`, `HUB_PASSWORD`, `TMDB_KEY` (facultative), `APPS`, `MEDIA`.
3. TrueNAS : Apps > Discover Apps > Custom App > *Install via YAML* : colle `docker-compose.yml` (en remplaçant les `${...}` par leurs valeurs si l'éditeur ne lit pas le `.env`).
4. Au premier démarrage, le serveur indexe les bibliothèques (quelques minutes pour des milliers de fichiers) ; l'avancement est dans le journal du conteneur et dans la page *Gestion* de l'interface.

Vérification : `http://192.168.0.42:8080` demande le mot de passe, puis affiche les bibliothèques.

## 4. Sur la télé

- **Fire TV** : navigateur Silk ou Firefox. **Google TV / Android TV** : TV Bro ou Puffin TV (Chrome n'est pas proposé).
- Ouvre `http://192.168.0.42:8080` à la maison (pas de HTTPS nécessaire) ; hors de la maison, `https://nas.tail59c227.ts.net` demande Tailscale sur l'appareil.
- Flèches pour naviguer, OK pour ouvrir, Retour pour revenir. Ajoute la page en favori / écran d'accueil du navigateur.

## 5. Au quotidien

| Besoin | Où |
| --- | --- |
| Nouveaux fichiers visibles | *Gestion* > Mettre à jour (ou `HUB_AUTOSCAN_HOURS`) |
| Affiches / synopsis des séries | *Gestion* > Enrichir (TMDB) (avec `TMDB_KEY`) |
| Supprimer les `Thumbs.db` | *Gestion* > Fichiers parasites > Supprimer ; retirer `:ro` des volumes concernés |
| Étiqueter / renommer / convertir | scripts de `media_library`, depuis VS Code, ou dans le conteneur (volumes sans `:ro`) : `docker exec -it media-hub python /app/media_library/scripts/Movies/Metadata.py --dir /srv/Films` |
| Les fiches HTML | retirées de `media_library` ; à régénérer ponctuellement avec un prompt dans VS Code si besoin |

## Sécurité

- `HUB_BIND=0.0.0.0` expose le serveur sur le réseau local : garde `HUB_PASSWORD`. Sur Internet, seul Tailscale (tailnet privé) est exposé ; n'active pas Funnel.
- Les bibliothèques sont montées en lecture seule par défaut ; le serveur n'écrit que dans `media_hub-data`.
- `.env` contient des secrets : ne le publie pas.

## Dépannage

| Symptôme | Piste |
| --- | --- |
| « ffmpeg introuvable » dans le journal | le dossier monté en `/opt/ffmpeg` doit contenir `ffmpeg` **et** `ffprobe`, exécutables |
| Image qui saccade en transcodage | `HUB_MAX_STREAMS=1`, ou lire en direct (H.264) : le plan de lecture annonce la raison |
| 503 « trop de flux » | `HUB_MAX_STREAMS` atteint ; attendre ou l'augmenter |
| Bibliothèque vide | le dossier doit s'appeler `Films`, `Series`, `Animations`, `Musique` ou `Livres` sous `/srv` (sinon `HUB_LIBRARIES`) |
| `https://nas…` ne répond pas | `docker logs media-hub-tailscale` : clé expirée, HTTPS non activé dans la console Tailscale |
