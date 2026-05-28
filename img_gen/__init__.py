"""img_gen — 프롬프트 → 이미지 (gpt-image-2 default).

기본 사용:

    from img_gen import generate
    paths = generate("무당파 도사 안개낀 산봉우리에서 태극권", cut_id="cut_01_a", n=3)

배치:

    from img_gen import generate_batch
    results = generate_batch([
        {"cut_id": "cut_01_a", "prompt": "..."},
        {"cut_id": "cut_01_b", "prompt": "..."},
    ], n=3)

Midjourney 는 이 모듈 범위 밖. 같은 cut 폴더에 `mj_001.png` 형식으로 수동 드롭하면 됨.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, TypedDict

from .config import Config
from .providers import GeneratedImage, generate_openai

__all__ = ["generate", "generate_batch", "GeneratedImage", "Config", "BatchItem"]


class BatchItem(TypedDict, total=False):
    cut_id: str
    prompt: str
    n: int
    size: str
    quality: str


def generate(
    prompt: str,
    cut_id: str,
    n: int | None = None,
    size: str | None = None,
    quality: str | None = None,
) -> list[Path]:
    """한 프롬프트에 대해 n장 생성. 파일 경로 리스트 반환."""
    images = generate_openai(
        prompt=prompt,
        cut_id=cut_id,
        n=n,
        size=size,
        quality=quality,
    )
    return [img.path for img in images]


def generate_batch(items: Iterable[BatchItem], n: int | None = None) -> dict[str, list[Path]]:
    """여러 cut 일괄 생성. {cut_id: [paths]} 반환.

    item 단위 옵션 (n/size/quality) 이 batch 단위 default 보다 우선.
    """
    cfg = Config.load()
    out: dict[str, list[Path]] = {}
    for item in items:
        cut_id = item["cut_id"]
        prompt = item["prompt"]
        item_n = item.get("n", n)
        images = generate_openai(
            prompt=prompt,
            cut_id=cut_id,
            n=item_n,
            size=item.get("size"),
            quality=item.get("quality"),
            config=cfg,
        )
        out[cut_id] = [img.path for img in images]
    return out
