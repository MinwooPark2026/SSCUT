"""CLI — 빠른 단발 호출용.

    python -m img_gen.cli "무당파 도사 안개낀 산봉우리" --cut cut_01 --n 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import generate
from .config import Config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="img_gen",
        description="프롬프트 → 이미지 (gpt-image-2). 같은 cut_id 폴더에 누적.",
    )
    p.add_argument("prompt", nargs="?", help="이미지 프롬프트 (생략 시 --prompt-file)")
    p.add_argument("--cut", required=True, help="cut_id (output 폴더 내 하위 폴더명)")
    p.add_argument("--n", type=int, default=None, help="생성 장수 (default: 3)")
    p.add_argument("--size", default=None, help="예: 1536x1024 (default), 1024x1024, 1024x1536")
    p.add_argument("--quality", default=None, choices=["low", "medium", "high", "auto"], help="default: medium")
    p.add_argument("--prompt-file", type=Path, help="프롬프트를 파일에서 로드")

    args = p.parse_args(argv)

    if args.prompt:
        prompt = args.prompt
    elif args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    else:
        p.error("prompt 또는 --prompt-file 둘 중 하나 필수")

    cfg = Config.load()
    print(f"[img_gen] model={cfg.model} size={args.size or cfg.default_size} "
          f"quality={args.quality or cfg.default_quality} n={args.n or cfg.default_n}")
    print(f"[img_gen] cut_id={args.cut} → {cfg.output_dir / args.cut}")
    print(f"[img_gen] prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")

    paths = generate(
        prompt=prompt,
        cut_id=args.cut,
        n=args.n,
        size=args.size,
        quality=args.quality,
    )
    print(f"[img_gen] 생성 완료 ({len(paths)}장):")
    for path in paths:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
