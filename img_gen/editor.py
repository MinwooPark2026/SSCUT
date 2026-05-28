"""SSCUT editor — 픽커 + 싱크 + 모션 통합 단일 서버 (default port 8765).

    ./run.sh -m img_gen.editor /path/to/SSCUT_project

라우트:
  GET /             → 302 → /files (landing)
  GET /picker       → picker.html (이미지 선택)
  GET /storyboard   → storyboard.html (콘티 편집)
  GET /files        → files.html (로컬 프로젝트 파일 탐색)
  GET /settings     → settings.html (.env 설정 편집)
  GET /sync         → sync.html (자막 싱크)
  GET /motion       → motion.html (모션·초점)
  GET /bgm          → bgm.html (BGM 후처리)
  GET /data         → 통합 JSON (picker + sync + motion 필드)
  GET /env_data     → .env 원문
  GET /storyboard_data → 콘티 파일 원문
  GET /bgm_data     → BGM 파일 목록 + 설정
  GET /files_data   → 로컬 폴더 파일 목록
  GET /file_preview → 텍스트·이미지·영상 미리보기
  GET /audio/<rel>  → AUDIO/BGM 오디오 파일
  GET /img/<c>/<f>  → 픽커 후보 이미지
  GET /image/<c>    → 선택된 소스 이미지 (sync·motion 용)
  GET /video/<c>    → base mp4 (Range 지원)
  POST /save        → PICKS.json (픽커)
  POST /save_env    → .env 저장
  POST /choose_folder → macOS 폴더 선택창
  POST /set_project → 앱 전체 프로젝트 폴더 변경
  POST /save_storyboard → 콘티 파일 저장
  POST /generate_image → 현재 컷 이미지 1장 추가 생성
  POST /save_timings  → 자막 timing (sync, motion 보존 merge)
  POST /save_motion   → motion 설정 (자막 보존 merge)
  POST /render        → final.mp4 재렌더 (motion + audio + subtitle 전부)
  POST /upload_bgm    → AUDIO/BGM 에 오디오 파일 추가
  POST /save_bgm      → BGM.json 저장
  POST /render_bgm    → final.mp4 + BGM → final_bgm.mp4
  POST /export_project → Final Cut FCPXML 또는 CapCut import package 생성
"""
from __future__ import annotations

import argparse
import html
import http.server
import io
import json
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import Config, ENV_PATH, PACKAGE_ROOT
from .providers import generate_openai
from .storyboard import parse_storyboard
from .video import (
    FFMPEG, concat_clips, find_font, get_duration,
    make_motion_clip, mux_cut,
)

HTML_DIR = Path(__file__).resolve().parent
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS
TEXT_EXTS = {".md", ".txt", ".json", ".srt", ".vtt"}
PROJECT_FILE_EXTS = MEDIA_EXTS | TEXT_EXTS
CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg",
}
SSCUT_MANIFEST = "SSCUT.json"
SSCUT_CUTS_DIR = "CUTS"
SSCUT_CONTI = "CONTI.md"
SSCUT_PICKS = "PICKS.json"
SSCUT_TIMELINE = "TIMELINE.json"
SSCUT_BGM = "BGM.json"
SSCUT_AUDIO_DIR = "AUDIO"
BASE_CACHE_VERSION = "cover-crop-v1"


def media_type_of(filename: str) -> str:
    """확장자로 image/video 판별."""
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    return "image"


# ─────────────────────────────────────────────────────────────────────────────
# storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_project_file(project_root: Path, rel_path: str, label: str) -> Path:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError(f"{label} 경로가 비어 있음")
    rel = Path(rel_path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"{label} 은 프로젝트 내부 상대 경로여야 함: {rel_path}")
    target = (project_root / rel).resolve()
    target.relative_to(project_root.resolve())
    return target


def load_sscut_manifest(project_root: Path) -> dict:
    project_root = project_root.expanduser().resolve()
    manifest_path = project_root / SSCUT_MANIFEST
    if not manifest_path.is_file():
        raise ValueError(f"SSCUT 프로젝트 아님: {SSCUT_MANIFEST} 없음 ({project_root})")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{SSCUT_MANIFEST} 형식 오류: object 필요")
    if data.get("schema") != "sscut-project-v1":
        raise ValueError(f"{SSCUT_MANIFEST} schema는 sscut-project-v1 이어야 함")
    return data


def _manifest_standard_path(manifest: dict, key: str, default_name: str) -> str:
    value = str(manifest.get(key) or default_name)
    if value != default_name:
        raise ValueError(f"{SSCUT_MANIFEST} {key} 값은 {default_name!r} 이어야 함")
    return value


def resolve_sscut_project(project_root: Path) -> tuple[Path, Path, Path]:
    """SSCUT v1 프로젝트 루트 검증 후 (project_root, CUTS, CONTI) 반환."""
    project_root = project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError(f"폴더 없음: {project_root}")
    manifest = load_sscut_manifest(project_root)

    cuts_dir = _resolve_project_file(project_root, _manifest_standard_path(manifest, "cuts_dir", SSCUT_CUTS_DIR), "cuts_dir")
    conti_path = _resolve_project_file(project_root, _manifest_standard_path(manifest, "conti", SSCUT_CONTI), "conti")
    _manifest_standard_path(manifest, "picks", SSCUT_PICKS)
    _manifest_standard_path(manifest, "timeline", SSCUT_TIMELINE)
    _manifest_standard_path(manifest, "renders_dir", "RENDERS")
    _manifest_standard_path(manifest, "cache_dir", "CACHE")
    _manifest_standard_path(manifest, "audio_dir", SSCUT_AUDIO_DIR)
    _manifest_standard_path(manifest, "bgm", SSCUT_BGM)
    if not cuts_dir.is_dir():
        raise ValueError(f"CUTS 폴더 없음: {cuts_dir}")
    if not conti_path.is_file():
        raise ValueError(f"CONTI 파일 없음: {conti_path}")
    return project_root, cuts_dir, conti_path


def project_root_for_output(out_dir: Path) -> Path:
    project_root = out_dir.expanduser().resolve().parent
    _, cuts_dir, _ = resolve_sscut_project(project_root)
    if cuts_dir.resolve() != out_dir.expanduser().resolve():
        raise ValueError(f"SSCUT CUTS 폴더가 아님: {out_dir}")
    return project_root


def manifest_path_for(out_dir: Path, key: str, default_name: str, label: str) -> Path:
    project_root = project_root_for_output(out_dir)
    manifest = load_sscut_manifest(project_root)
    return _resolve_project_file(project_root, _manifest_standard_path(manifest, key, default_name), label)


def selections_path(out_dir: Path) -> Path:
    return manifest_path_for(out_dir, "picks", SSCUT_PICKS, "picks")


def timings_path(out_dir: Path) -> Path:
    return manifest_path_for(out_dir, "timeline", SSCUT_TIMELINE, "timeline")


def bgm_config_path(out_dir: Path) -> Path:
    return manifest_path_for(out_dir, "bgm", SSCUT_BGM, "bgm")


def audio_dir(out_dir: Path) -> Path:
    project_root = project_root_for_output(out_dir)
    manifest = load_sscut_manifest(project_root)
    return _resolve_project_file(project_root, _manifest_standard_path(manifest, "audio_dir", SSCUT_AUDIO_DIR), "audio_dir")


def bgm_dir(out_dir: Path) -> Path:
    return audio_dir(out_dir) / "BGM"


def exports_dir(out_dir: Path) -> Path:
    return project_root_for_output(out_dir) / "EXPORTS"


def video_cache_dir(out_dir: Path) -> Path:
    project_root = project_root_for_output(out_dir)
    manifest = load_sscut_manifest(project_root)
    cache_dir = _resolve_project_file(project_root, _manifest_standard_path(manifest, "cache_dir", "CACHE"), "cache_dir")
    return cache_dir / "video"


def cut_root(out_dir: Path, cut_id: str) -> Path:
    return out_dir / cut_id


