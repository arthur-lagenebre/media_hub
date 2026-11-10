# Essais sur place

Ce que les tests automatiques ne peuvent pas vérifier : ils tournent dans un Chromium de bureau, sur une médiathèque factice, avec le ffmpeg de la machine de développement. Ce fichier est une séance à dérouler, un appareil à la fois, en cochant. Ce qui reste ouvert après la séance va dans [TODO.md](TODO.md) ; ce qui a été tranché, dans [decisions.md](decisions.md).

Rien ici ne bloque le reste du projet. Les séances 1 à 4 valent la peine d'être faites ; la séance 5 (télé) est facultative.

## Avant de commencer

- [ ] Le hub déployé est à jour (`GET /api/state` : `version` doit être celle de `hub/__init__.py`).
- [ ] Une clé `TMDB_KEY` est réglée (séance 1).
- [ ] Un compte administrateur, et un compte de test **sans** le droit « Envoyer » (séance 2).
- [ ] Un accès SSH au NAS (séance 2).
- [ ] Un fichier de 1 à 2 Go pour l'envoi (n'importe quel `.mkv`).

**Si quelque chose ne marche pas**, rapporte pour chaque point : l'appareil et le navigateur, ce que tu as fait, ce que tu attendais, ce que tu as vu, et la durée ou la ligne d'erreur quand le point demande une mesure. Un comportement déjà listé dans « Limites connues » de [TODO.md](TODO.md) n'est pas un bug nouveau : note seulement si ça gêne plus que prévu.

---

## Séance 1 : depuis le PC (≈ 1 h, dont l'attente des tâches)

Navigateur de bureau, administrateur, clé TMDB réglée.

### Notes TMDB

- [ ] *Gestion* > « Sagas (TMDB) » sur **toute** la bibliothèque de films. **Mesure à noter : la durée** (une requête par film étiqueté, sans cache).
- [ ] Lancer un rescan. **Attendu :** les notes restent.
- [ ] Trier « Mieux notés », en vue *Sagas* puis en vue *Films*. **Attendu :** une saga se range à la note de son meilleur film.

### Films manquants d'une personne

- [ ] *Gestion* > « Détail par bibliothèque » > « Films manquants » de la bibliothèque de films. **Mesures à noter : la durée, et la taille de `HUB_DATA/tmdb`** (`du -sh` sur le NAS). L'estimation de [deploy/README.md](../deploy/README.md) est « une heure ou plus la première fois, plusieurs centaines de Mo », non mesurée.
- [ ] Ouvrir la page d'un réalisateur (`#/person?n=`), puis celle d'un acteur : **attendu :** les films manquants en cases grisées, du plus récent au plus ancien.
- [ ] Sur une personne pas encore enrichie : « Chercher ses films manquants ». **Attendu :** les cases grisées apparaissent.
- [ ] Un bouton par bibliothèque (« Films des personnes (TMDB) ») sur une bibliothèque de séries ou d'animations. **Attendu :** une catégorie à la fois, sans toucher aux autres.

### Piles d'affiches à cache froid

- [ ] Vider le cache d'images du hub : le dossier `img/` sous `HUB_DATA` (sur le NAS, `/mnt/tank/apps/media/media_hub-data/img`), dont on supprime les fichiers. Puis ouvrir une bibliothèque de séries, de livres ou de BD. **Mesure à noter : la durée de ce premier affichage** (`STACK_MAX = 3` affiches lues par série). Ne pas confondre avec `tmdb/`, qui garde les affiches téléchargées de TMDB.
- [ ] Pendant ce temps, ouvrir une autre page dans un second onglet. **Attendu :** elle ne ralentit pas.

### Téléchargements

- [ ] Télécharger un album ou une saison dans **Chrome**, puis dans **Firefox**. **Attendu :** le navigateur demande d'autoriser les téléchargements multiples la première fois, puis les enchaîne.
- [ ] Pendant un téléchargement long, naviguer dans le hub. **Attendu :** rien ne se bloque, et le champ `streams` de `GET /api/state` (avec `max_streams`, la valeur de `HUB_MAX_STREAMS`) ne bouge pas : un téléchargement ne compte pas dans `HUB_MAX_STREAMS`.

### Reprise en « Son converti »

Un MKV H.264 avec images B et son DTS (par exemple « La reine des damnés »), lu sur le serveur avec le ffmpeg 7.0.2 de `deploy/fetch-ffmpeg.sh`.

