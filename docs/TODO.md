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

- [x] **Bouton « … » sur les films et les sagas** : *Voir / Revoir*, *Marquer vu / non vu*, *Ajouter à une saga* (administrateur, film connu de TMDB) et *TMDB*, sur la carte, au clic droit et à la touche « Menu ». À essayer à la télécommande.
- [x] **Changer les affiches** : fait pour les films et les séries, en choisissant une image sur l'appareil (fiche > *Changer l'affiche*, *Affiche d'origine* la retire ; l'image reste dans `HUB_DATA/posters`, rien n'est écrit dans la médiathèque). Reste, si l'envie vient : proposer les affiches de TMDB, et l'intégration au `.mkv`. Texte d'origine : (pour le confort, le hub est le seul lecteur visé). Aujourd'hui le hub ne garde jamais l'original d'un film : il le relit dans la médiathèque, `folder.jpg` à côté du film, sinon la jaquette embarquée dans le `.mkv` (`hub/media/images.py`, `item_source`). Seules les versions réduites vivent dans `HUB_DATA/img`, et l'affiche TMDB de `HUB_DATA/tmdb/` ne concerne que les séries et les animations. À décider avant de commencer :
  - *Dans `HUB_DATA`* (avis : préférable si le hub est le seul lecteur) : l'affiche choisie est gardée comme celle des séries (`poster_file`). Rien n'est écrit dans la médiathèque, le `:ro` du volume `Films` ne gêne pas, l'invariant « le hub n'écrit pas dans la médiathèque » est respecté. Coût : un autre lecteur (Jellyfin, Kodi) ne la verrait pas.
  - *`folder.jpg` à côté du film* : passe déjà devant la jaquette du `.mkv`, visible par d'autres lecteurs. Coût : nouvelle exception à l'invariant d'écriture, volume `Films` à passer en lecture-écriture, et le cache d'images doit tenir compte de la date de `folder.jpg` (aujourd'hui seule la pochette d'un album entre dans la clé, `_cover_stamp`).
  - *Dans le `.mkv`* : déconseillé depuis le hub (`mkvpropedit` absent du conteneur, film ouvert en lecture pendant l'écriture, nouvelle fonction à écrire dans `media_library` pour une image quelconque). Si le besoin vient, le faire en ligne de commande côté `media_library`.

- [ ] **Supprimer le choix « Sagas / pas Sagas » de la bibliothèque des films.** Décidé : plus de bouton, plus de réglage. Les films sont **toujours regroupés en sagas** (c'est l'état par défaut d'aujourd'hui : le bouton est allumé tant qu'on ne l'a pas éteint) ; la page de la saga montre ses films, et un film sans saga reste une carte seule. À faire : retirer le bouton « Sagas » et le réglage retenu (`sagas.<bibliothèque>`) de `hub/web/js/views.js`, et le test « le choix des sagas est retenu » de `tests/test_ui.py`. Le paramètre `sagas=1` de `GET /api/library/<id>` n'a pas à disparaître : un autre client peut encore vouloir la liste à plat (à garder, ou à retirer si personne d'autre ne s'en sert). *À confirmer* : si l'intention était plutôt de ne **jamais** regrouper, c'est l'inverse, mais cela cacherait les sagas dont le hub s'est donné tant de mal à numéroter les films.

### Séries et animations

- [x] **Bouton « … » sur les séries et les animations** : *Voir / Revoir*, *Marquer vu / non vu* et *TMDB* (une fois la série enrichie). À essayer à la télécommande.
- [x] **Changer les textes** : « Marquer comme vu » / « Marquer non vu » deviennent « Marquer toutes les saisons comme vu » / « Marquer toutes les saisons non vu ».
- [x] **« Marquer comme vu » / « Marquer non vu » sur chaque épisode**, éventuellement derrière un bouton « … ».
- [x] **Une série qui tombe sur la mauvaise fiche TMDB** (deux séries portent le même titre : *Hercule*). Deux remèdes : l'enrichissement garde désormais les doutes de la recherche (`doubts`, affichés en avertissement sur la fiche pour l'administrateur, et dans le journal de la tâche) ; et *Changer la fiche TMDB* (nom de la série, ou adresse de sa fiche sur themoviedb.org) fixe l'identifiant dans la base du hub (`tmdb-pin:<id>`). Pas de moyen de retirer ce choix : on en fait un autre. La médiathèque n'est pas touchée : pour que media_library voie le même choix, il reste à écrire `{tmdb-4615}` dans le nom du dossier. Reste, si l'envie vient : proposer d'écrire ce marqueur depuis le hub quand le volume n'est pas en lecture seule.

### Global

- [x] **Télécharger** les films, les séries, les animations, la musique et les livres (fait ; à essayer sur un vrai navigateur : Chrome et Firefox demandent d'autoriser les téléchargements multiples la première fois). Décision prise : **toujours un fichier à la fois, jamais de zip** (une coupure ne fait perdre qu'un fichier, et la reprise se fait par requête `Range`).
  - Films : un fichier.
  - Séries et animations : un épisode, ou une saison / la série entière ; dans ces deux cas le navigateur enchaîne les téléchargements, un par épisode.
  - Musique et livres : même principe, une piste ou un tome, ou un album / une série de tomes enchaînés.
  - Serveur : `/media/<id>/file?download=1` en faisait déjà assez (`Content-Disposition: attachment`, `Range`, hors `HUB_MAX_STREAMS`), rien n'a changé côté serveur. L'enchaînement par saison ou album est du ressort de l'interface seule. Vérifier qu'un téléchargement long ne bloque pas les autres requêtes et ne compte pas dans `HUB_MAX_STREAMS`.
  - Interface : masquer le bouton sur `data-device="tv"` (les navigateurs de télé téléchargent mal). À grouper avec le bouton « … ». Le jeton de média reste global : une URL de téléchargement n'est pas liée au compte.

