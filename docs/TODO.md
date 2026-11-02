# À faire

État au 1er novembre 2026, après la fusion des sagas (ordre de sortie, `sagas.json`, éditeur, films manquants), de la relecture de tout le code (scan, durcissement, robustesse), du déploiement et des tests d'interface. Tout le code prévu est dans `main` : ce qui reste demande soit d'essayer sur place, soit une décision.

## À faire de votre côté

- [x] **Redéployer** le compose. Vérifier d'abord que `HUB_PASSWORD` est dans le `.env` : il est obligatoire, sans lui `docker compose up` refuse de démarrer (il ne sert qu'à créer le premier compte, les comptes existants ne changent pas).
- [x] **Retirer le `:ro`** des volumes `Animations` et `Musique` dans le compose pour que le nettoyage des fichiers parasites puisse effacer. Pour `Musique`, il faut aussi que le serveur puisse écrire `artist.webp` dans les dossiers d'artistes (photos recadrées).
- [x] **Avant de « Supprimer » en musique** : compter les `folder.*` sur le NAS (`find /mnt/tank/media/Musique -iname 'folder.*' | wc -l`). En musique, le nettoyage efface toute image autre que `cover.{ext}`, `folder.jpg` compris.
- [x] **Après le redéploiement** : lancer un scan (le dossier de chaque artiste n'est connu qu'après), puis essayer *Changer la photo* / *Recadrer* sur une fiche d'artiste. Les photos d'avant restent sous `HUB_DATA/artists/` jusqu’à leur remplacement. Ensuite *Gestion* > *Entretien* > *Compter*, puis *Supprimer*. Si une erreur de droits apparaît, retirer `cap_drop` du compose (voir `deploy/README.md`, section Sécurité).
- [x] **Lancer « Sagas (TMDB) »** (*Gestion* > bibliothèque `Films`) avec la clé TMDB, et vérifier avec de vraies collections (MCU, Fast & Furious…) : seul un faux TMDB a été testé.
- [ ] **Essayer à la télécommande**, sur la télé : sagas, éditeur, sélecteur de films, cartes grisées. Les tests tournent dans un Chromium de bureau, pas sur Android TV, Fire TV ni webOS.
- [x] Si `test_admin_runs_an_update` échoue de nouveau (il a échoué une fois sur votre machine, jamais sur l'autre) : le relancer seul avec `python -W ignore -m unittest tests.test_ui.TestDetailPages.test_admin_runs_an_update` et garder les lignes de l'échec. Ce test attend au plus 30 s qu'un scan se termine.

## À décider

- [ ] **Cookie de connexion sans `Secure`.** Le marquer `Secure` casserait l'accès en `http://` sur le réseau local (la télé), il faudrait donc distinguer HTTPS de HTTP derrière Tailscale. Avis : ne pas le faire. Le cookie a déjà `HttpOnly; SameSite=Lax` et un `Max-Age` (`app.py`, `session_cookie`), le gain est faible sur un réseau de maison et le risque est de casser la télé.
- [ ] **Jeton de session sans expiration**, non révoqué à la déconnexion (c'est documenté dans `docs/api.md`). Le changer déconnecte tous les appareils. Avis : la vraie lacune est l'impossibilité de révoquer. Piste : un compteur par compte, intégré au jeton, qu'on incrémente à la déconnexion ou sur demande (« se déconnecter partout »). À vérifier dans `hub/accounts.py` avant de s'engager ; sans urgence.
- [ ] **Épingler les images Docker** (`python:3.13-slim`, `tailscale/tailscale:stable`) et la version de Pillow. Avis : inutile pour un NAS à la maison, sauf si chaque redéploiement doit donner exactement la même chose.
- [ ] **« Compter »** : afficher les 20 premiers fichiers qu'il supprimerait, pour voir ce qui partira avant de confirmer. Avis : oui, c'est un garde-fou à petit prix sur une action irréversible.
- [ ] **Sagas de `sagas.json`** : cacher aussi un film listé qui n'est pas encore sorti (aujourd'hui, il s'affiche toujours, puisqu'on l'a écrit exprès). Question : le cacher, ou le griser comme les films manquants ?

## Évolutions souhaitées

### Films

- [ ] **Bouton « … » sur les films et les sagas** : *Voir / Revoir*, *Marquer vu / non vu*, *Ajouter à une saga*, *TMDB*.
- [ ] **Changer les affiches** (pour le confort, le hub est le seul lecteur visé). Aujourd'hui le hub ne garde jamais l'original d'un film : il le relit dans la médiathèque, `folder.jpg` à côté du film, sinon la jaquette embarquée dans le `.mkv` (`hub/media/images.py`, `item_source`). Seules les versions réduites vivent dans `HUB_DATA/img`, et l'affiche TMDB de `HUB_DATA/tmdb/` ne concerne que les séries et les animations. À décider avant de commencer :
  - *Dans `HUB_DATA`* (avis : préférable si le hub est le seul lecteur) : l'affiche choisie est gardée comme celle des séries (`poster_file`). Rien n'est écrit dans la médiathèque, le `:ro` du volume `Films` ne gêne pas, l'invariant « le hub n'écrit pas dans la médiathèque » est respecté. Coût : un autre lecteur (Jellyfin, Kodi) ne la verrait pas.
  - *`folder.jpg` à côté du film* : passe déjà devant la jaquette du `.mkv`, visible par d'autres lecteurs. Coût : nouvelle exception à l'invariant d'écriture, volume `Films` à passer en lecture-écriture, et le cache d'images doit tenir compte de la date de `folder.jpg` (aujourd'hui seule la pochette d'un album entre dans la clé, `_cover_stamp`).
  - *Dans le `.mkv`* : déconseillé depuis le hub (`mkvpropedit` absent du conteneur, film ouvert en lecture pendant l'écriture, nouvelle fonction à écrire dans `media_library` pour une image quelconque). Si le besoin vient, le faire en ligne de commande côté `media_library`.

### Séries et animations

- [ ] **Bouton « … » sur les séries et les animations** : *Voir / Revoir*, *Marquer vu / non vu*, *TMDB*.
- [ ] **Changer les textes** : « Marquer comme vu » / « Marquer non vu » deviennent « Marquer toutes les saisons comme vu » / « Marquer toutes les saisons non vu ».
- [ ] **« Marquer comme vu » / « Marquer non vu » sur chaque épisode**, éventuellement derrière un bouton « … ».

### Global

- [ ] **Télécharger** les films, les séries, les animations, la musique et les livres. Décision prise : **toujours un fichier à la fois, jamais de zip** (une coupure ne fait perdre qu'un fichier, et la reprise se fait par requête `Range`).
  - Films : un fichier.
  - Séries et animations : un épisode, ou une saison / la série entière ; dans ces deux cas le navigateur enchaîne les téléchargements, un par épisode.
  - Musique et livres : même principe, une piste ou un tome, ou un album / une série de tomes enchaînés.
  - Serveur : une route (ou une option de `/media`) qui sert le fichier tel quel avec `Content-Disposition: attachment` ; à vérifier si `/media` n'en fait pas déjà assez. L'enchaînement par saison ou album est du ressort de l'interface seule. Vérifier qu'un téléchargement long ne bloque pas les autres requêtes et ne compte pas dans `HUB_MAX_STREAMS`.
  - Interface : masquer le bouton sur `data-device="tv"` (les navigateurs de télé téléchargent mal). À grouper avec le bouton « … ». Le jeton de média reste global : une URL de téléchargement n'est pas liée au compte.

### Interface

- [ ] **Renommer « Admin » en « Paramètres ».**
- [ ] **Séparer les paramètres en onglets**, en haut de la page et qui restent flottants :
  - *Gestion* (bibliothèques, sagas, entretien) : admin ;
  - *Comptes* : « Mon compte » pour tout le monde, « Comptes » pour l'admin ;
  - *Tâches* (avec la possibilité de tout déplier et de tout voir) : admin ;
  - *Serveur* : admin.
- [ ] **Une autre page d'accueil.** Pas d'idée encore : à chercher.

## Ordre proposé

1. **Interface** (`hub-ui`, puis `hub-reviewer`) : un seul composant de menu « … » pour les films, sagas, séries, animations et épisodes (les routes `POST /api/item/<id>/seen` et `POST /api/entry/<id>/seen` existent déjà), les nouveaux libellés « toutes les saisons », puis, dans une autre PR, « Admin » → « Paramètres » avec ses onglets. Vérifier que le menu se manie à la télécommande.
2. **Petites décisions** glissées dans les PR voisines : aperçu de « Compter », film non sorti dans une saga.
3. **Téléchargement** : route serveur (`hub-server`), puis entrée de menu et enchaînement (`hub-ui`).
4. **Affiches** : après avoir tranché entre `HUB_DATA` et `folder.jpg` (voir plus haut).
5. **Jeton révocable**, si l'envie vient.
6. **Autre page d'accueil** : en dernier, tant qu'on n'a pas identifié ce que l'accueil actuel manque à l'usage.

## Limites connues, non traitées

- [ ] Avec **deux bibliothèques de films**, les collections TMDB sont repérées par le seul nom de saga : la seconde peut être sautée.
- [ ] Un **film en double** (deux versions) dans une saga désactive sans bruit ses numéros et ses manquants.
- [ ] `deploy/fetch-ffmpeg.sh` ne vérifie aucune somme de contrôle.
- [ ] `pip install pillow` est lancé à chaque démarrage du conteneur, en root, sa sortie jetée.
- [ ] Les photos d'artistes, la liseuse et le lecteur n'ont pas été essayés sur la télé.

## Pas relu

- Le dépôt `media_library` (modifications non commitées côté développement).
- `.claude/skills/`, volontairement laissé de côté.
