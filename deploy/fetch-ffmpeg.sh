#!/bin/sh
# Télécharge un ffmpeg statique (x86_64) dans le dossier monté en /opt/ffmpeg.
# À lancer une fois, dans le shell du NAS :   sh fetch-ffmpeg.sh /mnt/tank/apps/media/ffmpeg
#
# La version est fixée et son archive vérifiée par sa somme SHA-256 avant d'être ouverte : un fichier altéré ou tronqué est refusé.
# Pour changer de version : mettre à jour VERSION et SHA256 ensemble. La somme se calcule sur l'archive téléchargée (`sha256sum ffmpeg-<version>-amd64-static.tar.xz`) ;
# le site publie son MD5 à côté (<adresse>.md5, pour la « release » courante) : le recouper avec `md5sum`.
set -eu
VERSION=7.0.2
SHA256=abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67

dest="${1:?usage: fetch-ffmpeg.sh <dossier>}"
mkdir -p "$dest"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="ffmpeg-$VERSION-amd64-static.tar.xz"
curl -fL -o "$tmp/$archive" "https://johnvansickle.com/ffmpeg/releases/$archive"
echo "$SHA256  $tmp/$archive" | sha256sum -c - || { echo "Somme SHA-256 de $archive inattendue : rien n'est installé." >&2; exit 1; }
tar -xJf "$tmp/$archive" -C "$tmp"
cp "$tmp"/ffmpeg-*-static/ffmpeg "$tmp"/ffmpeg-*-static/ffprobe "$dest"/
chmod 755 "$dest/ffmpeg" "$dest/ffprobe"
"$dest/ffmpeg" -version | head -1
