"""smoke test — 1장 생성으로 환경 검증.

    cd ~/Sponge_test/SSCUT
    source .venv/bin/activate
    python scripts/smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 패키지를 직접 임포트할 수 있게 (pip install -e . 안 해도 됨)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from img_gen import generate
from img_gen.config import Config


def main() -> int:
    cfg = Config.load()
    print(f"[smoke] model={cfg.model} output_dir={cfg.output_dir}")
    paths = generate(
        prompt=(
            "A serene Korean mountain landscape at dawn, "
            "Sony FX3 documentary style, cool gray-blue tones, soft mist, "
            "wide cinematic 16:9 composition"
        ),
        cut_id="_smoke",
        n=1,
        size="1536x1024",
        quality="medium",
    )
    print(f"[smoke] OK — {paths[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
