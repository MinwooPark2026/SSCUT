#!/usr/bin/env python3
"""Migrate a legacy cat test project into SSCUT v1 layout."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
DEFAULT_BGM_CONFIG = {
    "schema": "sscut-bgm-v1",
    "selected_clip_id": "",
    "clips": [],
}


def read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def title_from_markdown(path: Path) -> str:
    if not path.is_file():
        return path.parent.name
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or path.parent.name
    return path.parent.name


def find_conti(src_root: Path) -> Path | None:
    preferred = sorted(src_root.glob("*.storyboard.md"))
    if preferred:
        return preferred[0]
    markdowns = sorted(p for p in src_root.glob("*.md") if p.is_file())
    return markdowns[0] if markdowns else None


def migrate(src_root: Path, dst_root: Path) -> None:
    src_root = src_root.expanduser().resolve()
    dst_root = dst_root.expanduser().resolve()
    legacy_out = src_root / "out_test"
    if not src_root.is_dir():
        raise SystemExit(f"source folder not found: {src_root}")
    if not legacy_out.is_dir():
        raise SystemExit(f"legacy out_test folder not found: {legacy_out}")
    if dst_root.exists() and any(dst_root.iterdir()):
        raise SystemExit(f"destination exists and is not empty: {dst_root}")

    dst_root.mkdir(parents=True, exist_ok=True)
    cuts_dir = dst_root / "CUTS"
    cut_order: list[str] = []
    media_map: dict[str, dict[str, str]] = {}

    conti = find_conti(src_root)
    if conti:
        copy_file(conti, dst_root / "CONTI.md")
    for md in sorted(src_root.glob("*.md")):
        if conti and md.resolve() == conti.resolve():
            continue
        copy_file(md, dst_root / "SCRIPT.md")
        break

    for cut_src in sorted(p for p in legacy_out.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if cut_src.name in {"video"}:
            continue
        cut_id = cut_src.name
        cut_order.append(cut_id)
        cut_dst = cuts_dir / cut_id
        media_map[cut_id] = {}

        prompt = cut_src / "prompt.txt"
        if prompt.is_file():
            copy_file(prompt, cut_dst / "PROMPT.md")

        cut_meta = {
            "schema": "sscut-cut-v1",
            "cut_id": cut_id,
            "source_legacy_dir": str(cut_src),
        }
        write_json(cut_dst / "CUT.json", cut_meta)

        meta = cut_src / "_meta.txt"
        if meta.is_file():
            copy_file(meta, cut_dst / "images" / "openai" / "gpt-image-1" / "_meta.txt")

        for media in sorted(p for p in cut_src.iterdir() if p.is_file()):
            if media.name.startswith(".") or media.name in {"prompt.txt", "_meta.txt"}:
                continue
            ext = media.suffix.lower()
            if ext in IMAGE_EXTS:
                rel = Path("images") / "openai" / "gpt-image-1" / media.name
            elif ext in VIDEO_EXTS:
                rel = Path("videos") / "external" / media.name
            else:
                continue
            copy_file(media, cut_dst / rel)
            media_map[cut_id][media.name] = rel.as_posix()

    legacy_picks = read_json(legacy_out / "selections.json", {})
    picks = {
        "schema": "sscut-picks-v1",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "selections": {},
    }
    for cut_id, fname in (legacy_picks.get("selections") or {}).items():
        picks["selections"][cut_id] = media_map.get(cut_id, {}).get(fname, fname)
    write_json(dst_root / "PICKS.json", picks)

    timeline = read_json(legacy_out / "subtitle_timings.json", {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "timings": {},
    })
    timeline.setdefault("schema", "sscut-timeline-v1")
    write_json(dst_root / "TIMELINE.json", timeline)
    write_json(dst_root / "BGM.json", DEFAULT_BGM_CONFIG)
    (dst_root / "AUDIO" / "BGM").mkdir(parents=True, exist_ok=True)

    legacy_video = legacy_out / "video"
    if legacy_video.is_dir():
        cache_video = dst_root / "CACHE" / "video"
        for src in sorted(p for p in legacy_video.iterdir() if p.is_file()):
            copy_file(src, cache_video / src.name)
        final = legacy_video / "final.mp4"
        if final.is_file():
            copy_file(final, dst_root / "RENDERS" / "final.mp4")

    manifest = {
        "schema": "sscut-project-v1",
        "name": dst_root.name,
        "title": title_from_markdown(dst_root / "CONTI.md"),
        "app_goal": "External storyboard/source generation first; this app is for picking, timing, motion editing, render checks, and occasional extra image generation during edit.",
        "conti": "CONTI.md",
        "cuts_dir": "CUTS",
        "cut_order": cut_order,
        "picks": "PICKS.json",
        "timeline": "TIMELINE.json",
        "bgm": "BGM.json",
        "audio_dir": "AUDIO",
        "renders_dir": "RENDERS",
        "cache_dir": "CACHE",
        "created_from": str(src_root),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json(dst_root / "SSCUT.json", manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy out_test project to SSCUT v1.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    migrate(args.source, args.destination)
    print(f"[migrate] SSCUT project written: {args.destination.expanduser().resolve()}")


if __name__ == "__main__":
    main()
