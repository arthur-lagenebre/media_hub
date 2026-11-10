---
paths:
  - "hub/web/**"
---

# Interface (hub/web/)

La liste complète est dans `.claude/agents/hub-ui.md` et `docs/popups.md` ; ce qui suit ne se transgresse pas :

- **Aucun framework, bundler, npm, bibliothèque externe ni CDN.** Style JS des fichiers existants (fonctions et `var`, pas de modules ES ni de `async/await` là où le fichier n'en a pas) : la télé a un vieux navigateur.
- **Français partout** dans ce que voit l'utilisateur (libellés, `aria-label`, toasts).
- **Télécommande d'abord** : tout ce qui est cliquable s'atteint aux flèches, s'active par Entrée, et Retour remonte d'un niveau ; le focus est visible de loin. Une zone atteignable à la souris seule est un défaut.
- **Un code pour quatre appareils** (`data-device` : `pc`, `phone`, `tablet`, `tv`).
- **Données non fiables** (titres, synopsis, noms d'artistes) : `textContent` ou l'échappement du projet, jamais `innerHTML` brut.
- **La barre audio ne s'arrête jamais** pendant la navigation : ne pas recréer `<audio>`.
- **Un pop-up passe par sa fabrique** (`H.menu`, `H.confirm`, `H.form`, `H.prompt`, `H.box`), jamais un `div role="dialog"` à la main.
- Une donnée que l'API ne fournit pas : décrire la route à ajouter, ne pas modifier `hub/server/`.
