# À faire

Ce qui reste ouvert. Ce qui est fait est dans `git log` et dans les pull requests ; ce qui a été tranché, surtout les « non », est dans [decisions.md](decisions.md) : à lire avant de proposer quelque chose de nouveau.

## Prochaines fonctionnalités

Dans l'ordre où on les prend (7 novembre 2026), un lot par PR (ou deux).

- [ ] **Films manquants d'une personne : à essayer sur place.** Le lot 3 est livré (serveur et interface : `person_ids`, `POST /api/enrich/person`, bouton « Chercher ses films manquants » et cases grisées sur `#/person?n=`). Un bouton général (*Gestion* > « Films manquants (bibliothèques de films) », `POST /api/enrich/people`) le fait pour tous les réalisateurs et acteurs de premier plan des films, et un bouton par bibliothèque (« Films des personnes (TMDB) », films, séries, animations) pour une catégorie à la fois. Reste : un enrichissement réel avec ta clé TMDB (acteur, réalisateur), à la télécommande.
- [ ] **Lot 4 : boîte de réception (téléversement).** Des comptes qui ont le droit « Envoyer » déposent films, séries, musique ou livres ; l'administrateur les range lui-même. En trois PR : **4a** le serveur (migration du droit, réception en flux, limites, tests de sécurité ; *fait* : `hub/inbox.py`, `/api/inbox`, `POST /api/user/<id>/upload`, version 0.4.0), **4b** l'interface (*fait* : `hub/web/js/upload.js`, page `#/upload`, interrupteur « Peut envoyer des fichiers » dans Comptes, onglet « Boîte de réception » de Paramètres, version 0.5.0), **4c** le déploiement (espace libre sous `/data`, essai Funnel). Décision : [decisions.md](decisions.md). *Retenu :*
  - dépôt dans `HUB_DATA/inbox/<compte>/`, jamais dans une bibliothèque (les volumes restent en `:ro`), et pas de bouton qui déplace un fichier dans la médiathèque ;
  - un droit global par compte (colonne dans `users`, migration qui garde l'existant, administrateur toujours autorisé) ; pas d'envoi sans compte, pas de lien d'invitation ;
  - réception en flux : morceaux de 1 Mo vers un `.part` renommé à la fin (`body_bytes` lit tout en mémoire, 10 Mo : inutilisable ici), `Content-Length` obligatoire, limite par fichier, quota par compte, contrôle d'espace libre, extensions autorisées, nom nettoyé sans écrasement, nettoyage des `.part` abandonnés ;
  - jamais de ffprobe ni de scan sur un fichier déposé ; servi à l'administrateur seulement, en `attachment` ;
  - interface : page « Envoyer » (glisser-déposer, XHR avec progression, masquée sur la télé) et onglet « Boîte de réception » dans Gestion.
- [ ] **Refus précoce d'un gros envoi : le navigateur ne reçoit pas la réponse.** Le serveur contrôle avant de lire le corps puis coupe la connexion (`h.close_connection`) ; pour un gros fichier, Chromium n'a pas fini d'envoyer et signale une erreur réseau au lieu du 413, 415, 429 ou 507 (constaté avec un fichier de 3 Mo). L'interface (page « Envoyer », sans contrôle préalable) affiche alors un message neutre (« connexion coupée : serveur occupé ou fichier refusé »), et un 429 n'est pas réessayé tout seul. *À faire côté serveur :* après la réponse, fermer l'écriture (`shutdown(SHUT_WR)`) et vider une partie du corps (borne en octets et en temps) avant de fermer, pour que la réponse parvienne ; puis l'interface pourra distinguer 429 (réessayer) de 413, 415 et 507.

  *À essayer sur place :* un envoi de 1 à 2 Go à travers Funnel (débit et délais non configurables) et l'espace libre réel sous `/data`. La reprise d'un envoi interrompu (`Content-Range`) viendra plus tard, si le besoin se confirme.

## À essayer sur place

Les tests tournent dans un Chromium de bureau, pas sur la télé. Rien de tout cela ne bloque le reste.

- [ ] **La liseuse en double page** (lot 6, livré) sur une tablette et sur la télé : le bouton « Double page » en paysage, le focus des boutons « Double page » et « Décaler d’une page » à la télécommande, le sens de lecture retenu par série, la couverture seule puis les couples (et « Décaler d’une page » quand un livre commence par une double page). Sur un NAS lent : une vue ne s'affiche qu'une fois ses deux images chargées, les tours de page demandés entre-temps sont rejoués ensuite.
- [ ] **Les piles d'affiches à cache froid** sur le NAS : la première ouverture d'une bibliothèque de séries, de livres ou de BD après un vidage du cache d'images lit les affiches des premiers éléments de chaque série (`STACK_MAX = 3`). Mesurer la durée réelle de ce premier affichage, et vérifier qu'il ne ralentit pas les autres requêtes.
- [ ] **À la télécommande**, sur la télé (Android TV, Fire TV, webOS) : les sagas, l'éditeur de sagas, le sélecteur de films, les cartes grisées, le bouton « … » des films, des sagas, des séries et des animations, les photos d'artistes, la liseuse et le lecteur.
- [ ] **Les pop-up unifiés** ([popups.md](popups.md)) à la télécommande et au tactile : le pied de boutons est maintenant une rangée de boutons égaux (en colonne seulement pour plusieurs champs), y compris dans la file d'attente et le choix de films ; vérifier que Gauche / Droite passent d'un bouton à l'autre et que la file d'attente tient sur un téléphone étroit.
- [ ] **Les liens d'une fiche artiste** au tactile, sur un vrai téléphone : la croix de retrait à côté de chaque lien se touche sans viser à côté, et la fenêtre « Ajouter un lien » ne se laisse pas cacher par le clavier. Sur la télé, ces contrôles sont absents (saisir une adresse à la télécommande se fait mal).
- [ ] **Les téléchargements** sur un vrai navigateur : Chrome et Firefox demandent d'autoriser les téléchargements multiples la première fois. Vérifier aussi qu'un téléchargement long ne bloque pas les autres requêtes et ne compte pas dans `HUB_MAX_STREAMS`.
- [ ] **L'interface en anglais** sur la télé (Android TV, Fire TV) et sur un téléphone étroit : les textes anglais sont parfois plus longs que les français (boutons des Paramètres, barre du lecteur, fenêtres de droits). Les tests la parcourent à 1920 px et vérifient que rien ne manque, pas que rien ne déborde.
- [ ] **Reprendre un film en « Son converti »** sur le serveur (ffmpeg 7.0.2, MKV H.264 avec images B et son DTS, comme « La reine des damnés ») : l'image, le son et les sous-titres restent calés après une reprise, après un saut aux flèches et après un changement de piste audio. Les tests tournent avec le ffmpeg de la machine de développement, pas avec la version fixée dans `deploy/fetch-ffmpeg.sh`
- [ ] **Les sous-titres PGS** sur un vrai remux Blu-ray : le calage dans le temps, la position sur l'image quand le `.mkv` est recadré (le canvas suit la taille de l'écran du Blu-ray, pas celle de l'image recadrée), la durée de la première extraction sur un fichier de 30 Go (délai de 15 minutes), la fluidité du décodage dans le navigateur de la télé (Android TV, Fire TV). Les tests n'ont qu'un `.sup` fabriqué à la main.

- [ ] **Les notes TMDB** avec la vraie clé : un premier « Sagas (TMDB) » sur toute la bibliothèque de films (une requête par film étiqueté, sans le cache : durée réelle), puis un rescan, après lequel les notes doivent rester. Ensuite, le tri « Mieux notés » sur la vraie bibliothèque, en vue Sagas et Films : une saga se range à la note de son meilleur film. Sur la télé et sur un téléphone étroit : le disque de la note (anneau SVG) en bas à gauche de la vignette ne gêne ni le bouton « … » ni la barre de progression, et reste lisible de loin.

Si `test_admin_runs_an_update` échoue (il a échoué une fois sur une machine, jamais sur l'autre) : le relancer seul avec `python -W ignore -m unittest tests.test_ui.TestDetailPages.test_admin_runs_an_update` et garder les lignes de l'échec. Ce test attend au plus 30 s qu'un scan se termine.

## Limites connues, non traitées

- [ ] **Tests de la liseuse instables en CI** : `TestReaderSpreads` (`tests/test_ui.py`, la double page) échoue par intermittence sur le runner Linux, sur des branches qui n'ont rien à voir avec la liseuse : `docs-lot4` (documentation seulement), `ci-workflows`, `ci-correctifs` et `inbox-serveur` (serveur seulement), et jamais le même test d'une exécution à l'autre (`test_the_window_has_to_be_a_fifth_wider_than_tall`, `test_a_turn_followed_at_once_by_a_turn_back_never_leaves_the_screen_empty`, `test_two_quick_turns_in_single_mode_are_both_counted`, `test_the_last_spread_marks_the_volume_as_read`…). Le message est toujours « la condition n'est jamais devenue vraie » : `wait_pages` attend que les pages soient affichées et chargées, et `UiCase.until` n'attend que 4 s au total (40 essais de 100 ms). En local la classe passe (33 tests, plusieurs fois de suite) ; relancer le job CI suffit parfois, pas toujours. *Hypothèse, non vérifiée :* le chargement des images est plus lent sur le runner que le délai prévu. *À faire :* mesurer le temps réel d'affichage d'une vue sur le runner, puis allonger le délai de `wait_pages` (ou attendre un événement plutôt qu'un délai) ; ne pas ignorer les tests. Le test des droits (`test_the_administrator_sets_the_rights_and_the_account_sees_them`), qui échouait aussi souvent, est un autre sujet, corrigé par la PR #151.
- [ ] **Liseuse, double page** : passer de la page seule au double depuis une page paire donne des couples décalés (`[2,3]` au lieu de `[1,2]`) ; « Décaler d’une page » les recale. Une page paysage rompt aussi le couplage local : les couples ne suivent pas une grille fixe du livre.
- [ ] **Liseuse, images lentes** : les préchargements qui sortent de la fenêtre ne sont pas annulés (bande passante seulement), une image en erreur reste dans le cache et n'est pas retentée tant qu'elle y reste, et une image dont la requête ne répond jamais (ni chargement ni erreur) laisse les touches de la liseuse sans effet, faute de délai de garde (le curseur de progression reste utilisable).
- [ ] Une saga de `sagas.json` ne garde que la **première version** d'un film listé en double (`by_tmdb`, le premier gagne). Les sagas de l'étiquette, elles, numérotent les deux versions.
- [ ] Le conteneur `media_hub` **tourne en root** (avec `cap_drop: ALL` et deux capacités gardées, voir `deploy/README.md`).
- [ ] Le **jeton de session ne se périme pas**, et *Se déconnecter* n'efface que le cookie de l'appareil : seul *Déconnecter les autres appareils* révoque.
- [ ] Une **fiche TMDB fixée à la main** ne se retire pas, on en fait une autre. Le marqueur `{tmdb-…}` n'est pas écrit dans le nom du dossier : pour que `media_library` voie le même choix, il se tape à la main.
- [ ] Les sous-titres **VobSub (DVD)** ne sont pas affichés : seuls les PGS (Blu-ray) le sont. ffmpeg ne sait pas les extraire tels quels (pas de muxer `.idx`/`.sub`) : il faudrait lire les paquets du `.mkv` soi-même, avec la palette de leur `CodecPrivate`. Contournement : un fichier `.srt` à côté de la vidéo.
- [ ] Les PGS ne se **choisissent jamais d'office** à l'ouverture d'un film (l'extraction peut durer) : il faut les choisir une fois dans le menu « Sous-titres » ; la langue choisie est ensuite retenue, et un PGS de cette langue est repris s'il n'y a pas de piste texte.
- [ ] **Un élément disparu laisse ses références** : le scan efface ses lignes `items` et `entries`, mais ses clés `kv` restent (`enrich:<bibliothèque>:<id>` avec la note TMDB, `tmdb-pin:<id>`, `poster:<id>`, `dismiss:<compte>:<id>`). Elles resservent si le dossier revient au même chemin, mais un élément renommé ou déplacé change d'identifiant et laisse les anciennes clés sans emploi. À faire : les retirer (et leurs fichiers d'affiche) pour les éléments absents depuis un certain temps, avec un délai comme `ORPHAN_PROGRESS_DAYS` pour qu'un volume démonté n'efface rien. À vérifier d'abord : `artist:<mbid>`, `artistlinks:<mbid>`, `wanted:<bibliothèque>` et les listes de lecture.

## Idées mises de côté

À reprendre seulement si l'envie vient.

- [ ] **Affiches TMDB** : les récupérer pour les films qui n'en ont pas, ou les proposer dans *Changer l'affiche*.
- [ ] **Écrire le marqueur `{tmdb-…}`** depuis le hub quand le volume n'est pas en lecture seule.
- [ ] **Accueil** : « À revoir » (suggestions calculées par compte), un accueil par personne (« depuis ta dernière visite »).
- [ ] **`ARTISTS` dans `media_library`** : le scan lit les tags lui-même (`hub/scan/music.py`, `read_info`) parce que `track_info` ne rend pas encore `ARTISTS` ; le jour où il le fera, `credits` pourra disparaître du hub.
- [ ] **Mesurer le JavaScript** de l'interface : aucun outil de couverture ni de complexité n'existe pour `hub/web`. Une mesure grossière (3 novembre 2026) donnait 10 fonctions sur 862 à 15 décisions ou plus, presque toutes du dispatch de touches (`core.js`, lecteurs vidéo et de livres) ; à reprendre avec un vrai outil.

## Pas relu

- Le dépôt `media_library` (modifications non commitées côté développement).
- `.claude/skills/`, volontairement laissé de côté.
