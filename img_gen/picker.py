"""로컬 HTML 픽커 — out_test 폴더 컷별 후보 이미지를 한 화면에서 비교·선택.

    ./run.sh -m img_gen.picker /path/to/out_test \\
        --storyboard /path/to/스크립트.storyboard.md

자동으로 브라우저 열림. 클릭 즉시 `<out_test>/selections.json` 저장.
다음 단계(모션·영상)는 그 JSON 읽어서 선택된 이미지만 사용.
"""
from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

from .storyboard import parse_storyboard

HTML_PATH = Path(__file__).resolve().parent / "picker.html"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def discover_cuts(output_dir: Path) -> list[dict]:
    """output_dir 의 하위 폴더 = 컷. 각 폴더에서 prompt.txt + 이미지 파일 수집."""
    cuts = []
    for folder in sorted(output_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith("_") or folder.name.startswith("."):
            continue
        prompt_path = folder / "prompt.txt"
        prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.exists() else ""
        images = sorted(
            p.name for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            and not p.name.startswith("_") and not p.name.startswith(".")
        )
        if not images:
            continue  # 이미지 없는 폴더 (예: video/ 산출물) 스킵
        cuts.append({"cut_id": folder.name, "prompt": prompt, "images": images})
    return cuts


def load_selections(output_dir: Path) -> dict:
    p = output_dir / "selections.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("selections", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_selections(output_dir: Path, storyboard: Path | None, selections: dict) -> None:
    payload = {
        "output_dir": str(output_dir),
        "storyboard": str(storyboard) if storyboard else None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "selections": selections,
    }
    p = output_dir / "selections.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _make_handler(state):
    output_dir: Path = state["output_dir"]
    storyboard_path: Path | None = state["storyboard_path"]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 콘솔 조용히
            pass

        def _send_json(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path

            if path in ("/", "/index.html"):
                html = HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            if path == "/data":
                # 매 요청마다 다시 읽음 (외부에서 selections.json 수정해도 반영)
                cuts = discover_cuts(output_dir)
                self._send_json(200, {
                    "title": state["title"],
                    "output_dir": str(output_dir),
                    "cuts": cuts,
                    "storyboard_cuts": state["storyboard_cuts"],
                    "selections": load_selections(output_dir),
                })
                return

            if path.startswith("/img/"):
                # /img/<cut_id>/<filename>
                parts = path.split("/", 3)
                if len(parts) != 4:
                    self.send_error(400); return
                cut_id = urllib.parse.unquote(parts[2])
                fname = urllib.parse.unquote(parts[3])
                # path traversal 방어
                if "/" in cut_id or "/" in fname or ".." in cut_id or ".." in fname:
                    self.send_error(403); return
                img_path = (output_dir / cut_id / fname).resolve()
                try:
                    img_path.relative_to(output_dir.resolve())
                except ValueError:
                    self.send_error(403); return
                if not img_path.is_file() or img_path.suffix.lower() not in IMAGE_EXTS:
                    self.send_error(404); return
                ext = img_path.suffix.lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".webp": "image/webp"}[ext]
                data = img_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404)

        def do_POST(self):
            if self.path != "/save":
                self.send_error(404); return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(body)
                sels = payload.get("selections", {})
                if not isinstance(sels, dict):
                    raise ValueError("selections must be object")
                # 유효한 cut_id + 파일만 통과
                clean = {}
                for cut_id, fname in sels.items():
                    if not isinstance(cut_id, str) or not isinstance(fname, str):
                        continue
                    if "/" in cut_id or "/" in fname or ".." in cut_id or ".." in fname:
                        continue
                    img_path = output_dir / cut_id / fname
                    if img_path.is_file():
                        clean[cut_id] = fname
                save_selections(output_dir, storyboard_path, clean)
                self._send_json(200, {"ok": True, "saved": len(clean)})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})

    return Handler


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def main(argv=None):
    p = argparse.ArgumentParser(prog="img_gen.picker",
        description="컷별 후보 이미지 픽커 (로컬 브라우저).")
    p.add_argument("output_dir", type=Path,
        help="img_gen 이 생성한 컷 폴더들이 있는 디렉토리 (예: out_test)")
    p.add_argument("--storyboard", type=Path, default=None,
        help="스토리보드 .md 파일 (있으면 나레이션·시간 사이드바에 표시)")
    p.add_argument("--port", type=int, default=8765,
        help="시작 포트 (점유 시 +20 까지 자동 fallback)")
    p.add_argument("--no-browser", action="store_true",
        help="브라우저 자동 열기 비활성")
    args = p.parse_args(argv)

    output_dir = args.output_dir.expanduser().resolve()
    if not output_dir.is_dir():
        raise SystemExit(f"[picker] output_dir 없음: {output_dir}")

    cuts = discover_cuts(output_dir)
    if not cuts:
        raise SystemExit(
            f"[picker] cut 폴더가 없음: {output_dir}\n"
            f"        하위 폴더에 gpt_*.png / mj_*.png 가 있어야 함."
        )

    storyboard_cuts = []
    title = output_dir.name
    if args.storyboard:
        sb_path = args.storyboard.expanduser().resolve()
        if not sb_path.is_file():
            raise SystemExit(f"[picker] storyboard 파일 없음: {sb_path}")
        sb = parse_storyboard(sb_path)
        title = sb.title
        storyboard_cuts = [{
            "index": c.index, "time": c.time, "scene": c.scene,
            "subcut": c.subcut, "narration": c.narration, "bgm": c.bgm,
            "prompt_kr": c.prompt_kr, "prompt_en": c.prompt_en,
        } for c in sb.cuts]

    state = {
        "output_dir": output_dir,
        "title": title,
        "cuts": cuts,
        "storyboard_cuts": storyboard_cuts,
        "storyboard_path": args.storyboard,
    }

    handler = _make_handler(state)

    # 포트 자동 fallback
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
        raise SystemExit(f"[picker] 포트 {args.port}-{args.port+19} 모두 점유 중")

    url = f"http://127.0.0.1:{chosen_port}/"
    print(f"[picker] {title}")
    print(f"[picker] {len(cuts)} cuts · {sum(len(c['images']) for c in cuts)} images · {output_dir}")
    print(f"[picker] 열기: {url}  (Ctrl+C 로 종료)")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[picker] 종료. → {output_dir / 'selections.json'}")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
