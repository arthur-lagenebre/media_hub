"""Comment jouer un fichier : ce qu'il contient (ffprobe), ce que le client sait lire, et ce qu'il faut faire entre les deux.

Trois façons, de la moins chère à la plus chère :
  direct     le fichier tel quel, avec des requêtes Range (aucun calcul côté NAS) ;
  remux      l'image est recopiée, seul le son est converti en AAC (un E-AC-3 ou un DTS ne passe pas dans un navigateur) : presque gratuit ;
  transcode  l'image est ré-encodée en H.264 : quand le codec n'est pas lisible, ou que le débit demandé est plus bas que celui du fichier.

Le client annonce ce qu'il sait lire (`caps`), le serveur décide. Un i3-13100F sans carte vidéo capable d'encoder fait du transcodage logiciel : un flux 1080p passe sans peine, un 4K HEVC est à éviter (le plan le dit).
"""

from __future__ import annotations

import json
import subprocess

# Codec ffprobe -> nom que le client utilise dans ses capacités
VIDEO_NAMES = {"h264": "h264", "hevc": "hevc", "vp9": "vp9", "av1": "av1", "vp8": "vp8", "mpeg4": "mpeg4"}
AUDIO_NAMES = {"aac": "aac", "mp3": "mp3", "opus": "opus", "vorbis": "vorbis", "flac": "flac", "ac3": "ac3", "eac3": "eac3", "dts": "dts", "truehd": "truehd", "alac": "alac"}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd", "subviewer"}
CONTAINER_FAMILY = (("matroska", "mkv"), ("webm", "webm"), ("mov", "mp4"), ("mp4", "mp4"), ("avi", "avi"))


class ProbeError(Exception):
    pass


def run_ffprobe(ffprobe, path):
    if not ffprobe:
        raise ProbeError("ffprobe est introuvable : installe ffmpeg sur le serveur (voir deploy/README.md).")
    try:
        done = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProbeError(f"ffprobe a échoué : {e}")
    if done.returncode != 0:
        raise ProbeError(done.stderr.decode("utf-8", "replace").strip()[:300] or "ffprobe a échoué")
    return summarize(json.loads(done.stdout or b"{}"))


def keyframe_at(ffprobe, path, at):
    """Où une image recopiée commence vraiment quand on reprend à `at` secondes : l'image clé sur laquelle le fichier se place (celle qui contient `at`), à la milliseconde. `at` si ffprobe ne répond pas.

    Une image recopiée ne peut commencer que sur une image clé : demander `at` tombe sur la précédente, parfois plusieurs secondes avant, sans que personne le dise. Le client doit compter sa position (horloge, sous-titres) depuis cette image-là."""
    if at <= 0 or not ffprobe:
        return max(0.0, at)
    try:
        done = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", "-read_intervals", f"{at:.3f}%+#1", str(path)],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return at
    first = (done.stdout.decode("utf-8", "replace").splitlines() or [""])[0]            # le premier paquet lu après le placement : l'image clé
    when, _, flags = first.partition(",")
    try:
        return round(float(when), 3) if done.returncode == 0 and "K" in flags else at
    except ValueError:
        return at


def _fps(text):
    try:
        top, _, bottom = (text or "0/1").partition("/")
        return round(float(top) / float(bottom or 1), 3)
    except (ValueError, ZeroDivisionError):
        return 0


def _tags(stream):
    return {k.lower(): v for k, v in (stream.get("tags") or {}).items()}


def _container(names):
    return next((family for key, family in CONTAINER_FAMILY if key in names), names[0])


def _depth(video):
    """Les bits par composante : ceux que le fichier annonce, sinon 10 si le format de pixel le dit, sinon 8."""
    bits = video.get("bits_per_raw_sample") or ""
    return int(bits) if str(bits).isdigit() else (10 if "10" in (video.get("pix_fmt") or "") else 8)


def _video(video):
    return {
        "codec": VIDEO_NAMES.get(video.get("codec_name"), video.get("codec_name") or ""), "profile": video.get("profile") or "",
        "width": int(video.get("width") or 0), "height": int(video.get("height") or 0), "fps": _fps(video.get("avg_frame_rate")),
        "depth": _depth(video), "pix_fmt": video.get("pix_fmt") or "", "hdr": video.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        "bitrate": int(video.get("bit_rate") or 0),
    }


