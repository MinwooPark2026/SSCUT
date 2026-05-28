"""selections.json + storyboard + 선택 이미지 → 모션 + TTS + 자막 mp4 한 편.

    ./run.sh -m img_gen.video /path/to/out_test \\
        --storyboard /path/to/스크립트.storyboard.md

산출: <out_dir>/video/{cut_id}.mp4 (개별 컷, 캡컷 import 용) + final.mp4 (concat).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from openai import OpenAI

from .config import Config
from .storyboard import parse_storyboard

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

KOREAN_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/NanumGothic.ttf",
]


@dataclass
class CutPlan:
    cut_id: str
    image: Path
    duration: float
    narration: str
    subtitle: str
    subtitle_start: float


def find_font() -> str:
    for p in KOREAN_FONT_CANDIDATES:
        if Path(p).exists():
            return p
    raise RuntimeError(
        "Korean font 못 찾음. AppleSDGothicNeo 또는 NanumGothic 필요.\n"
        "확인한 경로:\n  " + "\n  ".join(KOREAN_FONT_CANDIDATES)
    )


def parse_time(time_str: str) -> tuple[float, float]:
    """'0:03.3~0:06.6' → (3.3, 6.6) seconds."""
    def parse_ts(s: str) -> float:
        s = s.strip()
        if ":" in s:
            mm, ss = s.split(":", 1)
            return float(mm) * 60 + float(ss)
        return float(s)
    parts = time_str.split("~")
    if len(parts) != 2:
        return 0.0, 0.0
    return parse_ts(parts[0]), parse_ts(parts[1])


def extract_subtitle(subcut_text: str) -> tuple[float, str]:
    """'2.3~3.3s 자막: "...텍스트..."' 또는 '자막: ...' → (start_seconds, text)."""
    # 따옴표 안의 자막
    m = re.search(
        r'(\d+(?:\.\d+)?)~\d+(?:\.\d+)?s?\s*자막:\s*["“]([^"”]+)["”]',
        subcut_text,
    )
    if m:
        return float(m.group(1)), m.group(2).strip()
    # 따옴표 없는 자막
    m = re.search(r'(\d+(?:\.\d+)?)~\d+(?:\.\d+)?s?\s*자막:\s*(.+?)(?:$|\n)', subcut_text)
    if m:
        return float(m.group(1)), m.group(2).strip()
    return 0.0, ""


def run_ffmpeg(cmd: list[str], desc: str = "") -> None:
    """ffmpeg subprocess, stderr 캡처. 실패 시 stderr 출력."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\n!!! ffmpeg 실패: {desc}", file=sys.stderr)
        print("CMD:", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
        print("STDERR:", r.stderr, file=sys.stderr)
        raise RuntimeError(f"ffmpeg failed (rc={r.returncode}): {desc}")


def get_duration(path: Path) -> float:
    """ffmpeg -i 의 stderr 에서 'Duration: HH:MM:SS.XX' 파싱."""
    r = subprocess.run([FFMPEG, "-i", str(path), "-hide_banner"],
                       capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        raise RuntimeError(f"duration 파싱 실패: {path}\nstderr: {r.stderr[:500]}")
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)


def make_motion_clip(image: Path, duration: float, output: Path,
                     motion_type: str = "zoom_in",
                     focal_x: float = 0.5, focal_y: float = 0.5,
                     zoom_amount: float = 0.10, fps: int = 30) -> None:
    """이미지 → 모션 1920x1080 mp4 (오디오 없음). 임의 해상도 입력 OK.

    입력 이미지는 원본 비율을 유지한 채 1920x1080 을 덮도록 확대하고,
    넘치는 가장자리는 잘라낸다.

    motion_type: 'zoom_in' / 'zoom_out' / 'pan_lr' / 'pan_rl' / 'shake' / 'static'
    focal_x, focal_y: [0,1] 정규화 초점 (0.5,0.5 = 중앙)
    zoom_amount: 0~0.50 (10% 권장). pan/shake 는 최소 5% 자동 적용 (이동 여유).
    """
    total_frames = max(int(round(duration * fps)), 2)
    fx = max(0.0, min(1.0, focal_x))
    fy = max(0.0, min(1.0, focal_y))
    za = max(0.0, min(0.50, zoom_amount))
    N = total_frames

    # 기본 focal 기반 x/y (zoom_in/out 용 — 초점이 화면 중앙으로 끌려옴)
    x_focal = f"clip(ow*({fx:.4f}*zoom-0.5)\\,0\\,iw*zoom-ow)"
    y_focal = f"clip(oh*({fy:.4f}*zoom-0.5)\\,0\\,ih*zoom-oh)"

    if motion_type == "zoom_in":
        z_expr = f"1.0+{za:.4f}*on/{N-1}"
        x_expr, y_expr = x_focal, y_focal
    elif motion_type == "zoom_out":
        z_expr = f"{1.0+za:.4f}-{za:.4f}*on/{N-1}"
        x_expr, y_expr = x_focal, y_focal
    elif motion_type == "pan_lr":
        # scale 고정 10%, 슬라이더는 속도 (cycles per duration). 기본 1x = 정확히 한 번 끝.
        z_expr = f"{1.10:.4f}"
        speed = max(0.1, za * 10)
        # progress = min(speed * on/(N-1), 1) — speed>1 시 일찍 끝나고 hold
        x_expr = f"(iw*zoom-ow)*min({speed:.4f}*on/{N-1}\\,1)"
        y_expr = y_focal
    elif motion_type == "pan_rl":
        z_expr = f"{1.10:.4f}"
        speed = max(0.1, za * 10)
        x_expr = f"(iw*zoom-ow)*(1-min({speed:.4f}*on/{N-1}\\,1))"
        y_expr = y_focal
    elif motion_type == "shake":
        # scale 고정 5% (사용자 요청). 슬라이더는 진동수 (Hz). 기본 2 Hz.
        # 진폭은 가용 범위의 절반 (±half range) → 시각적으로 명료.
        z_expr = f"{1.05:.4f}"
        freq = max(0.5, za * 20)
        x_center = f"ow*({fx:.4f}*zoom-0.5)"
        x_osc = f"(iw*zoom-ow)/2*sin(2*PI*{freq:.4f}*on/{fps})"
        x_expr = f"clip({x_center}+{x_osc}\\,0\\,iw*zoom-ow)"
        y_expr = y_focal
    elif motion_type == "static":
        z_expr = "1.0"
        x_expr = "0"
        y_expr = "0"
    else:  # fallback
        z_expr = f"1.0+{za:.4f}*on/{N-1}"
        x_expr, y_expr = x_focal, y_focal

    # 입력 해상도 무관: 16:9 로 cover-fit 후 crop.
    vf = (
        f"scale=1920:1080:force_original_aspect_ratio=increase,"
        f"crop=1920:1080,"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':"
        f"d=1:s=1920x1080:fps={fps}"
    )
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-t", f"{duration}",
        "-i", str(image),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ]
    run_ffmpeg(cmd, desc=f"motion {image.name} → {output.name}")


