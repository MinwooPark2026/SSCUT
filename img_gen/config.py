"""Config 로딩 — .env + 합리적 default."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PACKAGE_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class Config:
    api_key: str
    model: str
    image_models: list[str]
    output_dir: Path
    default_size: str
    default_quality: str
    default_n: int

    @classmethod
    def load(cls) -> "Config":
        if ENV_PATH.exists():
            load_dotenv(ENV_PATH, override=True)

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key or api_key in {"sk-...", "sk-", "YOUR_OPENAI_API_KEY"}:
            raise RuntimeError(
                "OPENAI_API_KEY 가 비어 있음. "
                f"{ENV_PATH} 를 만들고 키를 채우거나 환경변수로 export 하세요."
            )

        output_dir = Path(
            os.getenv("IMG_GEN_OUTPUT_DIR", str(PACKAGE_ROOT / "output"))
        ).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip()
        models_env = os.getenv("OPENAI_IMAGE_MODELS", "").strip()
        image_models = [m.strip() for m in models_env.split(",") if m.strip()]
        if not image_models:
            image_models = [model]
        elif model not in image_models:
            image_models.insert(0, model)

        return cls(
            api_key=api_key,
            model=model,
            image_models=image_models,
            output_dir=output_dir,
            default_size=os.getenv("IMG_GEN_SIZE", "1536x1024"),
            default_quality=os.getenv("IMG_GEN_QUALITY", "medium"),
            default_n=int(os.getenv("IMG_GEN_N", "3")),
        )