def _audio(index, s):
    tags, disp = _tags(s), s.get("disposition") or {}
    return {
        "index": index, "codec": AUDIO_NAMES.get(s.get("codec_name"), s.get("codec_name") or ""), "channels": int(s.get("channels") or 2),
        "lang": tags.get("language", ""), "title": tags.get("title", ""), "default": bool(disp.get("default")),
        "commentary": bool(disp.get("comment")) or bool(disp.get("visual_impaired")),
    }


def _sub(index, s):
    tags, disp = _tags(s), s.get("disposition") or {}
    codec = s.get("codec_name") or ""
    return {
        "index": index, "codec": codec, "lang": tags.get("language", ""), "title": tags.get("title", ""),
        "forced": bool(disp.get("forced")), "default": bool(disp.get("default")), "text": codec in TEXT_SUBS,
    }


def _format(fmt):
    return {"container": _container((fmt.get("format_name") or "").split(",")), "duration": float(fmt.get("duration") or 0), "bitrate": int(fmt.get("bit_rate") or 0),
            "size": int(fmt.get("size") or 0)}


def _main_video(streams):
    """Le flux d'image du film ; une jaquette embarquée (attached_pic) n'en est pas un."""
    return next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)


def summarize(data):
    """La sortie de ffprobe réduite à ce qui décide de la lecture. Ce résumé est ce qu'on garde dans la base."""
    streams = data.get("streams") or []
    video = _main_video(streams)
    return {**_format(data.get("format") or {}), "video": _video(video) if video else None,
            "audio": [_audio(i, s) for i, s in enumerate(x for x in streams if x.get("codec_type") == "audio")],
            "subs": [_sub(i, s) for i, s in enumerate(x for x in streams if x.get("codec_type") == "subtitle")]}


def default_audio(probe):
    """La piste que le lecteur choisirait seul : la piste par défaut qui n'est pas un commentaire, sinon la première."""
    tracks = probe["audio"]
    return next((t for t in tracks if t["default"] and not t["commentary"]), tracks[0] if tracks else None)


def _chosen_track(probe, audio):
    """La piste demandée si elle existe, sinon celle que le lecteur choisirait seul."""
    track = next((t for t in probe["audio"] if t["index"] == audio), None) if audio is not None else None
    return track or default_audio(probe)


def _video_readable(video, caps, vcaps):
    return bool(video) and video["codec"] in vcaps and (video["depth"] <= 8 or f"{video['codec']}10" in vcaps) and not (video["hdr"] and not caps.get("hdr"))


def _check_video(video, caps):
    """(lisible tel quel ?, raisons contraires) : le codec, puis les 10 bits et le HDR, que le lecteur doit annoncer."""
    vcaps = set(caps.get("video") or [])
    ok = _video_readable(video, caps, vcaps)
    if video and video["codec"] not in vcaps:
        return ok, [f"image {video['codec']} non lisible par ce lecteur"]
    if video and not ok:
        return ok, ["image 10 bits / HDR non lisible par ce lecteur"]
    return ok, []


def _check_limits(probe, video, caps, max_kbps, max_height):
    """(trop haute ?, trop lourde ?, raisons) : la hauteur demandée, ou à défaut celle du lecteur ; le débit demandé, à 5 % près."""
    limit_h = max_height or caps.get("height") or 0
    too_tall = bool(video and limit_h and video["height"] > limit_h)
    too_heavy = bool(max_kbps and probe["bitrate"] and probe["bitrate"] / 1000 > max_kbps * 1.05)
    why = []
    if too_tall:
        why.append(f"image {video['height']} p au-delà de {limit_h} p")
    if too_heavy:
        why.append(f"débit {round(probe['bitrate'] / 1e6, 1)} Mb/s au-delà de {round(max_kbps / 1000, 1)} Mb/s demandés")
    return too_tall, too_heavy, why


def _check_audio(track, caps):
    """(lisible tel quel ?, raisons contraires) : le codec, puis le nombre de voies."""
    channels = caps.get("channels") or 2
    acaps = set(caps.get("audio") or [])
    ok = track is None or (track["codec"] in acaps and track["channels"] <= channels)
    if track and track["codec"] not in acaps:
        return ok, [f"son {track['codec']} non lisible par ce lecteur"]
    if track and not ok:
        return ok, [f"son {track['channels']} voies, le lecteur en prend {channels}"]
    return ok, []