- [ ] Lire, quitter, reprendre. **Attendu :** image, son et sous-titres calés.
- [ ] Sauter aux flèches, puis changer de piste audio. **Attendu :** toujours calés.

### Sous-titres PGS

Un vrai remux Blu-ray.

- [ ] Choisir la piste PGS dans le menu « Sous-titres » (elle ne se choisit jamais d'office). **Mesure à noter : la durée de la première extraction** sur un fichier de 30 Go (délai de 15 minutes).
- [ ] **Attendu :** le calage dans le temps est bon.
- [ ] Sur un `.mkv` recadré : la position sur l'image. **Attendu :** les sous-titres sont à leur place (le canvas suit la taille de l'écran du Blu-ray, pas celle de l'image recadrée).

---

## Séance 2 : sur le NAS, la boîte de réception (≈ 30 min)

Suivre la procédure de [deploy/README.md](../deploy/README.md), « Essai à faire sur place (Funnel, 1 à 2 Go) ». Le serveur ne limite pas la taille d'un envoi : seul l'espace libre (`HUB_INBOX_KEEP_FREE`) refuse.

- [ ] **Espace avant :** `docker exec media-hub df -m /data` et, dans TrueNAS, l'espace libre et le quota du dataset `media_hub-data`. **Attendu :** les deux concordent. Sinon, note de combien.
- [ ] **Envoi de 1 à 2 Go à travers Funnel**, au `curl` puis depuis la page « Envoyer ». **Mesures à noter : le débit, et si l'envoi tient jusqu'au bout.** Les délais de Funnel ne se configurent pas.
- [ ] Le fichier est dans `inbox/<nom du compte>/`, à la bonne taille, sans `.part`.
- [ ] **Envoi annulé** dans le navigateur, en cours de route. **Attendu :** aucun `.part` ne reste dans `inbox/<nom du compte>/` (compter jusqu'à 3 minutes, voir la procédure).
- [ ] **Faute de place :** `truncate -s 2T gros.mkv`, puis l'envoyer. **Attendu :** 507 en quelques secondes, avant le moindre octet lu, et rien dans `inbox/`.
- [ ] **Compte sans le droit « Envoyer »** : essayer d'envoyer. **Attendu :** 403, message affiché, rien d'écrit.
- [ ] **Refus d'un gros fichier** (le compte sans droit avec le fichier de 1 à 2 Go). **À observer :** le navigateur affiche-t-il le 403, ou une erreur réseau ? Le serveur n'avale que 64 Mo ou 5 s, au-delà il coupe : c'est la limite connue. Ce point décide si elle mérite d'être traitée.
- [ ] **Coupure réseau** pendant un envoi (couper le Wi-Fi du poste). **À observer :** rien n'est gardé ; la reprise n'existe pas, un envoi coupé se refait en entier. Ce point dit si la reprise (`Content-Range`) est nécessaire.

### Boîte de réception sur le dataset HubReception (facultatif)

À faire seulement si le dataset « HubReception » est activé (le montage et `HUB_INBOX: /inbox` décommentés **ensemble** dans `docker-compose.yml`). La procédure complète est dans [deploy/README.md](../deploy/README.md), « Boîte de réception ». Ce que les tests ne peuvent pas voir : un vrai volume ZFS, SMB, les ACL.

- [ ] **Création, renommage, suppression depuis le conteneur :** `docker exec media-hub sh -c 'touch /inbox/.essai && mv /inbox/.essai /inbox/.essai2 && rm /inbox/.essai2' && echo OK`. **Attendu :** `OK`. Sinon, noter l'erreur (droits, ACL, dataset en lecture seule).
- [ ] **Pas d'« ATTENTION » dans les journaux :** `docker logs media-hub 2>&1 | grep -n "ATTENTION"`. **Attendu :** rien qui parle de la boîte de réception (ni « n'est pas utilisable », ni « recoupe », ni « n'est pas un point de montage »).
- [ ] **Envoi réel** depuis la page « Envoyer », puis `ls -l /mnt/tank/HubReception/`. **Attendu :** un dossier au **nom du compte** (« admin », « Léa »…), le fichier à la bonne taille, aucun `.part`, rien dans `media_hub-data/inbox/`.
- [ ] **Renommage et suppression par SMB :** depuis le partage de `HubReception`, renommer puis supprimer le fichier déposé. **Attendu :** les deux réussissent (entrée d'ACL avec héritage pour le compte SMB, voir le README). Les dossiers créés par le hub appartiennent à root.
- [ ] **Dossier de compte renommé ou supprimé par SMB, hub arrêté** (déconseillé hub en marche) : redémarrer. **Attendu :** l'ancien nom apparaît dans la liste de l'administrateur avec « (compte supprimé) » ; l'envoi suivant du compte recrée un dossier neuf, rien n'est perdu.
- [ ] **Faute de place sur ce dataset :** mettre temporairement `HUB_INBOX_KEEP_FREE` au-dessus de l'espace libre du dataset (`docker exec media-hub df -m /inbox`), redéployer, envoyer un petit fichier. **Attendu :** 507 avec « plus assez de place sur le serveur pour ce fichier », rien d'écrit. Remettre `10240` et redéployer.
- [ ] **Migration des anciens dossiers :** avant le premier démarrage de cette version, noter `ls media_hub-data/inbox/` (dossiers `default` et hexadécimaux) ; après : les dossiers portent le nom des comptes, les fichiers y sont, `grep "boîte de réception"` dans les journaux est vide ou explique chaque dossier resté sous son identifiant. Relancer le hub : rien ne bouge. Si le dataset est activé ensuite, déplacer le contenu de l'ancienne boîte hub arrêté (procédure du README) et vérifier que les fichiers se retrouvent sous les noms de comptes.
- [ ] **Non vérifié, à noter :** si `DAC_OVERRIDE` (la capacité gardée par le conteneur, qui tourne en root avec `cap_drop: ALL`) passe outre une **ACL NFSv4 ZFS** (préréglage SMB du dataset) pour créer, écrire et renommer. L'essai du premier point le tranche : s'il échoue sur un dataset aux ACL SMB, régler l'ACL du dataset, sans élargir les capacités du conteneur.

---

## Séance 3 : sur le téléphone (≈ 20 min)

Idéalement un téléphone étroit, une fois en français et une fois en anglais.

- [ ] **Pop-up au tactile :** ouvrir la file d'attente et le choix de films. **Attendu :** la file d'attente tient sur la largeur ; les boutons du pied sont de taille égale.
- [ ] **Fiche d'un artiste :** la croix de retrait à côté de chaque lien se touche sans viser à côté ; « Ajouter un lien » ne se laisse pas cacher par le clavier.
- [ ] **Interface en anglais :** les boutons des Paramètres, la barre du lecteur et les fenêtres de droits ne débordent pas (les tests ne vérifient que l'absence de texte manquant).
- [ ] **Disque de la note TMDB** sur une vignette : il ne gêne ni le bouton « … » ni la barre de progression, et reste lisible.

---

## Séance 4 : sur la tablette (≈ 15 min)

La liseuse en double page.

- [ ] En paysage, le bouton « Double page » apparaît et active la double page ; en portrait, elle est coupée.
- [ ] Un livre : la couverture seule, puis les couples de pages. Si le livre commence par une double page, « Décaler d'une page » recale les couples.
- [ ] Le sens de lecture est retenu **par série**.
- [ ] Sur un NAS lent : une vue ne s'affiche qu'une fois ses deux images chargées, et les tours de page demandés entre-temps sont rejoués ensuite.

---

## Séance 5 : sur la télé (facultatif)

Android TV, Fire TV, webOS. La télé n'est pas prioritaire : cette séance peut attendre, ou ne jamais avoir lieu.

- [ ] **À la télécommande :** les sagas, l'éditeur de sagas, le sélecteur de films, les cartes grisées, le bouton « … » des films, des sagas, des séries et des animations, les photos d'artistes, la liseuse et le lecteur.
- [ ] **Pop-up :** Gauche / Droite passent d'un bouton à l'autre.
- [ ] **Films manquants** d'une personne, à la télécommande.
- [ ] **Liseuse en double page :** le focus des boutons « Double page » et « Décaler d'une page ».
- [ ] **Interface en anglais :** rien ne déborde (barre du lecteur, Paramètres).
- [ ] **Disque de la note TMDB :** lisible de loin.
- [ ] **Sous-titres PGS :** la fluidité du décodage dans le navigateur de la télé.
