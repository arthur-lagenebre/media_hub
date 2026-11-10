---
paths:
  - "hub/**/*.py"
  - "!hub/web/**"
---

# Serveur Python (hub/)

La liste complète et commentée est dans `.claude/agents/hub-server.md` ; ce qui suit ne se transgresse pas :

- **Bibliothèque standard seulement.** Pillow reste derrière un `try/except ImportError`.
- **Lecture seule sur `HUB_SRV`** : le hub n'écrit que sous `HUB_DATA` (exceptions admises : nettoyage des fichiers parasites, `cover.{ext}`, photos d'artistes).
- **Sécurité** : un chemin venu d'une URL se résout puis se vérifie sous sa racine ; toute route est authentifiée ; `hmac.compare_digest` pour mots de passe et jetons ; SQL **toujours paramétré** ; `subprocess` avec une liste, jamais `shell=True`.
- **Base** : un changement de schéma monte `SCHEMA_VERSION` avec sa migration dans `MIGRATIONS`, sans perdre progression ni playlists. Un scan n'efface jamais les clés `enrich:`.
- **Erreurs** : `HttpError` avec un message en français ; un fichier illisible ne fait pas échouer tout le scan.
- **ffmpeg** : respecter `HUB_MAX_STREAMS`, arrêter le processus quand le client part, plan de lecture *direct* puis *remux* puis *transcodage*.
- Un champ retiré ou renommé dans l'API fait monter `API_VERSION`.