def _direct_or_remux(probe, caps, track, audio_ok, why):
    """L'image se lit telle quelle : direct si le conteneur et la piste aussi, sinon on recopie l'image dans un conteneur lisible."""
    chosen = track["index"] if track else None
    container_ok = probe["container"] in set(caps.get("containers") or ["mp4", "webm"])
    is_default = track is None or track is default_audio(probe)
    if container_ok and audio_ok and is_default:
        return {"mode": "direct", "audio": chosen, "why": [], "copy_video": True, "copy_audio": True}
    if not container_ok:
        why.append(f"conteneur {probe['container']} non lu par ce lecteur")
    if not is_default:
        why.append("autre piste audio que celle par défaut")
    return {"mode": "remux", "audio": chosen, "why": why, "copy_video": True, "copy_audio": audio_ok}


def plan(probe, caps, audio=None, max_kbps=0, max_height=0):
    """Décide comment jouer. `caps` : {"video": [...], "audio": [...], "containers": [...], "channels": n, "height": n}.

    Rend {"mode": direct|remux|transcode, "audio": index, "why": [raisons], "copy_video": bool, "copy_audio": bool}.
    """
    caps = caps or {}
    video = probe.get("video")
    track = _chosen_track(probe, audio)
    video_ok, why = _check_video(video, caps)
    too_tall, too_heavy, limits = _check_limits(probe, video, caps, max_kbps, max_height)
    audio_ok, sound = _check_audio(track, caps)
    why = why + limits + sound
    if not video_ok or too_tall or too_heavy:
        return {"mode": "transcode", "audio": track["index"] if track else None, "why": why, "copy_video": False, "copy_audio": False}
    return _direct_or_remux(probe, caps, track, audio_ok, why)


def _video_args(probe, decision, max_kbps, max_height, preset):
    """L'image : recopiée (étiquetée hvc1 si c'est du HEVC, pour les lecteurs d'Apple), ou ré-encodée en H.264 8 bits, réduite seulement si elle dépasse la hauteur demandée."""
    video = probe.get("video") or {}
    if decision["copy_video"]:
        return ["-c:v", "copy"] + (["-tag:v", "hvc1"] if video.get("codec") == "hevc" else [])
    return _x264_args(video, max_kbps, max_height, preset)


def _x264_args(video, max_kbps, max_height, preset):
    source = video.get("height") or 0
    wanted = max_height or source
    filters = [f"scale=-2:{wanted}"] if source > wanted else []
    args = ["-c:v", "libx264", "-preset", preset, "-crf", "21", "-profile:v", "high", "-level", "4.1", "-g", "48", "-sc_threshold", "0", "-vf", ",".join(filters + ["format=yuv420p"])]
    return args + (["-maxrate", f"{int(max_kbps * 0.92)}k", "-bufsize", f"{int(max_kbps * 2)}k"] if max_kbps else [])


def _audio_args(probe, decision, channels):
    """Le son : recopié, ou converti en AAC avec au plus autant de voies que la source et que le lecteur."""
    if decision["copy_audio"]:
        return ["-c:a", "copy"]
    src = next((t for t in probe["audio"] if t["index"] == decision["audio"]), {"channels": 2})
    out_channels = max(1, min(src["channels"], channels))
    return ["-c:a", "aac", "-b:a", f"{96 + 64 * out_channels}k", "-ac", str(out_channels)]


KEYFRAME_MARGIN = 0.25    # secondes : pour une image recopiée, `start` est une image clé (voir keyframe_at) ; ffmpeg retire parfois 0,13 s à l'instant demandé avant de chercher (MKV avec images B) et retomberait sur l'image clé d'avant


def ffmpeg_command(ffmpeg, path, probe, decision, start=0, max_kbps=0, max_height=0, channels=2, preset="veryfast"):
    """La ligne de commande qui produit un MP4 fragmenté sur la sortie standard. Le MP4 fragmenté se lit en continu, sans que le fichier soit fini.

    `start` : où reprendre. Quand l'image est ré-encodée, le flux commence à cet instant. Quand elle est recopiée, il commence sur l'image clé qui le contient : `start` doit être celle que rend `keyframe_at`."""
    track = decision["audio"]
    if decision["copy_video"] and start > 0:
        start += KEYFRAME_MARGIN
    cmd = [ffmpeg, "-v", "error", "-nostdin"] + (["-ss", f"{start:.3f}"] if start > 0 else [])
    cmd += ["-i", str(path), "-map", "0:v:0"] + ([] if track is None else ["-map", f"0:a:{track}"])
    cmd += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"] + _video_args(probe, decision, max_kbps, max_height, preset)
    cmd += [] if track is None else _audio_args(probe, decision, channels)
    return cmd + ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