def media_path_for_selection(out_dir: Path, cut_id: str, rel_path: str) -> Path:
    if "/" in cut_id or ".." in cut_id:
        raise ValueError("invalid cut_id")
    rel = Path(rel_path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("invalid media path")
    base = cut_root(out_dir, cut_id).resolve()
    target = (base / rel).resolve()
    target.relative_to(base)
    return target


def load_selections(out_dir: Path) -> dict:
    p = selections_path(out_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("selections", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


SELECTIONS_BACKUP_DIR = ".selections_backup"
SELECTIONS_BACKUP_KEEP = 10


def _rotate_selections_backup(out_dir: Path) -> None:
    """현 PICK 파일을 timestamp 이름으로 백업, 최근 N개만 유지."""
    src = selections_path(out_dir)
    if not src.exists():
        return
    bak_dir = src.parent / SELECTIONS_BACKUP_DIR
    bak_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 같은 초에 여러 번 저장 시 충돌 방지
    bak = bak_dir / f"selections_{ts}.json"
    n = 0
    while bak.exists() and n < 100:
        n += 1
        bak = bak_dir / f"selections_{ts}_{n:02d}.json"
    bak.write_bytes(src.read_bytes())
    # rotate: 최근 N개만 유지
    baks = sorted(bak_dir.glob("selections_*.json"), reverse=True)
    for old in baks[SELECTIONS_BACKUP_KEEP:]:
        old.unlink(missing_ok=True)


def save_selections(out_dir: Path, storyboard: Path | None, selections: dict) -> None:
    # 덮어쓰기 전에 직전 버전 백업 (.selections_backup/ 안)
    _rotate_selections_backup(out_dir)

    payload = {
        "schema": "sscut-picks-v1",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "selections": selections,
    }
    p = selections_path(out_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_timings(out_dir: Path) -> dict:
    p = timings_path(out_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("timings", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_timings(out_dir: Path, timings: dict) -> None:
    p = timings_path(out_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({
            "schema": "sscut-timeline-v1",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "timings": timings,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


DEFAULT_BGM_CONFIG = {
    "schema": "sscut-bgm-v1",
    "selected_clip_id": "",
    "clips": [],
}


def _float_clamp(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def normalize_bgm_config(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    clips_raw = raw.get("clips")
    if not isinstance(clips_raw, list):
        legacy_track = str(raw.get("track") or "").strip()
        clips_raw = [{
            "id": "bgm_001",
            "track": legacy_track,
            "timeline_start": raw.get("start", 0.0),
            "source_start": 0.0,
            "duration": 0.0,
            "volume": raw.get("volume", 0.25),
            "fade_in": raw.get("fade_in", 1.0),
            "fade_out": raw.get("fade_out", 2.0),
            "loop": raw.get("loop", True),
        }] if legacy_track else []

    clips = []
    used_ids = set()
    for i, item in enumerate(clips_raw):
        if not isinstance(item, dict):
            continue
        clip_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item.get("id") or "")).strip("_")
        if not clip_id or clip_id in used_ids:
            clip_id = f"bgm_{i + 1:03d}"
        while clip_id in used_ids:
            clip_id = f"bgm_{len(used_ids) + 1:03d}"
        used_ids.add(clip_id)
        track = str(item.get("track") or "").strip()
        clips.append({
            "id": clip_id,
            "track": track,
            "timeline_start": _float_clamp(item.get("timeline_start", item.get("start")), 0.0, 0.0, 3600.0),
            "source_start": _float_clamp(item.get("source_start", item.get("trim_start")), 0.0, 0.0, 3600.0),
            "duration": _float_clamp(item.get("duration"), 0.0, 0.0, 3600.0),
            "volume": _float_clamp(item.get("volume"), 0.25, 0.0, 1.0),
            "fade_in": _float_clamp(item.get("fade_in"), 1.0, 0.0, 60.0),
            "fade_out": _float_clamp(item.get("fade_out"), 2.0, 0.0, 60.0),
            "loop": bool(item.get("loop", True)),
        })
    selected = str(raw.get("selected_clip_id") or "").strip()
    if selected not in used_ids:
        selected = clips[0]["id"] if clips else ""
    return {
        "schema": "sscut-bgm-v1",
        "selected_clip_id": selected,
        "clips": clips,
    }


def ensure_bgm_storage(out_dir: Path) -> tuple[Path, Path]:
    audio = audio_dir(out_dir)
    bgm = bgm_dir(out_dir)
    audio.mkdir(parents=True, exist_ok=True)
    bgm.mkdir(parents=True, exist_ok=True)
    cfg_path = bgm_config_path(out_dir)
    if not cfg_path.exists():
        cfg_path.write_text(
            json.dumps(DEFAULT_BGM_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return audio, bgm


def load_bgm_config(out_dir: Path) -> dict:
    p = bgm_config_path(out_dir)
    if not p.exists():
        return dict(DEFAULT_BGM_CONFIG)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_BGM_CONFIG)
    return normalize_bgm_config(data)


def save_bgm_config(out_dir: Path, config: dict) -> dict:
    ensure_bgm_storage(out_dir)
    clean = normalize_bgm_config(config)
    for clip in clean["clips"]:
        if not clip["track"]:
            continue
        clip["track"] = normalize_bgm_track_rel(clip["track"])
        resolve_bgm_track(out_dir, clip["track"])
    clean["updated_at"] = datetime.now().isoformat(timespec="seconds")
    p = bgm_config_path(out_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return clean


def normalize_bgm_track_rel(track: str) -> str:
    rel = Path(str(track).strip())
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("BGM track은 AUDIO/BGM 내부 상대 경로여야 함")
    if len(rel.parts) == 1:
        rel = Path("BGM") / rel
    if not rel.parts or rel.parts[0] != "BGM":
        raise ValueError("BGM track은 AUDIO/BGM 아래 파일이어야 함")
    return rel.as_posix()


def resolve_bgm_track(out_dir: Path, track: str) -> Path:
    rel = Path(normalize_bgm_track_rel(track))
    if rel.suffix.lower() not in AUDIO_EXTS:
        raise ValueError(f"지원하지 않는 오디오 확장자: {rel.suffix}")
    audio = audio_dir(out_dir).resolve()
    bgm = bgm_dir(out_dir).resolve()
    target = (audio / rel).resolve()
    target.relative_to(bgm)
    if not target.is_file():
        raise ValueError(f"BGM 파일 없음: {target}")
    return target


def discover_bgm_tracks(out_dir: Path) -> list[dict]:
    ensure_bgm_storage(out_dir)
    root = bgm_dir(out_dir)
    tracks = []
    for p in sorted(root.rglob("*"), key=lambda x: x.relative_to(root).as_posix().lower()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        rel = p.relative_to(audio_dir(out_dir)).as_posix()
        item = {
            "name": p.name,
            "rel": rel,
            "path": str(p),
            "size": p.stat().st_size,
        }
        try:
            item["duration"] = get_duration(p)
        except Exception:
            item["duration"] = 0.0
        tracks.append(item)
    return tracks


def has_audio_stream(path: Path) -> bool:
    r = subprocess.run([FFMPEG, "-i", str(path), "-hide_banner"],
                       capture_output=True, text=True)
    return "Audio:" in r.stderr


def render_bgm_mix(out_dir: Path, config: dict, log_buf: io.StringIO) -> dict:
    cfg = save_bgm_config(out_dir, config)
    clips = [clip for clip in cfg["clips"] if clip.get("track")]
    if not clips:
        return {"ok": False, "error": "BGM 클립을 하나 이상 추가하세요"}

    project_root = project_root_for_output(out_dir)
    final = project_root / "RENDERS" / "final.mp4"
    if not final.is_file():
        return {"ok": False, "error": "먼저 모션/싱크 탭에서 Final 렌더링을 만들어야 합니다"}

    output = project_root / "RENDERS" / "final_bgm.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)

    final_duration = get_duration(final)
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(final)]
    print(f"[bgm] source: {final}", file=log_buf)

    active = []
    for clip in clips:
        track = resolve_bgm_track(out_dir, clip["track"])
        timeline_start = min(float(clip["timeline_start"]), final_duration)
        if timeline_start >= final_duration:
            print(f"[bgm] skip {clip['id']}: final 밖 시작점", file=log_buf)
            continue
        source_start = float(clip["source_start"])
        requested = float(clip["duration"])
        remaining_final = max(0.0, final_duration - timeline_start)
        if requested > 0:
            clip_duration = min(requested, remaining_final)
        elif clip["loop"]:
            clip_duration = remaining_final
        else:
            try:
                source_duration = max(0.0, get_duration(track) - source_start)
            except Exception:
                source_duration = remaining_final
            clip_duration = min(source_duration, remaining_final)
        if clip_duration <= 0.01:
            print(f"[bgm] skip {clip['id']}: 길이 없음", file=log_buf)
            continue
        input_index = len(active) + 1
        if clip["loop"]:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(track)]
        active.append((clip, track, input_index, timeline_start, source_start, clip_duration))

    if not active:
        return {"ok": False, "error": "렌더 가능한 BGM 클립이 없습니다"}

    filter_parts = []
    bgm_labels = []
    for n, (clip, track, input_index, timeline_start, source_start, clip_duration) in enumerate(active):
        fade_in = min(float(clip["fade_in"]), clip_duration)
        fade_out = min(float(clip["fade_out"]), clip_duration)
        filters = [
            f"atrim=start={source_start:.3f}:duration={clip_duration:.3f}",
            "asetpts=PTS-STARTPTS",
            f"volume={float(clip['volume']):.4f}",
        ]
        if fade_in > 0:
            filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            fade_start = max(0.0, clip_duration - fade_out)
            filters.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out:.3f}")
        if timeline_start > 0:
            filters.append(f"adelay={int(round(timeline_start * 1000))}:all=1")
        label = f"bgm{n}"
        bgm_labels.append(f"[{label}]")
        filter_parts.append(f"[{input_index}:a]{','.join(filters)}[{label}]")
        print(
            f"[bgm] clip {clip['id']}: {track.name} "
            f"timeline={timeline_start:.2f}s source={source_start:.2f}s "
            f"duration={clip_duration:.2f}s volume={float(clip['volume']):.2f} "
            f"fade=({fade_in:.2f},{fade_out:.2f}) loop={clip['loop']}",
            file=log_buf,
        )

    final_has_audio = has_audio_stream(final)
    if final_has_audio:
        filter_parts.append(
            f"[0:a]{''.join(bgm_labels)}amix=inputs={len(active) + 1}:"
            "duration=first:normalize=0:dropout_transition=0[a]"
        )
    elif len(active) == 1:
        filter_parts.append(f"{bgm_labels[0]}apad[a]")
    else:
        filter_parts.append(
            f"{''.join(bgm_labels)}amix=inputs={len(active)}:"
            "duration=longest:normalize=0:dropout_transition=0,apad[a]"
        )

    cmd += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "0:v:0",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-t", f"{final_duration:.3f}",
        str(output),
    ]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[:1000], file=log_buf)
        return {"ok": False, "error": "BGM 렌더링 실패"}

    total = get_duration(output)
    size_mb = output.stat().st_size / 1024 / 1024
    print(f"[bgm] 완료: {output.name}, {total:.1f}s, {size_mb:.1f}MB", file=log_buf)
    return {"ok": True, "final_path": str(output), "duration": total, "size_mb": size_mb}


# ─────────────────────────────────────────────────────────────────────────────
# external NLE exports
# ─────────────────────────────────────────────────────────────────────────────

def _frames(seconds: float, fps: int = 30) -> int:
    return max(1, int(round(max(0.0, seconds) * fps)))


def _fcpx_time(seconds: float, fps: int = 30) -> str:
    return f"{_frames(seconds, fps)}/{fps}s"


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    ms_total = int(round(seconds * 1000))
    h, rem = divmod(ms_total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned or "sscut"


def _copy_export_file(src: Path, dest_dir: Path, name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 2
        while dest.exists():
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1
    shutil.copy2(src, dest)
    return dest


def _export_cut_rows(out_dir: Path, sb_path: Path | None) -> tuple[str, list[dict]]:
    project_root = project_root_for_output(out_dir)
    manifest = load_sscut_manifest(project_root)
    title = str(manifest.get("title") or manifest.get("name") or project_root.name)
    cut_order = [str(c) for c in manifest.get("cut_order", [])]
    selections = load_selections(out_dir)
    timings = load_timings(out_dir)
    video_dir = video_cache_dir(out_dir)
    sb_by_id = {}
    if sb_path and sb_path.is_file():
        sb = parse_storyboard(sb_path)
        title = sb.title or title
        sb_by_id = {c.cut_id: c for c in sb.cuts if c.cut_id}

    rows: list[dict] = []
    cursor = 0.0
    ordered_ids = cut_order or sorted(set(timings.keys()) | set(selections.keys()))
    for index, cut_id in enumerate(ordered_ids, 1):
        selected = selections.get(cut_id)
        if not selected:
            continue
        source = media_path_for_selection(out_dir, cut_id, selected)
        if not source.is_file():
            continue
        is_video = source.suffix.lower() in VIDEO_EXTS
        t = timings.get(cut_id, {})
        tts = video_dir / f"{cut_id}_tts.mp3"
        base = video_dir / f"{cut_id}_base.mp4"
        rendered = video_dir / f"{cut_id}.mp4"
        if is_video:
            duration = get_duration(source)
            audio = None
        elif base.exists():
            duration = get_duration(base)
            audio = tts if tts.is_file() else None
        elif tts.is_file():
            duration = max(3.0, get_duration(tts) + 0.3)
            audio = tts
        elif rendered.exists():
            duration = get_duration(rendered)
            audio = None
        else:
            duration = 3.0
            audio = None

        sb_cut = sb_by_id.get(cut_id)
        subtitle_start = float(t.get("start", 0.0))
        subtitle_text = str(t.get("text", "")).strip()
        if not subtitle_text and sb_cut:
            subtitle_start, subtitle_text = extract_subtitle(sb_cut.subcut)
        rows.append({
            "index": index,
            "cut_id": cut_id,
            "timeline_start": cursor,
            "duration": duration,
            "selected": selected,
            "source": source,
            "media_type": "video" if is_video else "image",
            "audio": audio,
            "subtitle_start": subtitle_start,
            "subtitle_text": subtitle_text,
            "motion": t.get("motion", {}),
        })
        cursor += duration
    return title, rows


def _write_srt(rows: list[dict], path: Path) -> int:
    blocks = []
    n = 1
    for row in rows:
        text = str(row.get("subtitle_text") or "").strip()
        if not text:
            continue
        start = float(row["timeline_start"]) + float(row.get("subtitle_start") or 0.0)
        end = float(row["timeline_start"]) + float(row["duration"])
        if end <= start:
            end = start + 1.0
        blocks.append(f"{n}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
        n += 1
    path.write_text("\n".join(blocks), encoding="utf-8")
    return len(blocks)


def _export_finalcut(out_dir: Path, sb_path: Path | None) -> dict:
    title, rows = _export_cut_rows(out_dir, sb_path)
    if not rows:
        return {"ok": False, "error": "내보낼 선택 컷이 없습니다"}
    export_root = exports_dir(out_dir) / "finalcut"
    export_root.mkdir(parents=True, exist_ok=True)
    fcpxml_path = export_root / f"{_safe_name(title)}.fcpxml"
    srt_path = export_root / f"{_safe_name(title)}.srt"
    total = sum(float(r["duration"]) for r in rows)

    resources = [
        '    <format id="fmt1080p30" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080" colorSpace="1-1-1 (Rec. 709)"/>'
    ]
    spine = []
    asset_id = 1
    for row in rows:
        media_id = f"r{asset_id}"
        asset_id += 1
        source = row["source"]
        duration = float(row["duration"])
        attrs = [
            f'id="{media_id}"',
            f'name="{html.escape(source.name, quote=True)}"',
            f'src="{html.escape(source.as_uri(), quote=True)}"',
            'start="0s"',
            f'duration="{_fcpx_time(duration)}"',
            'hasVideo="1"',
        ]
        if row["media_type"] == "video":
            attrs += ['hasAudio="1"', 'audioSources="1"', 'audioChannels="2"', 'audioRate="44100"']
        attrs.append('format="fmt1080p30"')
        resources.append("    <asset " + " ".join(attrs) + "/>")
        spine.append(
            f'        <asset-clip name="{html.escape(row["cut_id"], quote=True)}" ref="{media_id}" '
            f'offset="{_fcpx_time(float(row["timeline_start"]))}" start="0s" '
            f'duration="{_fcpx_time(duration)}" format="fmt1080p30"/>'
        )
        audio = row.get("audio")
        if audio and Path(audio).is_file():
            audio_id = f"r{asset_id}"
            asset_id += 1
            audio_dur = min(get_duration(Path(audio)), duration)
            resources.append(
                f'    <asset id="{audio_id}" name="{html.escape(Path(audio).name, quote=True)}" '
                f'src="{html.escape(Path(audio).as_uri(), quote=True)}" start="0s" '
                f'duration="{_fcpx_time(audio_dur)}" hasAudio="1" audioSources="1" '
                f'audioChannels="2" audioRate="44100"/>'
            )
            spine.append(
                f'        <asset-clip name="{html.escape(row["cut_id"] + "_tts", quote=True)}" ref="{audio_id}" lane="-1" '
                f'offset="{_fcpx_time(float(row["timeline_start"]))}" start="0s" duration="{_fcpx_time(audio_dur)}"/>'
            )

    fcpxml = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<fcpxml version="1.10">',
        '  <resources>',
        *resources,
        '  </resources>',
        '  <library>',
        f'    <event name="{html.escape(title, quote=True)}">',
        f'      <project name="{html.escape(title, quote=True)}">',
        f'        <sequence format="fmt1080p30" duration="{_fcpx_time(total)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">',
        '          <spine>',
        *spine,
        '          </spine>',
        '        </sequence>',
        '      </project>',
        '    </event>',
        '  </library>',
        '</fcpxml>',
        '',
    ])
    fcpxml_path.write_text(fcpxml, encoding="utf-8")
    subtitle_count = _write_srt(rows, srt_path)
    manifest = {
        "schema": "sscut-export-v1",
        "format": "finalcut",
        "title": title,
        "fcpxml": str(fcpxml_path),
        "srt": str(srt_path),
        "cuts": len(rows),
        "duration": total,
        "notes": [
            "FCPXML은 원본 이미지/영상과 TTS 오디오를 참조합니다.",
            "자막은 함께 생성된 SRT를 Final Cut에 별도 import하세요.",
            "모션 keyframe export는 아직 1차 구현 범위 밖입니다.",
        ],
    }
    (export_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "format": "finalcut", "path": str(fcpxml_path), "folder": str(export_root),
            "srt": str(srt_path), "cuts": len(rows), "subtitles": subtitle_count, "duration": total}


def _export_capcut(out_dir: Path, sb_path: Path | None) -> dict:
    title, rows = _export_cut_rows(out_dir, sb_path)
    if not rows:
        return {"ok": False, "error": "내보낼 선택 컷이 없습니다"}
    export_root = exports_dir(out_dir) / "capcut" / _safe_name(title)
    media_dir = export_root / "media"
    audio_dir_export = export_root / "audio"
    export_root.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    audio_dir_export.mkdir(parents=True, exist_ok=True)
    srt_path = export_root / "subtitles.srt"
    csv_path = export_root / "timeline.csv"

    copied_rows = []
    for row in rows:
        src = Path(row["source"])
        media_name = f'{row["index"]:03d}_{_safe_name(row["cut_id"])}{src.suffix.lower()}'
        media_copy = _copy_export_file(src, media_dir, media_name)
        audio_copy = None
        audio = row.get("audio")
        if audio and Path(audio).is_file():
            audio_copy = _copy_export_file(Path(audio), audio_dir_export, f'{row["index"]:03d}_{_safe_name(row["cut_id"])}_tts{Path(audio).suffix.lower()}')
        copied = dict(row)
        copied["media_file"] = media_copy
        copied["audio_file"] = audio_copy
        copied_rows.append(copied)

    subtitle_count = _write_srt(copied_rows, srt_path)
    csv_lines = ["index,cut_id,start,duration,media_type,media_file,audio_file,subtitle_start,subtitle_text"]
    for row in copied_rows:
        vals = [
            str(row["index"]),
            row["cut_id"],
            f'{float(row["timeline_start"]):.3f}',
            f'{float(row["duration"]):.3f}',
            row["media_type"],
            Path(row["media_file"]).name,
            Path(row["audio_file"]).name if row.get("audio_file") else "",
            f'{float(row.get("subtitle_start") or 0.0):.3f}',
            str(row.get("subtitle_text") or "").replace('"', '""'),
        ]
        csv_lines.append(",".join('"' + v + '"' for v in vals))
    csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

    manifest = {
        "schema": "sscut-export-v1",
        "format": "capcut-package",
        "title": title,
        "cuts": len(rows),
        "duration": sum(float(r["duration"]) for r in rows),
        "media_dir": str(media_dir),
        "audio_dir": str(audio_dir_export),
        "subtitles_srt": str(srt_path),
        "timeline_csv": str(csv_path),
        "notes": [
            "CapCut 공식 프로젝트 타임라인 import 포맷은 안정적으로 공개되어 있지 않습니다.",
            "이 패키지는 CapCut에서 직접 import 가능한 미디어 파일과 SRT 자막, 수동 배치용 timeline.csv를 제공합니다.",
        ],
    }
    (export_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (export_root / "README.txt").write_text(
        "CapCut import package\n\n"
        "1. media/ 안의 이미지와 영상을 CapCut에 import하세요.\n"
        "2. audio/ 안의 TTS 파일을 필요 컷 아래에 배치하세요.\n"
        "3. subtitles.srt를 자막으로 import하세요.\n"
        "4. timeline.csv의 start/duration 값을 기준으로 컷 길이를 맞추세요.\n\n"
        "CapCut 프로젝트 draft 파일은 버전별로 바뀌는 비공개 포맷이라 직접 생성하지 않습니다.\n",
        encoding="utf-8",
    )
    return {"ok": True, "format": "capcut", "path": str(export_root / "manifest.json"),
            "folder": str(export_root), "srt": str(srt_path), "cuts": len(rows),
            "subtitles": subtitle_count, "duration": manifest["duration"]}


def export_project(out_dir: Path, sb_path: Path | None, fmt: str) -> dict:
    fmt = (fmt or "").strip().lower()
    if fmt in {"finalcut", "fcpxml", "fcp"}:
        return _export_finalcut(out_dir, sb_path)
    if fmt in {"capcut", "capcut-package"}:
        return _export_capcut(out_dir, sb_path)
    return {"ok": False, "error": "format은 finalcut 또는 capcut 이어야 합니다"}


def ensure_env_file() -> Path:
    """.env 가 없으면 .env.example 을 복사해서 생성."""
    if ENV_PATH.exists():
        return ENV_PATH
    example = PACKAGE_ROOT / ".env.example"
    if not example.is_file():
        raise FileNotFoundError(f".env.example 없음: {example}")
    ENV_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return ENV_PATH


def save_env_text(text: str) -> None:
    """로컬 .env 저장. 직전 파일은 .env_backup 에 보관."""
    ensure_env_file()
    bak_dir = PACKAGE_ROOT / ".env_backup"
    bak_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = bak_dir / f".env_{ts}.backup"
    n = 0
    while bak.exists() and n < 100:
        n += 1
        bak = bak_dir / f".env_{ts}_{n:02d}.backup"
    bak.write_bytes(ENV_PATH.read_bytes())

    tmp = ENV_PATH.parent / ".env.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(ENV_PATH)


def save_storyboard_text(path: Path, text: str) -> None:
    """콘티 원문 저장. 직전 파일은 같은 폴더 .storyboard_backup 에 보관."""
    bak_dir = path.parent / ".storyboard_backup"
    bak_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if path.exists():
        bak = bak_dir / f"{path.stem}_{ts}{path.suffix}"
        n = 0
        while bak.exists() and n < 100:
            n += 1
            bak = bak_dir / f"{path.stem}_{ts}_{n:02d}{path.suffix}"
        bak.write_bytes(path.read_bytes())

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# discovery + storyboard parsing
# ─────────────────────────────────────────────────────────────────────────────

RESERVED_FOLDERS = {"video"}  # 렌더 산출물 폴더 — cut 아님


def discover_cuts(out_dir: Path) -> list[dict]:
    """SSCUT v1 CUTS/<cut_id>/images|videos 아래의 미디어를 찾는다."""
    project_root = project_root_for_output(out_dir)
    manifest = load_sscut_manifest(project_root)
    cut_order = [str(x) for x in manifest.get("cut_order", []) if isinstance(x, str)]
    folders_by_name = {
        p.name: p for p in out_dir.iterdir()
        if p.is_dir() and not p.name.startswith("_") and not p.name.startswith(".")
    }
    ordered_names = [name for name in cut_order if name in folders_by_name]
    ordered_names.extend(sorted(name for name in folders_by_name if name not in set(ordered_names)))

    cuts = []
    for name in ordered_names:
        folder = folders_by_name[name]
        prompt_path = folder / "PROMPT.md"
        prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.is_file() else ""
        media = []
        seen = set()
        media_files = []
        for media_dir in (folder / "images", folder / "videos"):
            if media_dir.is_dir():
                media_files.extend(p for p in media_dir.rglob("*") if p.is_file())
        for p in sorted(media_files, key=lambda x: x.relative_to(folder).as_posix()):
            if not p.is_file():
                continue
            if p.name.startswith("_") or p.name.startswith("."):
                continue
            ext = p.suffix.lower()
            rel = p.relative_to(folder).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            if ext in IMAGE_EXTS:
                media.append({"name": rel, "type": "image"})
            elif ext in VIDEO_EXTS:
                media.append({"name": rel, "type": "video"})
        if not media:
            continue
        cuts.append({"cut_id": folder.name, "prompt": prompt, "media": media})
    return cuts


def extract_subtitle(subcut_text: str) -> tuple[float, str]:
    m = re.search(
        r'(\d+(?:\.\d+)?)~\d+(?:\.\d+)?s?\s*자막:\s*["“]([^"”]+)["”]',
        subcut_text,
    )
    if m:
        return float(m.group(1)), m.group(2).strip()
    m = re.search(r'(\d+(?:\.\d+)?)~\d+(?:\.\d+)?s?\s*자막:\s*(.+?)(?:$|\n)', subcut_text)
    if m:
        return float(m.group(1)), m.group(2).strip()
    return 0.0, ""


def storyboard_cut_for_id(out_dir: Path, sb_path: Path | None, cut_id: str):
    """cut_id와 같은 콘티 row 반환. 컷 ID가 없으면 row 순서로 fallback."""
    if not sb_path:
        return None
    sb = parse_storyboard(sb_path)
    by_id = {c.cut_id: c for c in sb.cuts if c.cut_id}
    if cut_id in by_id:
        return by_id[cut_id]
    cuts = discover_cuts(out_dir)
    cut_ids = [c["cut_id"] for c in cuts]
    if cut_id not in cut_ids:
        return None
    idx = cut_ids.index(cut_id)
    return sb.cuts[idx] if idx < len(sb.cuts) else None


def prompt_for_cut(out_dir: Path, sb_path: Path | None, cut_id: str) -> str:
    """CUTS/<cut_id>/PROMPT.md 를 기준 프롬프트로 사용."""
    if "/" in cut_id or ".." in cut_id:
        raise ValueError("invalid cut_id")
    prompt_path = out_dir / cut_id / "PROMPT.md"
    if prompt_path.is_file():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if prompt:
            return prompt

    raise ValueError(f"프롬프트 없음: {cut_id}. CUTS/{cut_id}/PROMPT.md 필요")


def scan_local_folder(root: Path, max_files: int = 600) -> dict:
    """로컬 폴더 한 단계의 폴더와 작업 파일 목록."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"폴더 없음: {root}")

    skip_dirs = {
        ".git", ".venv", ".env_backup", ".selections_backup", ".storyboard_backup",
        "__pycache__", "node_modules",
    }
    dirs = []
    files = []
    for p in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if len(files) >= max_files:
            break
        if p.name in skip_dirs or p.name.startswith(".DS_Store"):
            continue
        rel = p.relative_to(root)
        if p.is_dir():
            dirs.append({
                "name": p.name,
                "path": str(p),
                "rel": str(rel),
                "kind": "folder",
                "size": None,
            })
            continue
        ext = p.suffix.lower()
        if ext not in PROJECT_FILE_EXTS:
            continue
        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in VIDEO_EXTS:
            kind = "video"
        elif ext in AUDIO_EXTS:
            kind = "audio"
        else:
            kind = "text"
        files.append({
            "name": p.name,
            "path": str(p),
            "rel": str(rel),
            "kind": kind,
            "ext": ext,
            "size": p.stat().st_size,
        })

    return {
        "root": str(root),
        "parent": str(root.parent),
        "dirs": dirs,
        "files": files,
        "truncated": len(files) >= max_files,
    }


def choose_folder_with_dialog(start_dir: Path) -> Path | None:
    """macOS Finder 폴더 선택창. 취소하면 None."""
    start_dir = start_dir.expanduser().resolve()
    if not start_dir.is_dir():
        start_dir = PACKAGE_ROOT
    start_text = str(start_dir).replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'POSIX path of (choose folder with prompt "SSCUT 프로젝트 폴더를 선택하세요" '
        f'default location POSIX file "{start_text}")'
    )
    r = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if "-128" in stderr or "User canceled" in stderr:
            return None
        raise RuntimeError(stderr or "폴더 선택 실패")
    chosen = r.stdout.strip()
    return Path(chosen).expanduser().resolve() if chosen else None


def detect_storyboard(root: Path) -> Path | None:
    """SSCUT.json 의 conti 경로만 사용."""
    _, _, conti_path = resolve_sscut_project(root)
    return conti_path


def detect_output_dir(root: Path) -> Path:
    """SSCUT.json 의 cuts_dir 경로만 사용."""
    _, cuts_dir, _ = resolve_sscut_project(root)
    return cuts_dir


def set_project_dir(state: dict, root: Path) -> tuple[Path, Path, Path | None]:
    """서버의 활성 SSCUT 프로젝트 폴더를 변경."""
    root, out_dir, sb_path = resolve_sscut_project(root)
    state["project_root"] = root
    state["out_dir"] = out_dir
    state["sb_path"] = sb_path
    return root, out_dir, sb_path


# ─────────────────────────────────────────────────────────────────────────────
# video base/render
# ─────────────────────────────────────────────────────────────────────────────

def _base_cache_meta_path(video_dir: Path, cut_id: str) -> Path:
    return video_dir / f"{cut_id}_base.json"


def _base_cache_payload(image: Path) -> dict:
    st = image.stat()
    return {
        "version": BASE_CACHE_VERSION,
        "source_path": str(image.resolve()),
        "source_mtime_ns": st.st_mtime_ns,
        "source_size": st.st_size,
    }


def _base_cache_is_current(video_dir: Path, cut_id: str, image: Path) -> bool:
    meta_path = _base_cache_meta_path(video_dir, cut_id)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected = _base_cache_payload(image)
    except Exception:
        return False
    return all(meta.get(k) == v for k, v in expected.items())


def _write_base_cache_meta(video_dir: Path, cut_id: str, image: Path) -> None:
    try:
        _base_cache_meta_path(video_dir, cut_id).write_text(
            json.dumps(_base_cache_payload(image), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def ensure_base_video(
    video_dir: Path, cut_id: str, image: Path, font: str,
) -> tuple[Path, float] | tuple[None, float]:
    """base.mp4 없으면 생성. (path, duration) 반환. 실패 시 (None, 0)."""
    base = video_dir / f"{cut_id}_base.mp4"
    if base.exists() and _base_cache_is_current(video_dir, cut_id, image):
        return base, get_duration(base)

    existing = video_dir / f"{cut_id}.mp4"
    tts_path = video_dir / f"{cut_id}_tts.mp3"
    if not existing.exists() and not tts_path.exists():
        if base.exists():
            return base, get_duration(base)
        # video.py 안 돌아간 상태 → sync/motion 불가
        return None, 0.0
    if existing.exists():
        duration = get_duration(existing)
    else:
        duration = max(3.0, get_duration(tts_path) + 0.3)

    if not tts_path.exists():
        return None, duration

    motion_tmp = video_dir / f"{cut_id}_motion_tmp.mp4"
    print(f"[editor] base 생성: {cut_id} (duration={duration:.1f}s)")
    make_motion_clip(image, duration, motion_tmp)
    mux_cut(motion_tmp, tts_path, subtitle="", subtitle_start=0,
            duration=duration, font=font, output=base)
    motion_tmp.unlink(missing_ok=True)
    _write_base_cache_meta(video_dir, cut_id, image)
    return base, duration


def _normalize_video(input_path: Path, output_path: Path, fps: int = 30) -> None:
    """외부 영상을 1920x1080 30fps h264 + AAC 44.1k stereo 로 정규화.
    concat demuxer 안전성 + 우리 파이프 표준 매칭."""
    has_audio = has_audio_stream(input_path)
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(input_path),
    ]
    if has_audio:
        audio_map = ["-map", "0:a:0"]
    else:
        duration = get_duration(input_path)
        cmd += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        audio_map = ["-map", "1:a:0"]
    cmd += [
        "-vf", f"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps={fps}",
        "-map", "0:v:0", *audio_map,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        str(output_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"normalize 실패 {input_path.name}: {r.stderr[:500]}")


def _burn_subtitle(
    base: Path, subtitle: str, start: float, duration: float,
    font: str, output: Path, log_buf: io.StringIO | None = None,
) -> None:
    has_sub = bool(subtitle and subtitle.strip())
    has_audio = has_audio_stream(base)
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(base)]
    if not has_audio:
        cmd += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
    if has_sub:
        sub_file = output.with_name(output.stem + "_sub.txt")
        sub_file.write_text(subtitle, encoding="utf-8")
        sub_escaped = str(sub_file).replace(":", r"\:")
        drawtext = (
            f"drawtext=fontfile={font}:textfile={sub_escaped}:"
            f"fontsize=54:fontcolor=white:"
            f"borderw=4:bordercolor=black@0.85:"
            f"box=1:boxcolor=black@0.35:boxborderw=18:"
            f"x=(w-text_w)/2:y=h-180:enable='gte(t,{start})'"
        )
        cmd += ["-vf", drawtext]
    audio_map = ["-map", "0:a:0"] if has_audio else ["-map", "1:a:0"]
    cmd += [
        "-map", "0:v:0", *audio_map,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-t", f"{duration}",
        str(output),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = f"burn 실패 {output.name}: {r.stderr[:500]}"
        if log_buf:
            print(msg, file=log_buf)
        raise RuntimeError(msg)
    if has_sub:
        (output.with_name(output.stem + "_sub.txt")).unlink(missing_ok=True)


def render_final(out_dir: Path, font: str, log_buf: io.StringIO) -> dict:
    video_dir = video_cache_dir(out_dir)
    video_dir.mkdir(parents=True, exist_ok=True)
    timings = load_timings(out_dir)
    selections = load_selections(out_dir)
    if not timings or not selections:
        return {"ok": False, "error": "timings 또는 selections 비어있음"}

    cut_paths: list[Path] = []
    for cut_id in sorted(timings.keys()):
        if cut_id not in selections:
            print(f"[render] {cut_id}: 선택 미디어 없음, 스킵", file=log_buf)
            continue

        sel_file = selections[cut_id]
        sel_path = media_path_for_selection(out_dir, cut_id, sel_file)
        is_external_video = sel_path.suffix.lower() in VIDEO_EXTS
        t = timings[cut_id]
        sub_start = float(t.get("start", 0))
        sub_text = str(t.get("text", "")).strip()

        # ─── 외부 영상 컷 흐름: 정규화 → (옵션) 자막 burn ───
        if is_external_video:
            if not sel_path.is_file():
                return {"ok": False, "error": f"외부 영상 파일 없음: {sel_path}"}
            duration = get_duration(sel_path)
            print(f"[render] {cut_id}: 외부 영상 {sel_file} ({duration:.1f}s)"
                  f"{', 자막 ' + repr(sub_text) if sub_text else ''}", file=log_buf)
            normalized = video_dir / f"{cut_id}_base.mp4"
            _normalize_video(sel_path, normalized)
            cut_path = video_dir / f"{cut_id}.mp4"
            _burn_subtitle(normalized, sub_text, sub_start, duration, font, cut_path, log_buf)
            cut_paths.append(cut_path)
            continue

        # ─── 이미지 컷 흐름 (기존): motion + TTS + 자막 ───
        base = video_dir / f"{cut_id}_base.mp4"
        tts_path = video_dir / f"{cut_id}_tts.mp3"
        if base.exists():
            duration = get_duration(base)
        elif tts_path.exists():
            duration = max(3.0, get_duration(tts_path) + 0.3)
        else:
            return {"ok": False, "error": f"base 도 TTS 도 없음: {cut_id}"}

        m = t.get("motion", {}) or {}
        m_type = m.get("type", "zoom_in")
        m_fx = float(m.get("focal_x", 0.5))
        m_fy = float(m.get("focal_y", 0.5))
        m_amt = float(m.get("zoom_amount", 0.10))

        print(f"[render] {cut_id}: image+motion={m_type} focal=({m_fx:.2f},{m_fy:.2f}) "
              f"amt={m_amt:.2f}, sub start={sub_start:.2f}s {sub_text!r}", file=log_buf)

        image = sel_path
        motion_tmp = video_dir / f"{cut_id}_motion_tmp.mp4"
        make_motion_clip(image, duration, motion_tmp,
                         motion_type=m_type, focal_x=m_fx, focal_y=m_fy, zoom_amount=m_amt)
        mux_cut(motion_tmp, tts_path if tts_path.exists() else None,
                subtitle="", subtitle_start=0,
                duration=duration, font=font, output=base)
        motion_tmp.unlink(missing_ok=True)
        _write_base_cache_meta(video_dir, cut_id, image)

        cut_path = video_dir / f"{cut_id}.mp4"
        _burn_subtitle(base, sub_text, sub_start, duration, font, cut_path, log_buf)
        cut_paths.append(cut_path)

    if not cut_paths:
        return {"ok": False, "error": "렌더 가능한 컷 없음"}

    final = project_root_for_output(out_dir) / "RENDERS" / "final.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    print(f"[render] concat → {final.name}", file=log_buf)
    concat_clips(cut_paths, final)
    total = get_duration(final)
    size_mb = final.stat().st_size / 1024 / 1024
    print(f"[render] 완료: {total:.1f}s, {size_mb:.1f}MB", file=log_buf)
    return {"ok": True, "final_path": str(final),
            "duration": total, "size_mb": size_mb}


# ─────────────────────────────────────────────────────────────────────────────
# build_data — 통합 데이터
# ─────────────────────────────────────────────────────────────────────────────

def build_data(out_dir: Path, sb_path: Path | None, font: str) -> dict:
    cuts_picker = discover_cuts(out_dir)
    selections = load_selections(out_dir)
    timings = load_timings(out_dir)

    storyboard_cuts = []
    title = out_dir.name
    sb_cuts = []
    if sb_path:
        sb = parse_storyboard(sb_path)
        title = sb.title
        sb_cuts = sb.cuts
        storyboard_cuts = [{
            "index": c.index, "cut_id": c.cut_id, "time": c.time, "scene": c.scene,
            "subcut": c.subcut, "narration": c.narration, "bgm": c.bgm,
            "prompt_kr": c.prompt_kr, "prompt_en": c.prompt_en,
        } for c in sb.cuts]

    # 컷 ID가 있으면 ID로 매칭하고, 없으면 CUTS/ 정렬 순서와 콘티 row 순서를 매칭.
    sorted_cut_ids = [c["cut_id"] for c in cuts_picker]
    sb_by_id = {c.cut_id: c for c in sb_cuts if c.cut_id}
    sb_match = {}
    for i, cid in enumerate(sorted_cut_ids):
        sb_match[cid] = sb_by_id.get(cid) or (sb_cuts[i] if i < len(sb_cuts) else None)

    video_dir = video_cache_dir(out_dir)
    video_dir.mkdir(parents=True, exist_ok=True)

    cuts_unified = []
    for c in cuts_picker:
        cut_id = c["cut_id"]
        selected = selections.get(cut_id)
        entry = {
            "cut_id": cut_id,
            "prompt": c["prompt"],
            "media": c["media"],
            "selected": selected,
        }
        if selected:
            sel_path = media_path_for_selection(out_dir, cut_id, selected)
            sb_cut = sb_match.get(cut_id)
            is_external_video = sel_path.suffix.lower() in VIDEO_EXTS
            entry["selected_type"] = "video" if is_external_video else "image"
            entry["is_external_video"] = is_external_video

            if is_external_video:
                # 외부 영상: 영상 자체가 컷. base 재렌더 X, motion 적용 X.
                duration = get_duration(sel_path) if sel_path.is_file() else 0.0
                # subtitle 은 외부 영상에서도 사용 가능 (burn 처리)
                if cut_id in timings and "start" in timings[cut_id]:
                    t = timings[cut_id]
                    sub_start = float(t.get("start", 0))
                    sub_text = str(t.get("text", ""))
                elif sb_cut:
                    sub_start, sub_text = extract_subtitle(sb_cut.subcut)
                    if not sub_text:
                        sub_text = ""  # 외부 영상엔 default 자막 안 입힘
                        sub_start = 0.0
                else:
                    sub_start, sub_text = 0.0, ""

                entry.update({
                    # 영상 자체를 직접 서빙 (base 없음)
                    "video_url": f"/img/{urllib.parse.quote(cut_id, safe='')}/{urllib.parse.quote(selected, safe='')}",
                    "image_url": None,
                    "duration": duration,
                    "subtitle_start": sub_start,
                    "subtitle_text": sub_text,
                    "narration": sb_cut.narration if sb_cut else "",
                    "motion": None,  # 외부 영상엔 motion 불가
                })
            else:
                # 이미지 흐름 (기존)
                image_path = sel_path
                base_path, duration = ensure_base_video(video_dir, cut_id, image_path, font)
                if base_path:
                    if cut_id in timings and "start" in timings[cut_id]:
                        t = timings[cut_id]
                        sub_start = float(t.get("start", 0))
                        sub_text = str(t.get("text", ""))
                    elif sb_cut:
                        sub_start, sub_text = extract_subtitle(sb_cut.subcut)
                        if not sub_text:
                            sub_text = sb_cut.narration
                            sub_start = max(0, duration - 1.0)
                    else:
                        sub_start, sub_text = 0.0, ""

                    m = timings.get(cut_id, {}).get("motion", {})
                    motion = {
                        "type": m.get("type", "zoom_in"),
                        "focal_x": float(m.get("focal_x", 0.5)),
                        "focal_y": float(m.get("focal_y", 0.5)),
                        "zoom_amount": float(m.get("zoom_amount", 0.10)),
                    }

                    entry.update({
                        "video_url": f"/video/{urllib.parse.quote(cut_id)}",
                        "image_url": f"/image/{urllib.parse.quote(cut_id)}",
                        "duration": duration,
                        "subtitle_start": sub_start,
                        "subtitle_text": sub_text,
                        "narration": sb_cut.narration if sb_cut else "",
                        "motion": motion,
                    })

        cuts_unified.append(entry)

    return {
        "title": title,
        "output_dir": str(out_dir),
        "cuts": cuts_unified,
        "storyboard_cuts": storyboard_cuts,
        "selections": selections,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────────────────────

def _make_handler(state):
    font: str = state["font"]
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, code: int, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_html(self, fname: str):
            html = (HTML_DIR / fname).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _serve_image(self, img_path: Path):
            ext = img_path.suffix.lower()
            ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
            data = img_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def _serve_video(self, path: Path):
            ctype = CONTENT_TYPES.get(path.suffix.lower(), "video/mp4")
            file_size = path.stat().st_size
            range_header = self.headers.get("Range")
            if range_header:
                m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else file_size - 1
                    end = min(end, file_size - 1)
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    with path.open("rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            try:
                                self.wfile.write(chunk)
                            except (BrokenPipeError, ConnectionResetError):
                                return
                            remaining -= len(chunk)
                    return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError):
                return

        def _serve_local_file(self, path: Path):
            ext = path.suffix.lower()
            if ext not in PROJECT_FILE_EXTS:
                self.send_error(404)
                return
            ctype = CONTENT_TYPES.get(ext)
            if not ctype:
                ctype = "application/json; charset=utf-8" if ext == ".json" else "text/plain; charset=utf-8"
            if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
                self._serve_video(path)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            with lock:
                out_dir: Path = state["out_dir"]
                sb_path: Path | None = state["sb_path"]
                project_root: Path = state.get("project_root", out_dir)

            if path == "/":
                self.send_response(302); self.send_header("Location", "/files"); self.end_headers(); return
            if path == "/storyboard": self._serve_html("storyboard.html"); return
            if path == "/files": self._serve_html("files.html"); return
            if path == "/settings": self._serve_html("settings.html"); return
            if path == "/picker": self._serve_html("picker.html"); return
            if path == "/sync": self._serve_html("sync.html"); return
            if path == "/motion": self._serve_html("motion.html"); return
            if path == "/bgm": self._serve_html("bgm.html"); return

            if path == "/env_data":
                try:
                    env_path = ensure_env_file()
                    self._send_json(200, {
                        "ok": True,
                        "path": str(env_path),
                        "example_path": str(PACKAGE_ROOT / ".env.example"),
                        "text": env_path.read_text(encoding="utf-8"),
                    })
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e), "path": str(ENV_PATH), "text": ""})
                return

            if path == "/image_models":
                try:
                    cfg = Config.load()
                    self._send_json(200, {
                        "ok": True,
                        "default_model": cfg.model,
                        "models": cfg.image_models,
                        "default_size": cfg.default_size,
                        "default_quality": cfg.default_quality,
                        "sizes": ["1536x1024", "1024x1024", "1024x1536"],
                        "qualities": ["low", "medium", "high", "auto"],
                    })
                except Exception as e:
                    self._send_json(200, {
                        "ok": False,
                        "error": str(e),
                        "default_model": "gpt-image-1",
                        "models": ["gpt-image-1"],
                        "default_size": "1536x1024",
                        "default_quality": "medium",
                        "sizes": ["1536x1024", "1024x1024", "1024x1536"],
                        "qualities": ["low", "medium", "high", "auto"],
                    })
                return

            if path == "/storyboard_data":
                if not sb_path:
                    self._send_json(200, {
                        "ok": False,
                        "error": "CONTI.md 파일이 지정되지 않음",
                        "path": None,
                        "title": None,
                        "text": "",
                    })
                    return
                try:
                    text = sb_path.read_text(encoding="utf-8")
                    sb = parse_storyboard(sb_path)
                    self._send_json(200, {
                        "ok": True,
                        "path": str(sb_path),
                        "title": sb.title,
                        "cuts": len(sb.cuts),
                        "project_cut_ids": [c["cut_id"] for c in discover_cuts(out_dir)],
                        "text": text,
                    })
                except Exception as e:
                    self._send_json(500, {
                        "ok": False,
                        "error": str(e),
                        "path": str(sb_path),
                        "title": None,
                        "text": "",
                    })
                return

            if path == "/bgm_data":
                try:
                    ensure_bgm_storage(out_dir)
                    config = load_bgm_config(out_dir)
                    final = project_root_for_output(out_dir) / "RENDERS" / "final.mp4"
                    final_exists = final.is_file()
                    data = {
                        "ok": True,
                        "config_path": str(bgm_config_path(out_dir)),
                        "audio_dir": str(audio_dir(out_dir)),
                        "bgm_dir": str(bgm_dir(out_dir)),
                        "final_path": str(final),
                        "final_exists": final_exists,
                        "final_duration": get_duration(final) if final_exists else 0.0,
                        "output_path": str(project_root_for_output(out_dir) / "RENDERS" / "final_bgm.mp4"),
                        "tracks": discover_bgm_tracks(out_dir),
                        "config": config,
                    }
                    self._send_json(200, data)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return

            if path == "/files_data":
                q = urllib.parse.parse_qs(parsed.query)
                root_raw = q.get("path", [str(project_root)])[0].strip()
                try:
                    data = scan_local_folder(Path(root_raw or project_root))
                    data["ok"] = True
                    data["project_root"] = str(project_root)
                    data["project_output_dir"] = str(out_dir)
                    data["output_dir"] = str(out_dir)
                    data["storyboard"] = str(sb_path) if sb_path else None
                    self._send_json(200, data)
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/file_preview":
                q = urllib.parse.parse_qs(parsed.query)
                file_raw = q.get("path", [""])[0]
                try:
                    file_path = Path(file_raw).expanduser().resolve()
                    if not file_path.is_file():
                        self.send_error(404)
                        return
                    self._serve_local_file(file_path)
                except Exception:
                    self.send_error(404)
                return

            if path == "/data":
                data = build_data(out_dir, sb_path, font)
                self._send_json(200, data)
                return

            if path.startswith("/audio/"):
                track = urllib.parse.unquote(path[len("/audio/"):])
                try:
                    audio_path = resolve_bgm_track(out_dir, track)
                    self._serve_video(audio_path)
                except Exception:
                    self.send_error(404)
                return

            if path.startswith("/img/"):
                # 이미지 + 외부 영상 후보 둘 다 처리. 영상은 Range 지원 필요.
                parts = path.split("/", 3)
                if len(parts) != 4:
                    self.send_error(400); return
                cut_id = urllib.parse.unquote(parts[2])
                fname = urllib.parse.unquote(parts[3])
                if "/" in cut_id or ".." in cut_id:
                    self.send_error(403); return
                try:
                    media_path = media_path_for_selection(out_dir, cut_id, fname)
                except Exception:
                    self.send_error(403); return
                if not media_path.is_file() or media_path.suffix.lower() not in MEDIA_EXTS:
                    self.send_error(404); return
                if media_path.suffix.lower() in VIDEO_EXTS:
                    self._serve_video(media_path)
                else:
                    self._serve_image(media_path)
                return

            if path.startswith("/image/"):
                cut_id = urllib.parse.unquote(path[len("/image/"):])
                if "/" in cut_id or ".." in cut_id:
                    self.send_error(403); return
                sel_p = selections_path(out_dir)
                if not sel_p.exists():
                    self.send_error(404); return
                fname = json.loads(sel_p.read_text(encoding="utf-8")).get("selections", {}).get(cut_id)
                if not fname:
                    self.send_error(404); return
                try:
                    img_path = media_path_for_selection(out_dir, cut_id, fname)
                except Exception:
                    self.send_error(403); return
                if not img_path.is_file():
                    self.send_error(404); return
                self._serve_image(img_path)
                return

            if path.startswith("/video/"):
                cut_id = urllib.parse.unquote(path[len("/video/"):])
                if "/" in cut_id or ".." in cut_id:
                    self.send_error(403); return
                video_dir = video_cache_dir(out_dir)
                base = (video_dir / f"{cut_id}_base.mp4").resolve()
                try:
                    base.relative_to(video_dir.resolve())
                except ValueError:
                    self.send_error(403); return
                if not base.is_file():
                    self.send_error(404); return
                self._serve_video(base)
                return

            self.send_error(404)

        def do_POST(self):
            parsed_post = urllib.parse.urlparse(self.path)
            path = parsed_post.path
            with lock:
                out_dir: Path = state["out_dir"]
                sb_path: Path | None = state["sb_path"]
                project_root: Path = state.get("project_root", out_dir)

            if path == "/set_project":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    root = Path(str(payload.get("path") or project_root))
                    with lock:
                        project_dir, project_out_dir, project_sb = set_project_dir(state, root)
                    self._send_json(200, {
                        "ok": True,
                        "path": str(project_dir),
                        "project_root": str(project_dir),
                        "output_dir": str(project_out_dir),
                        "project_output_dir": str(project_out_dir),
                        "storyboard": str(project_sb) if project_sb else None,
                    })
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/save":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    sels = payload.get("selections", {})
                    if not isinstance(sels, dict):
                        raise ValueError("selections must be object")
                    clean = {}
                    for cut_id, fname in sels.items():
                        if not isinstance(cut_id, str) or not isinstance(fname, str):
                            continue
                        if "/" in cut_id or ".." in cut_id:
                            continue
                        try:
                            selected_path = media_path_for_selection(out_dir, cut_id, fname)
                        except Exception:
                            continue
                        if selected_path.is_file():
                            clean[cut_id] = fname
                    with lock:
                        save_selections(out_dir, sb_path, clean)
                    self._send_json(200, {"ok": True, "saved": len(clean)})
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/save_env":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    text = payload.get("text", "")
                    if not isinstance(text, str):
                        raise ValueError("text must be string")
                    save_env_text(text)
                    self._send_json(200, {"ok": True, "path": str(ENV_PATH)})
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/choose_folder":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    start = Path(str(payload.get("path") or project_root))
                    chosen = choose_folder_with_dialog(start)
                    if chosen is None:
                        self._send_json(200, {"ok": False, "canceled": True})
                    else:
                        with lock:
                            project_dir, project_out_dir, project_sb = set_project_dir(state, chosen)
                        self._send_json(200, {
                            "ok": True,
                            "path": str(project_dir),
                            "project_root": str(project_dir),
                            "output_dir": str(project_out_dir),
                            "project_output_dir": str(project_out_dir),
                            "storyboard": str(project_sb) if project_sb else None,
                        })
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/generate_image":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    cut_id = str(payload.get("cut_id", ""))
                    if not cut_id or "/" in cut_id or ".." in cut_id:
                        raise ValueError("invalid cut_id")
                    if not (out_dir / cut_id).is_dir():
                        raise ValueError(f"cut 폴더 없음: {cut_id}")

                    cfg = Config.load()
                    model = str(payload.get("model") or cfg.model).strip()
                    if model not in cfg.image_models:
                        raise ValueError(f"허용되지 않은 모델: {model}")
                    size = str(payload.get("size") or cfg.default_size).strip()
                    quality = str(payload.get("quality") or cfg.default_quality).strip()
                    if quality not in ("low", "medium", "high", "auto"):
                        raise ValueError(f"invalid quality: {quality}")

                    prompt_override = payload.get("prompt_override", "")
                    if not isinstance(prompt_override, str):
                        raise ValueError("prompt_override must be string")
                    prompt = prompt_override.strip()
                    prompt_source = "custom" if prompt else "prompt.md"
                    if not prompt:
                        prompt = prompt_for_cut(out_dir, sb_path, cut_id)
                    model_dir = re.sub(r"[^A-Za-z0-9_.-]+", "-", model).strip("-") or "model"
                    output_base = out_dir / cut_id / "images" / "openai" / model_dir
                    provider_cut_id = ""
                    local_cfg = replace(cfg, model=model, output_dir=output_base)
                    with lock:
                        images = generate_openai(
                            prompt=prompt,
                            cut_id=provider_cut_id,
                            n=1,
                            size=size,
                            quality=quality,
                            config=local_cfg,
                            model=model,
                        )
                    image_entries = [{
                        "name": img.path.relative_to(out_dir / cut_id).as_posix(),
                        "path": str(img.path),
                    } for img in images]
                    prompt_history = output_base / "_prompt_history.jsonl"
                    with prompt_history.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                            "cut_id": cut_id,
                            "model": model,
                            "prompt_source": prompt_source,
                            "prompt": prompt,
                            "images": image_entries,
                        }, ensure_ascii=False) + "\n")
                    self._send_json(200, {
                        "ok": True,
                        "cut_id": cut_id,
                        "model": model,
                        "prompt": prompt,
                        "prompt_source": prompt_source,
                        "images": image_entries,
                        "media": discover_cuts(out_dir),
                    })
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/save_storyboard":
                if not sb_path:
                    self._send_json(400, {"ok": False, "error": "CONTI.md 파일이 지정되지 않음"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    text = payload.get("text", "")
                    if not isinstance(text, str):
                        raise ValueError("text must be string")
                    with lock:
                        save_storyboard_text(sb_path, text)
                        sb = parse_storyboard(sb_path)
                    self._send_json(200, {
                        "ok": True,
                        "path": str(sb_path),
                        "title": sb.title,
                        "cuts": len(sb.cuts),
                    })
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/save_timings":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    timings = payload.get("timings", {})
                    if not isinstance(timings, dict):
                        raise ValueError("timings must be object")
                    with lock:
                        existing = load_timings(out_dir)
                        for cut_id, t in timings.items():
                            if not isinstance(cut_id, str) or "/" in cut_id or ".." in cut_id:
                                continue
                            cur = existing.get(cut_id, {})
                            cur["start"] = float(t.get("start", 0))
                            cur["text"] = str(t.get("text", ""))
                            existing[cut_id] = cur
                        save_timings(out_dir, existing)
                    self._send_json(200, {"ok": True, "saved": len(timings)})
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/save_motion":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    cut_id = str(payload.get("cut_id", ""))
                    motion = payload.get("motion", {})
                    if not cut_id or "/" in cut_id or ".." in cut_id:
                        raise ValueError("invalid cut_id")
                    if not isinstance(motion, dict):
                        raise ValueError("motion must be object")
                    mtype = str(motion.get("type", "zoom_in"))
                    if mtype not in ("zoom_in", "zoom_out", "pan_lr", "pan_rl", "shake", "static"):
                        mtype = "zoom_in"
                    with lock:
                        existing = load_timings(out_dir)
                        cur = existing.get(cut_id, {})
                        cur["motion"] = {
                            "type": mtype,
                            "focal_x": max(0.0, min(1.0, float(motion.get("focal_x", 0.5)))),
                            "focal_y": max(0.0, min(1.0, float(motion.get("focal_y", 0.5)))),
                            "zoom_amount": max(0.0, min(0.50, float(motion.get("zoom_amount", 0.10)))),
                        }
                        existing[cut_id] = cur
                        save_timings(out_dir, existing)
                    self._send_json(200, {"ok": True})
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/upload_bgm":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    q = urllib.parse.parse_qs(parsed_post.query)
                    filename = Path(str(q.get("filename", [""])[0])).name
                    if not filename:
                        raise ValueError("filename 필요")
                    if Path(filename).suffix.lower() not in AUDIO_EXTS:
                        raise ValueError("지원하는 오디오 파일: mp3, wav, m4a, aac, flac, ogg")
                    data = self.rfile.read(length)
                    if not data:
                        raise ValueError("빈 파일")
                    with lock:
                        _, bgm = ensure_bgm_storage(out_dir)
                        target = (bgm / filename).resolve()
                        target.relative_to(bgm.resolve())
                        tmp = target.with_suffix(target.suffix + ".tmp")
                        tmp.write_bytes(data)
                        tmp.replace(target)
                        rel = target.relative_to(audio_dir(out_dir)).as_posix()
                    self._send_json(200, {"ok": True, "track": rel, "path": str(target)})
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/save_bgm":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    config = payload.get("config", payload)
                    if not isinstance(config, dict):
                        raise ValueError("config must be object")
                    with lock:
                        clean = save_bgm_config(out_dir, config)
                    self._send_json(200, {"ok": True, "config": clean, "path": str(bgm_config_path(out_dir))})
                except Exception as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                return

            if path == "/render":
                log_buf = io.StringIO()
                try:
                    with lock:
                        with redirect_stdout(log_buf), redirect_stderr(log_buf):
                            result = render_final(out_dir, font, log_buf)
                    result["log"] = log_buf.getvalue()
                    self._send_json(200, result)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e),
                                          "log": log_buf.getvalue()})
                return

            if path == "/export_project":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    fmt = str(payload.get("format") or "finalcut")
                    with lock:
                        result = export_project(out_dir, sb_path, fmt)
                    status = 200 if result.get("ok") else 400
                    self._send_json(status, result)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e)})
                return

            if path == "/render_bgm":
                length = int(self.headers.get("Content-Length", 0))
                log_buf = io.StringIO()
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    config = payload.get("config", payload)
                    if not isinstance(config, dict):
                        raise ValueError("config must be object")
                    with lock:
                        with redirect_stdout(log_buf), redirect_stderr(log_buf):
                            result = render_bgm_mix(out_dir, config, log_buf)
                    result["log"] = log_buf.getvalue()
                    self._send_json(200, result)
                except Exception as e:
                    self._send_json(500, {"ok": False, "error": str(e),
                                          "log": log_buf.getvalue()})
                return

            self.send_error(404)

    return Handler


class _ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="img_gen.editor",
        description="SSCUT 픽커 + 싱크 + 모션 통합 로컬 서버.",
    )
    p.add_argument("project_dir", type=Path, help="SSCUT.json 이 있는 프로젝트 루트")
    p.add_argument("--port", type=int, default=8765,
                   help="시작 포트 (점유 시 +20 까지 자동 fallback)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--landing", default="files", choices=["files", "storyboard", "settings", "picker", "motion", "sync", "bgm"],
                   help="브라우저 자동 열기 시작 페이지 (default: files)")
    args = p.parse_args(argv)

    try:
        project_root, out_dir, sb_path = resolve_sscut_project(args.project_dir)
    except Exception as e:
        sys.exit(f"[editor] SSCUT 프로젝트를 열 수 없음: {e}")

    font = find_font()
    print(f"[editor] font: {font}")

    state = {"project_root": project_root, "out_dir": out_dir, "sb_path": sb_path, "font": font}
    handler = _make_handler(state)

    httpd = None
    chosen_port = None
    for try_port in range(args.port, args.port + 20):
        try:
            httpd = _ReusableTCPServer(("127.0.0.1", try_port), handler)
            chosen_port = try_port
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit(f"[editor] 포트 {args.port}-{args.port+19} 모두 점유 중")

    base_url = f"http://127.0.0.1:{chosen_port}"
    print(f"[editor] 열기: {base_url}/  (Ctrl+C 로 종료)")
    print(f"[editor]   {base_url}/files  {base_url}/storyboard  {base_url}/settings  {base_url}/picker  {base_url}/motion  {base_url}/sync  {base_url}/bgm")
    print(f"[editor] selected 컷의 base 영상은 첫 /data 호출 시 lazy 생성")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(f"{base_url}/{args.landing}")).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[editor] 종료.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
