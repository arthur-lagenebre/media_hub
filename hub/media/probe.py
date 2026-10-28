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


def _fps(text):
    try:
        top, _, bottom = (text or "0/1").partition("/")
        return round(float(top) / float(bottom or 1), 3)
    except (ValueError, ZeroDivisionError):
        return 0


def summarize(data):
    """La sortie de ffprobe réduite à ce qui décide de la lecture. Ce résumé est ce qu'on garde dans la base."""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    names = (fmt.get("format_name") or "").split(",")
    container = next((family for key, family in CONTAINER_FAMILY if key in names), names[0] if names and names[0] else "")
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    out = {"container": container, "duration": float(fmt.get("duration") or 0), "bitrate": int(fmt.get("bit_rate") or 0), "size": int(fmt.get("size") or 0), "video": None, "audio": [], "subs": []}
    if video:
        bits = video.get("bits_per_raw_sample") or ""
        depth = int(bits) if str(bits).isdigit() else (10 if "10" in (video.get("pix_fmt") or "") else 8)
        out["video"] = {
            "codec": VIDEO_NAMES.get(video.get("codec_name"), video.get("codec_name") or ""), "profile": video.get("profile") or "",
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0), "fps": _fps(video.get("avg_frame_rate")),
            "depth": depth, "pix_fmt": video.get("pix_fmt") or "", "hdr": video.get("color_transfer") in ("smpte2084", "arib-std-b67"),
            "bitrate": int(video.get("bit_rate") or 0),
        }
    for s in (x for x in streams if x.get("codec_type") == "audio"):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        out["audio"].append({
            "index": len(out["audio"]), "codec": AUDIO_NAMES.get(s.get("codec_name"), s.get("codec_name") or ""), "channels": int(s.get("channels") or 2),
            "lang": tags.get("language", ""), "title": tags.get("title", ""), "default": bool(disp.get("default")),
            "commentary": bool(disp.get("comment")) or bool(disp.get("visual_impaired")),
        })
    for s in (x for x in streams if x.get("codec_type") == "subtitle"):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        codec = s.get("codec_name") or ""
        out["subs"].append({
            "index": len(out["subs"]), "codec": codec, "lang": tags.get("language", ""), "title": tags.get("title", ""),
            "forced": bool(disp.get("forced")), "default": bool(disp.get("default")), "text": codec in TEXT_SUBS,
        })
    return out


def default_audio(probe):
    """La piste que le lecteur choisirait seul : la piste par défaut qui n'est pas un commentaire, sinon la première."""
    tracks = probe["audio"]
    return next((t for t in tracks if t["default"] and not t["commentary"]), tracks[0] if tracks else None)


def plan(probe, caps, audio=None, max_kbps=0, max_height=0):
    """Décide comment jouer. `caps` : {"video": [...], "audio": [...], "containers": [...], "channels": n, "height": n}.

    Rend {"mode": direct|remux|transcode, "audio": index, "why": [raisons], "copy_video": bool, "copy_audio": bool}.
    """
    caps = caps or {}
    vcaps, acaps = set(caps.get("video") or []), set(caps.get("audio") or [])
    containers = set(caps.get("containers") or ["mp4", "webm"])
    video = probe.get("video")
    track = None
    if audio is not None:
        track = next((t for t in probe["audio"] if t["index"] == audio), None)
    track = track or default_audio(probe)
    chosen = track["index"] if track else None
    why = []

    video_ok = bool(video) and video["codec"] in vcaps and (video["depth"] <= 8 or f"{video['codec']}10" in vcaps) and not (video["hdr"] and not caps.get("hdr"))
    if video and video["codec"] not in vcaps:
        why.append(f"image {video['codec']} non lisible par ce lecteur")
    elif video and not video_ok:
        why.append("image 10 bits / HDR non lisible par ce lecteur")
    limit_h = max_height or caps.get("height") or 0
    too_tall = bool(video and limit_h and video["height"] > limit_h)
    too_heavy = bool(max_kbps and probe["bitrate"] and probe["bitrate"] / 1000 > max_kbps * 1.05)
    if too_tall:
        why.append(f"image {video['height']} p au-delà de {limit_h} p")
    if too_heavy:
        why.append(f"débit {round(probe['bitrate'] / 1e6, 1)} Mb/s au-delà de {round(max_kbps / 1000, 1)} Mb/s demandés")

    audio_ok = track is None or (track["codec"] in acaps and track["channels"] <= (caps.get("channels") or 2))
    if track and track["codec"] not in acaps:
        why.append(f"son {track['codec']} non lisible par ce lecteur")
    elif track and not audio_ok:
        why.append(f"son {track['channels']} voies, le lecteur en prend {caps.get('channels') or 2}")
    container_ok = probe["container"] in containers

    if not video_ok or too_tall or too_heavy:
        return {"mode": "transcode", "audio": chosen, "why": why, "copy_video": False, "copy_audio": False}
    is_default = track is None or track is default_audio(probe)
    if container_ok and audio_ok and is_default:
        return {"mode": "direct", "audio": chosen, "why": [], "copy_video": True, "copy_audio": True}
    if not container_ok:
        why.append(f"conteneur {probe['container']} non lu par ce lecteur")
    if not is_default:
        why.append("autre piste audio que celle par défaut")
    return {"mode": "remux", "audio": chosen, "why": why, "copy_video": True, "copy_audio": audio_ok}


def ffmpeg_command(ffmpeg, path, probe, decision, start=0, max_kbps=0, max_height=0, channels=2, preset="veryfast"):
    """La ligne de commande qui produit un MP4 fragmenté sur la sortie standard. Le MP4 fragmenté se lit en continu, sans que le fichier soit fini."""
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path), "-map", "0:v:0"]
    track = decision["audio"]
    if track is not None:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
    video = probe.get("video") or {}
    if decision["copy_video"]:
        cmd += ["-c:v", "copy"]
        if video.get("codec") == "hevc":
            cmd += ["-tag:v", "hvc1"]
    else:
        height = min(x for x in (max_height, video.get("height", 0)) if x) if (max_height or video.get("height")) else 0
        filters = []
        if height and video.get("height", 0) > height:
            filters.append(f"scale=-2:{height}")
        filters.append("format=yuv420p")
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", "21", "-profile:v", "high", "-level", "4.1", "-g", "48", "-sc_threshold", "0", "-vf", ",".join(filters)]
        if max_kbps:
            cmd += ["-maxrate", f"{int(max_kbps * 0.92)}k", "-bufsize", f"{int(max_kbps * 2)}k"]
    if track is not None:
        if decision["copy_audio"]:
            cmd += ["-c:a", "copy"]
        else:
            src = next((t for t in probe["audio"] if t["index"] == track), {"channels": 2})
            out_channels = max(1, min(src["channels"], channels))
            cmd += ["-c:a", "aac", "-b:a", f"{96 + 64 * out_channels}k", "-ac", str(out_channels)]
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd
