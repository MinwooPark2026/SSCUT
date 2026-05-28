"""자막 싱크 조정 도구 — 영상 보며 시작 시각·텍스트 실시간 조절 후 final 재렌더.

    ./run.sh -m img_gen.sync /path/to/out_test \\
        --storyboard /path/to/스크립트.storyboard.md

전제: 먼저 `img_gen.video` 한 번 돌려서 {cut_id}.mp4 + {cut_id}_tts.mp3 가 있어야 함.
첫 실행 시 base 영상 ({cut_id}_base.mp4, 자막 없음) 한 번 생성. 이후엔 재사용.
"""
from __future__ import annotations

import argparse
import http.server
import io
import json
import re
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

from .config import Config
from .storyboard import parse_storyboard
from .video import (
    FFMPEG, concat_clips, find_font, get_duration,
    make_motion_clip, mux_cut, parse_time, extract_subtitle, synth_tts,
)


def load_timings(out_dir: Path) -> dict:
    p = out_dir / "subtitle_timings.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("timings", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_timings(out_dir: Path, timings: dict) -> None:
    p = out_dir / "subtitle_timings.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "timings": timings,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


def ensure_base_video(
    video_dir: Path, cut_id: str, image: Path, font: str,
) -> tuple[Path, float]:
    """{cut_id}_base.mp4 (자막 없음) 가 없으면 만들고 경로+duration 반환."""
    base = video_dir / f"{cut_id}_base.mp4"
    if base.exists():
        return base, get_duration(base)

    # 기존 final cut mp4 에서 duration 추출 (TTS 연장 반영된 정확한 값)
    existing = video_dir / f"{cut_id}.mp4"
    if not existing.exists():
        raise RuntimeError(
            f"기존 cut mp4 없음: {existing}\n"
            f"먼저 'img_gen.video' 를 실행해서 mp4·TTS 를 만든 뒤 sync 도구 사용."
        )
    duration = get_duration(existing)

    tts_path = video_dir / f"{cut_id}_tts.mp3"
    if not tts_path.exists():
        raise RuntimeError(f"TTS mp3 없음: {tts_path}")

    # 모션 새로 생성 → audio mux (자막 없음)
    motion_tmp = video_dir / f"{cut_id}_motion_tmp.mp4"
    print(f"[sync] base 생성: {cut_id} (duration={duration:.1f}s)")
    make_motion_clip(image, duration, motion_tmp)
    mux_cut(motion_tmp, tts_path, subtitle="", subtitle_start=0,
            duration=duration, font=font, output=base)
    motion_tmp.unlink(missing_ok=True)
    return base, duration


def build_initial_data(
    out_dir: Path, sb_path: Path, font: str,
) -> tuple[str, list[dict]]:
    """selections + storyboard 매칭, base 영상 보장, cuts 데이터 빌드."""
    sel_path = out_dir / "selections.json"
    if not sel_path.is_file():
        raise RuntimeError(f"selections.json 없음: {sel_path}")
    selections = json.loads(sel_path.read_text(encoding="utf-8")).get("selections", {})
    if not selections:
        raise RuntimeError("selections 비어있음")

    sb = parse_storyboard(sb_path)
    title = sb.title

    sorted_cut_ids = sorted(selections.keys())
    timings = load_timings(out_dir)
    video_dir = out_dir / "video"
    video_dir.mkdir(exist_ok=True)

    cuts = []
    for i, cut_id in enumerate(sorted_cut_ids):
        if i >= len(sb.cuts):
            print(f"[sync] !! {cut_id}: 스토리보드 매칭 없음, 스킵")
            continue
        sb_cut = sb.cuts[i]
        image = out_dir / cut_id / selections[cut_id]
        base_path, duration = ensure_base_video(video_dir, cut_id, image, font)

        # 저장된 timing 우선, 없으면 storyboard 기본값
        if cut_id in timings:
            t = timings[cut_id]
            sub_start = float(t.get("start", 0))
            sub_text = str(t.get("text", ""))
        else:
            sub_start, sub_text = extract_subtitle(sb_cut.subcut)
            if not sub_text:
                sub_text = sb_cut.narration
                sub_start = max(0, duration - 1.0)

        # motion 설정 (없으면 기본값)
        motion = timings.get(cut_id, {}).get("motion", {})
        motion_out = {
            "type": motion.get("type", "zoom_in"),
            "focal_x": float(motion.get("focal_x", 0.5)),
            "focal_y": float(motion.get("focal_y", 0.5)),
            "zoom_amount": float(motion.get("zoom_amount", 0.10)),
        }

        cuts.append({
            "cut_id": cut_id,
            "video_url": f"/video/{urllib.parse.quote(cut_id)}",
            "image_url": f"/image/{urllib.parse.quote(cut_id)}",
            "duration": duration,
            "subtitle_start": sub_start,
            "subtitle_text": sub_text,
            "narration": sb_cut.narration,
            "image": str(image),
            "motion": motion_out,
        })

    return title, cuts


def render_final(
    out_dir: Path, font: str, log_buf: io.StringIO,
) -> dict:
    """현재 timings (subtitle + motion) 로 각 컷 motion·base·subtitle 재렌더 + concat."""
    video_dir = out_dir / "video"
    timings = load_timings(out_dir)
    if not timings:
        return {"ok": False, "error": "timings 비어있음"}

    sel_path = out_dir / "selections.json"
    if not sel_path.is_file():
        return {"ok": False, "error": "selections.json 없음"}
    selections = json.loads(sel_path.read_text(encoding="utf-8")).get("selections", {})

    cut_paths: list[Path] = []
    for cut_id in sorted(timings.keys()):
        if cut_id not in selections:
            print(f"[render] {cut_id}: 선택 이미지 없음, 스킵", file=log_buf)
            continue

        base = video_dir / f"{cut_id}_base.mp4"
        tts_path = video_dir / f"{cut_id}_tts.mp3"

        # duration 확정: 기존 base 또는 TTS 길이 기반
        if base.exists():
            duration = get_duration(base)
        elif tts_path.exists():
            duration = max(3.0, get_duration(tts_path) + 0.3)
        else:
            return {"ok": False, "error": f"base 도 TTS 도 없음: {cut_id}"}

        t = timings[cut_id]
        motion = t.get("motion", {}) or {}
        m_type = motion.get("type", "zoom_in")
        m_fx = float(motion.get("focal_x", 0.5))
        m_fy = float(motion.get("focal_y", 0.5))
        m_amt = float(motion.get("zoom_amount", 0.10))

        sub_start = float(t.get("start", 0))
        sub_text = str(t.get("text", "")).strip()

        print(f"[render] {cut_id}: motion={m_type} focal=({m_fx:.2f},{m_fy:.2f}) "
              f"amt={m_amt:.2f}, sub start={sub_start:.2f}s {sub_text!r}", file=log_buf)

        # 1) motion 재생성 (현재 설정으로)
        image = out_dir / cut_id / selections[cut_id]
        motion_tmp = video_dir / f"{cut_id}_motion_tmp.mp4"
        make_motion_clip(
            image, duration, motion_tmp,
            motion_type=m_type, focal_x=m_fx, focal_y=m_fy, zoom_amount=m_amt,
        )

        # 2) motion + audio mux → base 갱신 (자막 없음)
        mux_cut(motion_tmp, tts_path if tts_path.exists() else None,
                subtitle="", subtitle_start=0,
                duration=duration, font=font, output=base)
        motion_tmp.unlink(missing_ok=True)

        # 3) base 위 자막 burn-in → cut.mp4
        cut_path = video_dir / f"{cut_id}.mp4"
        _burn_subtitle(base, sub_text, sub_start, duration, font, cut_path, log_buf)
        cut_paths.append(cut_path)

    if not cut_paths:
        return {"ok": False, "error": "렌더 가능한 컷 없음"}

    final = video_dir / "final.mp4"
    print(f"[render] concat → {final.name}", file=log_buf)
    concat_clips(cut_paths, final)
    total = get_duration(final)
    size_mb = final.stat().st_size / 1024 / 1024
    print(f"[render] 완료: {total:.1f}s, {size_mb:.1f}MB", file=log_buf)

    return {
        "ok": True,
        "final_path": str(final),
        "duration": total,
        "size_mb": size_mb,
    }


def _burn_subtitle(
    base: Path, subtitle: str, start: float, duration: float,
    font: str, output: Path, log_buf: io.StringIO | None = None,
) -> None:
    """base 영상 (motion+audio) 에 자막만 얹어 새 mp4. 비디오·오디오 재인코딩."""
    import subprocess
    has_sub = bool(subtitle and subtitle.strip())
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(base)]
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
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
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