### Musique

- [ ] **La bibliothèque de musique par artiste.** Aujourd'hui la page montre tous les albums, regroupés sous le nom de leur artiste. En plus, au choix : la liste des **artistes seulement**, sans leurs albums, chacun avec **la photo qu'on a choisie pour lui** (*Changer la photo*, *Recadrer*, photo de Wikipédia : `GET /img/artist/<lib>/<id>`), et à défaut la pochette de son premier album. Un clic ouvre la fiche de l'artiste, où sont ses albums.
  - Décidé : **les deux vues coexistent**, et c'est la personne qui choisit entre « Artistes » et « Albums ». **La vue par défaut reste celle d'aujourd'hui (les albums).** Le choix se fait dans **Paramètres**, pas dans la barre d'outils de la page : voir « Réglages d'affichage » plus bas.
  - Décidé : les albums sans artiste ont leur carte dans la vue par artistes, sous un nom lisible (« Artiste inconnu », ou « Artistes divers » pour un « Various Artists » traduit en français), **en dernier** pour que les vrais artistes viennent d'abord (en premier si on préfère : un simple tri).
  - À trancher : où se retient le choix : **par appareil** (comme les réglages de lecture, dans le navigateur) ou **par compte** (il suit la personne de la télé au téléphone, mais demande de garder les réglages côté serveur).
  - Côté serveur, la liste de la bibliothèque rend des albums : il faudra une liste d'artistes (nom, nombre d'albums, présence d'une photo), avec les filtres qui existent déjà (lettre, genre, recherche).
  - Côté interface : une carte ronde ou carrée par artiste, la photo chargée par `/img/artist/...` (le cache d'images en tient déjà compte), le focus à la télécommande comme les autres cartes.

- [ ] **Les artistes de chaque piste, sous son titre, dans la liste des pistes d'un album.** Cas vu : une bande originale (« Various Artists ») où chaque piste a ses artistes, qu'on ne voit nulle part dans la liste ; seuls les invités (`feat.`) y apparaissent.
  - Vérifié : la donnée existe déjà. C'est le tag **`ARTIST`** de chaque fichier (`info.artist`, lu au scan par `media_library`), à ne pas confondre avec **`ALBUMARTIST`**, qui donne l'artiste de l'album et son groupement. `GET /api/item/<id>` la rend pour chaque piste (`entries[].artist`) et la barre de lecture l'affiche déjà : **aucun changement serveur**, seule la liste de la fiche (`views.js`, `tracks`) ne la montre pas.
  - À faire : sous le titre de la piste, l'artiste de la piste quand il **diffère** de l'artiste de l'album (sinon le même nom se répète sur chaque ligne d'un album d'un seul artiste) ; une seule ligne, qui **remplace** l'affichage des « feat. » d'aujourd'hui (décidé : pas une seconde ligne en plus). Règle proposée : sur un album d'un artiste, « Deep Purple feat. X » montre « feat. X » (la part qui s'ajoute à l'artiste de l'album) ; sur une compilation, chaque piste montre son artiste en entier.
  - Ce qui classe un album « Various Artists » ou « Deep Purple » : rien de particulier, aucune règle « compilation » n'existe dans `media_library` ni dans le hub. L'artiste de l'album est, dans l'ordre, le tag **`ALBUMARTIST`** (la valeur la plus répandue parmi les pistes), sinon le tag `ARTIST` (idem), sinon le nom lu dans le dossier, sinon « Artiste inconnu ». « Various Artists » n'est donc que le texte écrit dans `ALBUMARTIST`, et tous les albums qui l'écrivent (à la casse et aux accents près) partagent la même fiche d'artiste. Piège : une compilation **sans** `ALBUMARTIST` est rangée sous l'artiste qui revient le plus dans ses pistes. Pour « Artistes divers » (traduction française voulue), c'est un simple affichage de ce nom, sans changer le classement.
  - À vérifier : si les listes de la file d'attente et des playlists montrent bien l'artiste de chaque piste (elles mélangent des albums, donc l'artiste y compte encore plus).
  - Test : un album d'essai dont les pistes ont des artistes différents (les fixtures n'en ont pas), et un test d'interface qui lit la ligne sous le titre.

### Interface

- [ ] **Réglages d'affichage dans Paramètres, pour tout le monde (administrateur ou non).** Les réglages d'affichage vont dans les Paramètres, qui servent maintenant surtout aux comptes ordinaires : un onglet (ou la suite de *Lecture*) avec
  - **Musique** : la vue ouverte par défaut, *Albums* (valeur d'aujourd'hui) ou *Artistes* ;
  - la place des réglages qu'on ajoutera ensuite (tri par défaut, section ouverte des livres…).

- [x] **Renommer « Admin » en « Paramètres ».**
- [x] **Séparer les paramètres en onglets**, en haut de la page et qui restent flottants (fait ; j'ai ajouté un onglet *Lecture* pour la qualité, l'appareil et le son 5.1, qui servent à tout le monde) :
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
