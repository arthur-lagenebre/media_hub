#!/bin/sh
# Télécharge un ffmpeg statique (x86_64) dans le dossier monté en /opt/ffmpeg.
# À lancer une fois, dans le shell du NAS :   sh fetch-ffmpeg.sh /mnt/tank/apps/media/ffmpeg
set -eu
dest="${1:?usage: fetch-ffmpeg.sh <dossier>}"
mkdir -p "$dest"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fL -o "$tmp/ff.tar.xz" https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xJf "$tmp/ff.tar.xz" -C "$tmp"
cp "$tmp"/ffmpeg-*-static/ffmpeg "$tmp"/ffmpeg-*-static/ffprobe "$dest"/
chmod 755 "$dest/ffmpeg" "$dest/ffprobe"
"$dest/ffmpeg" -version | head -1