def _make_handler(state):
    out_dir: Path = state["out_dir"]
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

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path

            if path in ("/", "/index.html"):
                html = (Path(__file__).parent / "sync.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if path == "/motion":
                html = (Path(__file__).parent / "motion.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if path.startswith("/image/"):
                cut_id = urllib.parse.unquote(path[len("/image/"):])
                if "/" in cut_id or ".." in cut_id:
                    self.send_error(403); return
                # 선택된 이미지 경로 (selections.json 참조)
                sel_data = json.loads((out_dir / "selections.json").read_text(encoding="utf-8"))
                fname = sel_data.get("selections", {}).get(cut_id)
                if not fname:
                    self.send_error(404); return
                img_path = (out_dir / cut_id / fname).resolve()
                try:
                    img_path.relative_to(out_dir.resolve())
                except ValueError:
                    self.send_error(403); return
                if not img_path.is_file():
                    self.send_error(404); return
                ext = img_path.suffix.lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
                data = img_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/data":
                # cuts 재조립 (timings 최신 반영)
                with lock:
                    title, cuts = build_initial_data(out_dir, state["sb_path"], font)
                self._send_json(200, {"title": title, "cuts": cuts})
                return

            if path.startswith("/video/"):
                cut_id = urllib.parse.unquote(path[len("/video/"):])
                if "/" in cut_id or ".." in cut_id:
                    self.send_error(403); return
                base = (out_dir / "video" / f"{cut_id}_base.mp4").resolve()
                try:
                    base.relative_to((out_dir / "video").resolve())
                except ValueError:
                    self.send_error(403); return
                if not base.is_file():
                    self.send_error(404); return
                self._serve_video(base)
                return

            self.send_error(404)

        def _serve_video(self, path: Path):
            """간단 byte-range 지원 → HTML5 video 스크럽 가능."""
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
                    self.send_header("Content-Type", "video/mp4")
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
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
            # 전체 전송
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(path.read_bytes())

        def do_POST(self):
            if self.path == "/save_timings":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = self.rfile.read(length).decode("utf-8")
                    payload = json.loads(body)
                    timings = payload.get("timings", {})
                    if not isinstance(timings, dict):
                        raise ValueError("timings must be object")
                    with lock:
                        existing = load_timings(out_dir)  # 기존 (motion 포함) 읽기
                        for cut_id, t in timings.items():
                            if not isinstance(cut_id, str):
                                continue
                            if "/" in cut_id or ".." in cut_id:
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

            if self.path == "/save_motion":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = self.rfile.read(length).decode("utf-8")
                    payload = json.loads(body)
                    cut_id = str(payload.get("cut_id", ""))
                    motion = payload.get("motion", {})
                    if not cut_id or "/" in cut_id or ".." in cut_id:
                        raise ValueError("invalid cut_id")
                    if not isinstance(motion, dict):
                        raise ValueError("motion must be object")
                    motion_type = str(motion.get("type", "zoom_in"))
                    if motion_type not in ("zoom_in", "zoom_out", "static"):
                        motion_type = "zoom_in"
                    with lock:
                        existing = load_timings(out_dir)
                        cur = existing.get(cut_id, {})
                        cur["motion"] = {
                            "type": motion_type,
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

            if self.path == "/render":
                log_buf = io.StringIO()
                try:
                    with lock:
                        with redirect_stdout(log_buf), redirect_stderr(log_buf):
                            result = render_final(out_dir, font, log_buf)
                    result["log"] = log_buf.getvalue()
                    self._send_json(200, result)
                except Exception as e:
                    self._send_json(500, {
                        "ok": False, "error": str(e),
                        "log": log_buf.getvalue(),
                    })
                return

            self.send_error(404)

    return Handler


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="img_gen.sync",
        description="자막 싱크 조정 도구 (영상 보면서 start·text 실시간 편집 + final 재렌더)",
    )
    p.add_argument("output_dir", type=Path,
                   help="selections.json + video/ 가 있는 폴더 (img_gen.video 먼저 실행)")
    p.add_argument("--storyboard", type=Path, required=True, help="스토리보드 .md")
    p.add_argument("--port", type=int, default=8766,
                   help="시작 포트 (점유 시 +20 까지 자동 fallback)")
    p.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 비활성")
    args = p.parse_args(argv)

    out_dir = args.output_dir.expanduser().resolve()
    if not out_dir.is_dir():
        sys.exit(f"[sync] output_dir 없음: {out_dir}")

    sb_path = args.storyboard.expanduser().resolve()
    if not sb_path.is_file():
        sys.exit(f"[sync] storyboard 없음: {sb_path}")

    font = find_font()
    print(f"[sync] font: {font}")

    # 첫 호출: base 영상 만들고 cuts 구성 (몇십 초 걸릴 수 있음)
    print(f"[sync] base 영상 준비 중 (없으면 첫 한 번만 생성)...")
    title, cuts = build_initial_data(out_dir, sb_path, font)
    print(f"[sync] {title} — {len(cuts)} cuts 준비 완료")

    state = {"out_dir": out_dir, "sb_path": sb_path, "font": font}
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
        sys.exit(f"[sync] 포트 {args.port}-{args.port+19} 모두 점유 중")

    url = f"http://127.0.0.1:{chosen_port}/"
    print(f"[sync] 열기: {url}  (Ctrl+C 로 종료)")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[sync] 종료. → {out_dir / 'subtitle_timings.json'}")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
