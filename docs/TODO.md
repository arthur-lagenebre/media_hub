# À faire

État au 1er novembre 2026, après la fusion des sagas (ordre de sortie, `sagas.json`, éditeur, films manquants), de la relecture de tout le code (scan, durcissement, robustesse), du déploiement et des tests d'interface. Tout le code prévu est dans `main` : ce qui reste demande soit d'essayer sur place, soit une décision.

## À faire de votre côté

- [x] **Redéployer** le compose. Vérifier d'abord que `HUB_PASSWORD` est dans le `.env` : il est obligatoire, sans lui `docker compose up` refuse de démarrer (il ne sert qu'à créer le premier compte, les comptes existants ne changent pas).
- [x] **Retirer le `:ro`** des volumes `Animations` et `Musique` dans le compose pour que le nettoyage des fichiers parasites puisse effacer. Pour `Musique`, il faut aussi que le serveur puisse écrire `artist.webp` dans les dossiers d'artistes (photos recadrées).
- [x] **Avant de « Supprimer » en musique** : compter les `folder.*` sur le NAS (`find /mnt/tank/media/Musique -iname 'folder.*' | wc -l`). En musique, le nettoyage efface toute image autre que `cover.{ext}`, `folder.jpg` compris.
- [x] **Après le redéploiement** : lancer un scan (le dossier de chaque artiste n'est connu qu'après), puis essayer *Changer la photo* / *Recadrer* sur une fiche d'artiste. Les photos d'avant restent sous `HUB_DATA/artists/` jusqu’à leur remplacement. Ensuite *Gestion* > *Entretien* > *Compter*, puis *Supprimer*. Si une erreur de droits apparaît, retirer `cap_drop` du compose (voir `deploy/README.md`, section Sécurité).
- [x] **Lancer « Sagas (TMDB) »** (*Gestion* > bibliothèque `Films`) avec la clé TMDB, et vérifier avec de vraies collections (MCU, Fast & Furious…) : seul un faux TMDB a été testé.
- [ ] **Essayer à la télécommande**, sur la télé : sagas, éditeur, sélecteur de films, cartes grisées. Les tests tournent dans un Chromium de bureau, pas sur Android TV, Fire TV ni webOS.
- [ ] Si `test_admin_runs_an_update` échoue de nouveau (il a échoué une fois sur votre machine, jamais sur l'autre) : le relancer seul avec `python -W ignore -m unittest tests.test_ui.TestDetailPages.test_admin_runs_an_update` et garder les lignes de l'échec. Ce test attend au plus 30 s qu'un scan se termine.

## À décider

- [ ] **Cookie de connexion sans `Secure`.** Le marquer `Secure` casserait l'accès en `http://` sur le réseau local (la télé), il faudrait donc distinguer HTTPS de HTTP derrière Tailscale.
- [ ] **Jeton de session sans expiration**, non révoqué à la déconnexion (c'est documenté dans `docs/api.md`). Le changer déconnecte tous les appareils.
- [ ] **Épingler les images Docker** (`python:3.13-slim`, `tailscale/tailscale:stable`) et la version de Pillow. Avis : inutile pour un NAS à la maison, sauf si chaque redéploiement doit donner exactement la même chose.
- [ ] **« Compter »** : afficher les 20 premiers fichiers qu'il supprimerait, pour voir ce qui partira avant de confirmer.
- [ ] **Sagas de `sagas.json`** : cacher aussi un film listé qui n'est pas encore sorti (aujourd'hui, il s'affiche toujours, puisqu'on l'a écrit exprès).

## Évolutions souhaitées

### Films

- [ ] **Bouton « … » sur les films et les sagas** : *Voir / Revoir*, *Marquer vu / non vu*, *Ajouter à une saga*, *TMDB*.
- [ ] **Changer les affiches.** Aujourd'hui le hub prend `folder.jpg` à côté du film, sinon la jaquette `cover.jpg` embarquée dans le `.mkv`. Si l'on en choisit une autre, la glisser aussi dans le `.mkv` : le hub appellerait `media_library` (qui sait déjà remplacer cette jaquette avec `mkvpropedit`, mais seulement à partir d'une affiche TMDB : il lui faut une fonction qui prenne n'importe quelle image). À régler avant : `mkvpropedit` n'est pas dans le conteneur, le volume `Films` est monté en `:ro` dans le compose du dépôt, un film en cours de lecture est ouvert pendant l'écriture, et le cache d'images doit être invalidé après le changement. Deux temps possibles : d'abord écrire `folder.jpg` (sans toucher au `.mkv`), puis l'intégration au `.mkv`.

### Séries et animations

- [ ] **Bouton « … » sur les séries et les animations** : *Voir / Revoir*, *Marquer vu / non vu*, *TMDB*.
- [ ] **Changer les textes** : « Marquer comme vu » / « Marquer non vu » deviennent « Marquer toutes les saisons comme vu » / « Marquer toutes les saisons non vu ».
- [ ] **« Marquer comme vu » / « Marquer non vu » sur chaque épisode**, éventuellement derrière un bouton « … ».

### Global

- [ ] **Télécharger** les films, les séries, les animations, la musique et les livres.

### Interface

- [ ] **Renommer « Admin » en « Paramètres ».**
- [ ] **Séparer les paramètres en onglets**, en haut de la page et qui restent flottants :
  - *Gestion* (bibliothèques, sagas, entretien) : admin ;
  - *Comptes* : « Mon compte » pour tout le monde, « Comptes » pour l'admin ;
  - *Tâches* (avec la possibilité de tout déplier et de tout voir) : admin ;
  - *Serveur* : admin.
- [ ] **Une autre page d'accueil.** Pas d'idée encore : à chercher.

## Limites connues, non traitées

- [ ] Avec **deux bibliothèques de films**, les collections TMDB sont repérées par le seul nom de saga : la seconde peut être sautée.
- [ ] Un **film en double** (deux versions) dans une saga désactive sans bruit ses numéros et ses manquants.
- [ ] `deploy/fetch-ffmpeg.sh` ne vérifie aucune somme de contrôle.
- [ ] `pip install pillow` est lancé à chaque démarrage du conteneur, en root, sa sortie jetée.
- [ ] Les photos d'artistes, la liseuse et le lecteur n'ont pas été essayés sur la télé.

## Pas relu

- Le dépôt `media_library` (modifications non commitées côté développement).
- `.claude/skills/`, volontairement laissé de côté.
