"""OpenAI gpt-image-2 호출. Midjourney 는 이 모듈 범위 밖 (수동 드롭)."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI, BadRequestError, NotFoundError

from .config import Config


@dataclass
class GeneratedImage:
    path: Path
    revised_prompt: str | None  # 모델이 prompt 를 rewrite 한 경우 (gpt-image-1 동작)


def _decode_and_save(b64_data: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(b64_data))


def _try_models(client: OpenAI, model_candidates: list[str], **kwargs):
    """첫 모델 호출, 모델 not-found 시 다음 후보로 fallback."""
    last_err: Exception | None = None
    for model in model_candidates:
        try:
            return client.images.generate(model=model, **kwargs), model
        except NotFoundError as e:
            last_err = e
            continue
        except BadRequestError as e:
            # "model not found" 가 BadRequest 로 떨어지는 경우도 있음
            msg = str(e).lower()
            if "model" in msg and ("not found" in msg or "does not exist" in msg or "unknown" in msg):
                last_err = e
                continue
            raise
    raise RuntimeError(
        f"모든 모델 후보 실패: {model_candidates}. 마지막 에러: {last_err}"
    )


def generate_openai(
    prompt: str,
    cut_id: str,
    n: int | None = None,
    size: str | None = None,
    quality: str | None = None,
    config: Config | None = None,
    model: str | None = None,
) -> list[GeneratedImage]:
    """OpenAI gpt-image-2 로 한 prompt 에 대해 n장 생성.

    파일 이름 규칙: `output/<cut_id>/gpt_001.png`, `gpt_002.png`, ...
    같은 cut_id 폴더에 이미 파일이 있으면 다음 번호부터 이어서 매김.
    """
    cfg = config or Config.load()
    n = n or cfg.default_n
    size = size or cfg.default_size
    quality = quality or cfg.default_quality

    selected_model = model or cfg.model
    client = OpenAI(api_key=cfg.api_key)

    cut_dir = cfg.output_dir / cut_id
    cut_dir.mkdir(parents=True, exist_ok=True)

    # prompt.txt 적어두기 (재생성·MJ 수동 호출 시 참조용)
    prompt_path = cut_dir / "prompt.txt"
    if not prompt_path.exists():
        prompt_path.write_text(prompt, encoding="utf-8")

    existing = sorted(cut_dir.glob("gpt_*.png"))
    next_idx = (
        max((int(p.stem.split("_")[1]) for p in existing), default=0) + 1
    )

    # gpt-image-2 가 한 번 호출에 n>1 을 지원하는지 불확실 → 1장씩 loop 가 안전
    # (지원하면 나중에 batched 로 바꿀 수 있음)
    model_candidates = [selected_model]
    if selected_model != "gpt-image-1":
        model_candidates.append("gpt-image-1")  # fallback

    results: list[GeneratedImage] = []
    actual_model: str | None = None

    for i in range(n):
        kwargs = {
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "n": 1,
        }
        response, used_model = _try_models(client, model_candidates, **kwargs)
        actual_model = used_model
        # 다음 호출은 같은 모델로 바로
        model_candidates = [used_model]

        for item in response.data:
            idx = next_idx + len(results)
            fname = f"gpt_{idx:03d}.png"
            path = cut_dir / fname
            if getattr(item, "b64_json", None):
                _decode_and_save(item.b64_json, path)
            elif getattr(item, "url", None):
                # URL 응답이면 다운로드
                import urllib.request
                urllib.request.urlretrieve(item.url, path)
            else:
                raise RuntimeError(f"응답에 b64_json 도 url 도 없음: {item}")

            results.append(
                GeneratedImage(
                    path=path,
                    revised_prompt=getattr(item, "revised_prompt", None),
                )
            )

    # 어떤 모델로 실제 생성됐는지 cut 폴더에 메모 (debugging 용)
    if actual_model:
        meta_path = cut_dir / "_meta.txt"
        with meta_path.open("a", encoding="utf-8") as f:
            f.write(f"model={actual_model} size={size} quality={quality} n={n}\n")

    return results
