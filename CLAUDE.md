# media_hub

Serveur de médiathèque (films, séries, musique, livres) : un seul processus Python, **bibliothèque standard uniquement** (Pillow facultatif), interface web sans framework servie telle quelle. Il *lit* ce que `media_library` écrit dans les fichiers et n'écrit jamais dans la médiathèque. Présentation, réglages `HUB_*` et routes : [README.md](README.md) ; choix écartés : [docs/decisions.md](docs/decisions.md) (à lire avant de « corriger » ce qui a l'air d'une dette).

## Commandes

```bash
python -m unittest tests.test_database tests.test_scan   # les modules touchés : /check
python -m unittest discover -s tests                     # la suite complète (~30 min) : /ship seulement
HUB_SRV=<dossier> HUB_DATA=<dossier-données> python -m hub   # http://127.0.0.1:8080
```

`media_library` doit être **voisin** de `media_hub` (ou désigné par `HUB_REPO`), sinon aucun test ne démarre. Les tests tournent sur une médiathèque factice (`tests/fixtures.build_library`), jamais sur la vraie. Un test sauté (Playwright, ffmpeg) n'est pas un test réussi : le dire.

## Où est quoi

- `hub/` : serveur (`server/app.py` routes, `server/jobs.py` tâches de fond, `db.py` SQLite, `scan/`, `media/`, `enrich.py`, `config.py`).
- `hub/web/` : interface (HTML, CSS, JS ; espace de noms global `H` dans `core.js`).
- `tests/`, `deploy/` (compose du NAS), `docs/` (API, décisions, TODO, pop-ups, agents).

Le serveur (`hub-server`) et l'interface (`hub-ui`) sont deux domaines : ne modifie pas l'un en travaillant sur l'autre, décris ce qu'il faut. Les consignes détaillées de chaque domaine sont dans `.claude/rules/` (chargées selon les fichiers touchés) et dans `.claude/agents/`.

## Flux git

- Branches thématiques en kebab-case (`accueil`, `titre-invisible`), **jamais de commit direct sur `main`**, fusion par pull request **faite par l'utilisateur**. Pas de PR sans son accord.
- Messages de commit **en français** : `<Sujet> : <ce qui change>`, puis pourquoi / quoi / preuve. Ajouter les fichiers un par un.
- `/ship` livre (tests complets, version, commits, PR) ; `/merged` range après une fusion ; `/refresh` nettoie les branches fusionnées ; `/worktree <lot>` isole une session parallèle ; `/check` lance les tests des modules touchés.
- **Une session par lot = un worktree** (`../media_hub-<lot>`) : jamais de `checkout`, `stash` ni `clean` dans un dossier où une autre session a des modifications.
- Un hook (`.claude/hooks/guard-git.py`) refuse push forcé, `--no-verify`, `git branch -D`, `git add -A`, commit sur `main` et `gh pr merge`. Un autre demande confirmation avant de lancer un agent d'implémentation entre 23 h et 6 h.

## Documentation à tenir à jour

Toute route nouvelle : tableau « Interface HTTP » du README, `docs/api.md`, `docs/TODO.md`. Tout réglage `HUB_*` nouveau : tableau « Réglages » du README. `__version__` (`hub/__init__.py`) se règle une fois par PR, selon `/ship`.

## Langue

Ce que voit l'utilisateur (interface, messages d'erreur, commits, PR, comptes rendus) est **en français**. Les noms de tests et d'identifiants restent en anglais, comme dans le code existant.
