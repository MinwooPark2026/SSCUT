#!/usr/bin/env bash
# img_gen launcher — venv + .env 한 번에. 첫 실행 시 venv·deps 자동 셋업.
#
# 사용:
#   ./run.sh scripts/smoke.py
#   ./run.sh -m img_gen.cli "프롬프트" --cut cut_01 --n 3
#
# 디자인:
#   - venv·.env·output 모두 이 스크립트 옆 (Sponge_test 안)
#   - Dropbox 외부, 다른 기기와 sync 안 됨

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1) venv 없으면 만들기
if [ ! -d ".venv" ]; then
    echo "[run.sh] .venv 없음 — python3.11 -m venv .venv"
    if ! command -v python3.11 >/dev/null 2>&1; then
        echo "[run.sh] python3.11 없음. 'brew install python@3.11' 후 다시 실행."
        exit 1
    fi
    python3.11 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install -U pip
    pip install -r requirements.txt
else
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# 2) .env export
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
else
    if [ -f ".env.example" ]; then
        echo "[run.sh] .env 없음 — .env.example 을 .env 로 복사합니다. 설정 탭에서 값을 채우세요."
        cp .env.example .env
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
    else
        echo "[run.sh] .env 및 .env.example 없음."
        exit 1
    fi
fi

# 3) 실행
exec python "$@"