def synth_tts(client: OpenAI, text: str, output: Path, voice: str = "nova") -> str:
    """OpenAI TTS → mp3. gpt-4o-mini-tts → tts-1 fallback. 사용한 모델 반환."""
    output.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for model in ("gpt-4o-mini-tts", "tts-1"):
        try:
            with client.audio.speech.with_streaming_response.create(
                model=model, voice=voice, input=text, response_format="mp3",
            ) as resp:
                resp.stream_to_file(str(output))
            return model
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"TTS 실패 (모든 모델): {last_err}")


def mux_cut(
    motion: Path, audio: Path | None, subtitle: str, subtitle_start: float,
    duration: float, font: str, output: Path,
) -> None:
    """모션 + (옵션) 오디오 + (옵션) 자막 → 최종 컷 mp4.

    subtitle="" 또는 None 이면 drawtext 없이 base 영상 (sync 도구의 preview 용).
    """
    has_subtitle = bool(subtitle and subtitle.strip())
    sub_file: Path | None = None
    vf_args: list[str] = []

    if has_subtitle:
        sub_file = output.with_name(output.stem + "_sub.txt")
        sub_file.write_text(subtitle, encoding="utf-8")
        sub_file_escaped = str(sub_file).replace(":", r"\:")
        drawtext = (
            f"drawtext="
            f"fontfile={font}:"
            f"textfile={sub_file_escaped}:"
            f"fontsize=54:fontcolor=white:"
            f"borderw=4:bordercolor=black@0.85:"
            f"box=1:boxcolor=black@0.35:boxborderw=18:"
            f"x=(w-text_w)/2:y=h-180:"
            f"enable='gte(t,{subtitle_start})'"
        )
        vf_args = ["-vf", drawtext]

    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(motion)]
    if audio is not None:
        cmd += ["-i", str(audio)]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += vf_args
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-t", f"{duration}",
        "-map", "0:v:0", "-map", "1:a:0",
        str(output),
    ]
    run_ffmpeg(cmd, desc=f"mux → {output.name}")
    if sub_file is not None:
        sub_file.unlink(missing_ok=True)


