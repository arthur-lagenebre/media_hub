"""Sous-titres : ceux posés à côté de la vidéo (.srt, .vtt, .ass) et ceux que le .mkv porte, rendus en WebVTT.

Le lecteur les affiche lui-même (voir app.js) : les repères de temps sont ceux du film, même quand la lecture repart d'un autre point. Les sous-titres en image (PGS, VobSub) ne se convertissent pas en texte : ils sont listés, mais grisés.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SIDE_EXTS = {".srt", ".vtt", ".ass", ".ssa"}
TIMESTAMP = re.compile(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})")
LANG_NAMES = {"fr": "Français", "fre": "Français", "fra": "Français", "en": "English", "eng": "English", "es": "Español", "spa": "Español", "de": "Deutsch", "ger": "Deutsch", "deu": "Deutsch",
              "it": "Italiano", "ita": "Italiano", "ja": "日本語", "jpn": "日本語", "pt": "Português", "por": "Português", "und": ""}


def language(code):
    return LANG_NAMES.get((code or "").lower(), code or "")


def side_files(video):
    """[{'index', 'path', 'label', 'lang', 'forced'}] : les fichiers de sous-titres qui portent le nom de la vidéo (« Film.fr.forced.srt »)."""
    video = Path(video)
    found = []
    try:
        siblings = sorted(video.parent.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return found
    for path in siblings:
        if path.suffix.lower() not in SIDE_EXTS or not path.name.casefold().startswith(video.stem.casefold()):
            continue
        tail = path.stem[len(video.stem):].strip(" ._-")
        words = [w for w in re.split(r"[ ._-]+", tail) if w]
        forced = any(w.lower() in ("forced", "force", "forcé") for w in words)
        sdh = any(w.lower() in ("sdh", "cc", "hi") for w in words)
        lang = next((w.lower() for w in words if w.lower() in LANG_NAMES), "")
        label = " · ".join(x for x in (language(lang) or (tail if not lang else ""), "Forcé" if forced else "", "SDH" if sdh else "") if x) or "Sous-titres"
        found.append({"index": len(found), "path": path, "label": label, "lang": lang, "forced": forced})
    return found


def decode(data):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def srt_to_vtt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    return "WEBVTT\n\n" + TIMESTAMP.sub(r"\1.\2", text)


def ffmpeg_vtt(ffmpeg, source, stream=None, timeout=120):
    """WebVTT d'une piste du fichier (ou de sa première piste, pour un .ass) via ffmpeg."""
    if not ffmpeg:
        raise RuntimeError("ffmpeg est introuvable : impossible de convertir ces sous-titres.")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", str(source)]
    cmd += ["-map", f"0:s:{stream}"] if stream is not None else []
    cmd += ["-f", "webvtt", "pipe:1"]
    done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip()[:200] or "conversion impossible")
    return done.stdout.decode("utf-8", "replace")


def side_vtt(ffmpeg, path):
    path = Path(path)
    if path.suffix.lower() == ".vtt":
        return decode(path.read_bytes())
    if path.suffix.lower() == ".srt":
        return srt_to_vtt(decode(path.read_bytes()))
    return ffmpeg_vtt(ffmpeg, path)
