# Image de media_hub (facultative : le compose de ce dossier monte les sources dans python:3.13-slim).
# Construite par .github/workflows/image.yml avec media_library comme contexte nommé :
#   docker buildx build -f deploy/Dockerfile --build-context media_library=../media_library .
FROM python:3.13-slim

# ffmpeg (remux, transcodage, sous-titres) est cherché dans le PATH ; Pillow est facultatif (pochettes plus légères).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --disable-pip-version-check Pillow

COPY --from=media_library . /app/media_library
COPY hub /app/media_hub/hub
WORKDIR /app/media_hub

ENV PYTHONUNBUFFERED=1 \
    HUB_REPO=/app/media_library \
    HUB_SRV=/srv \
    HUB_DATA=/data \
    HUB_BIND=0.0.0.0 \
    HUB_PORT=8080
VOLUME /data
EXPOSE 8080

# Le serveur écoute tout le réseau : sans compte (base vide) ni HUB_PASSWORD pour créer le premier, il refuse de démarrer plutôt que de s'ouvrir à tous.
CMD ["sh", "-c", "[ -n \"$HUB_PASSWORD\" ] || [ -f \"$HUB_DATA/hub.db\" ] || { echo 'media_hub : définis HUB_PASSWORD (premier compte administrateur), le serveur écoute tout le réseau.' >&2; exit 1; }; exec python -m hub"]