def concat_clips(clips: list[Path], output: Path) -> None:
    """concat demuxer 로 mp4 합치기. 모든 클립이 같은 codec/resolution 가정."""
    list_file = output.with_name(output.stem + "_concat.txt")
    list_file.write_text(
        "\n".join(f"file '{c.resolve()}'" for c in clips), encoding="utf-8"
    )
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy",
        str(output),
    ]
    run_ffmpeg(cmd, desc=f"concat → {output.name}")
    list_file.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="img_gen.video",
        description="selections.json + storyboard + 이미지 → 샘플 영상 1편",
    )
    p.add_argument("output_dir", type=Path, help="selections.json 이 있는 폴더")
    p.add_argument("--storyboard", type=Path, required=True, help="스토리보드 .md")
    p.add_argument("--voice", default="nova",
                   help="OpenAI TTS voice (alloy/echo/nova/shimmer 등, default: nova)")
    p.add_argument("--no-tts", action="store_true", help="TTS 생략 (무음)")
    p.add_argument("--zoom", type=float, default=1.10,
                   help="줌 종료 배율 (1.0 = 정지, default: 1.10)")
    args = p.parse_args(argv)

    out_dir = args.output_dir.expanduser().resolve()
    sel_path = out_dir / "selections.json"
    if not sel_path.is_file():
        sys.exit(f"[video] selections.json 없음: {sel_path}")

    sel_data = json.loads(sel_path.read_text(encoding="utf-8"))
    selections: dict[str, str] = sel_data.get("selections", {})
    if not selections:
        sys.exit(f"[video] selections 비어있음")

    sb = parse_storyboard(args.storyboard)
    print(f"[video] 스토리보드: {sb.title} ({len(sb.cuts)} cuts)")
    print(f"[video] 선택된 컷: {len(selections)}")

    font = find_font()
    print(f"[video] font: {font}")

    # 컷 정렬 = 폴더명 sort. storyboard.cuts 도 sort 순으로 매칭.
    sorted_cut_ids = sorted(selections.keys())
    plans: list[CutPlan] = []
    for i, cut_id in enumerate(sorted_cut_ids):
        if i >= len(sb.cuts):
            print(f"[video] !! {cut_id}: 매칭 스토리보드 컷 없음, 스킵")
            continue
        sb_cut = sb.cuts[i]
        start, end = parse_time(sb_cut.time)
        duration = max(end - start, 1.0) if end > start else 3.0
        sub_start, sub_text = extract_subtitle(sb_cut.subcut)
        if not sub_text:
            sub_text = sb_cut.narration  # fallback
            sub_start = max(0, duration - 1.0)
        plans.append(CutPlan(
            cut_id=cut_id,
            image=out_dir / cut_id / selections[cut_id],
            duration=duration,
            narration=sb_cut.narration,
            subtitle=sub_text,
            subtitle_start=sub_start,
        ))

    if not plans:
        sys.exit("[video] 실행 가능한 plan 없음")

    video_dir = out_dir / "video"
    video_dir.mkdir(exist_ok=True)

    client = None
    if not args.no_tts:
        cfg = Config.load()
        client = OpenAI(api_key=cfg.api_key)

    cut_clips: list[Path] = []
    for idx, plan in enumerate(plans, 1):
        print(f"\n[video] ── {idx}/{len(plans)} {plan.cut_id} (target {plan.duration:.1f}s) ──")
        print(f"        나레이션: {plan.narration!r}")
        print(f"        자막 (start={plan.subtitle_start:.1f}s): {plan.subtitle!r}")

        # TTS 먼저 (길이 확정 위해)
        tts_path: Path | None = None
        if client and plan.narration:
            tts_path = video_dir / f"{plan.cut_id}_tts.mp3"
            tts_model = synth_tts(client, plan.narration, tts_path, args.voice)
            tts_dur = get_duration(tts_path)
            print(f"        TTS: {tts_model} voice={args.voice} → {tts_dur:.1f}s")
            actual_dur = max(plan.duration, tts_dur + 0.3)
            if actual_dur > plan.duration:
                print(f"        TTS 가 더 길어 cut 연장: {plan.duration:.1f}s → {actual_dur:.1f}s")
                plan.duration = actual_dur

        # 모션
        motion_path = video_dir / f"{plan.cut_id}_motion.mp4"
        za = max(0.0, args.zoom - 1.0)
        print(f"        모션 생성 (zoom_in → {args.zoom:.2f}, amount={za:.2f})...")
        make_motion_clip(plan.image, plan.duration, motion_path,
                         motion_type="zoom_in", zoom_amount=za)

        # mux: 모션 + TTS + 자막
        cut_path = video_dir / f"{plan.cut_id}.mp4"
        mux_cut(motion_path, tts_path, plan.subtitle,
                plan.subtitle_start, plan.duration, font, cut_path)

        # 중간 모션 파일 삭제
        motion_path.unlink(missing_ok=True)
        cut_clips.append(cut_path)
        print(f"        → {cut_path}")

    final = video_dir / "final.mp4"
    print(f"\n[video] concat {len(cut_clips)} clips → {final.name}")
    concat_clips(cut_clips, final)

    total = get_duration(final)
    size_mb = final.stat().st_size / 1024 / 1024
    print(f"\n[video] ✅ 완료")
    print(f"        최종 파일: {final}")
    print(f"        길이: {total:.1f}s, 크기: {size_mb:.1f}MB")
    print(f"        개별 컷도 {video_dir} 에 남아있음 (캡컷 import 가능)")


if __name__ == "__main__":
    main()
