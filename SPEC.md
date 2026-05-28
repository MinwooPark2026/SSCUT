# SPEC — 비디오 컷 픽커 + 자막 싱크 + 모션 에디터

> 통합 로컬 웹툴. 외주 재구현 기준 스펙시트.
> v1.0 · 2026-05-19

---

## 1. Goal

한국어 정보형 유튜브 영상 (5~30분) 을 한 사람이 만들 때, **AI 생성 이미지·외부 영상 클립·TTS 나레이션·자막** 을 **컷 단위** 로 조합하여 최종 mp4 한 편을 빠르게 산출하기 위한 로컬 웹툴.

핵심 가치 (이걸로 차별화):
- 컷별 **후보 N장 중 픽** (상용은 보통 1장 자동)
- **이미지·외부 영상 동일 객체** 로 다룸 (Remotion / HyperFrame mp4 등 자유 삽입)
- **자막 timing·텍스트 1프레임 단위 조정**
- **초점·모션 픽셀 단위 컨트롤**
- 로컬·파일 기반 (vendor lock-in X, 토큰비만)

## 2. User Profile

- 단일 사용자 (개인 크리에이터)
- macOS (Apple Silicon 우선)
- 한국어 콘텐츠 중심
- 기존 워크플로우: 대본 → 스토리보드(.md) → 이미지 → 영상 편집
- 기술 친화 (CLI, .json 직접 수정 OK)
- 캡컷·Premiere 등 NLE 와 병행 (이 도구는 자동화 영역)

## 3. End-to-end Workflow

```
[외부] 대본 → 스토리보드 .md 작성
   ↓
[외부] 이미지 생성 도구 (gpt-image-1 등) → 컷별 N장
[외부 옵션] Remotion/HyperFrame → 컷별 mp4 (선택 후보로 드롭)
   ↓
[본 시스템] 단일 명령어 → 브라우저 자동 오픈
   ├─ /picker  : 컷별 후보 픽
   ├─ /sync    : 자막 시각·텍스트 조정
   └─ /motion  : 이미지 컷의 Ken Burns 류 모션 + 초점
   ↓
[본 시스템] "Final 렌더링" → final.mp4
   ↓
[외부 옵션] 캡컷 등에서 BGM/트랜지션/마무리
```

## 4. High-level Architecture

```
┌─ macOS ─────────────────────────────────────────────────┐
│  Browser (Chrome/Safari)                                │
│    Tabs ↔ /picker  /sync  /motion                       │
│       │  http://127.0.0.1:8765                          │
│  ┌────▼─────────────────────────────────────────────┐   │
│  │ Single Local HTTP Server (Python stdlib)         │   │
│  │  • Static HTML serving                           │   │
│  │  • JSON API (read/save)                          │   │
│  │  • Range request (video streaming)               │   │
│  │  • Render pipeline (ffmpeg orchestration)        │   │
│  └────┬─────────────────────────────────┬───────────┘   │
│       │ subprocess                       │ HTTPS         │
│  ┌────▼──────────────┐         ┌─────────▼──────────┐    │
│  │ ffmpeg (vendored, │         │ OpenAI API         │    │
│  │ venv 안 격리)     │         │ (image, tts)       │    │
│  └───────────────────┘         └────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

- **단일 프로세스, 단일 포트, 단일 PID** (이전엔 3 서버였다가 통합 — 디버깅·시작·종료 일관성)
- **로컬 only** (127.0.0.1, no auth)
- **파일 기반 state** (DB 없음, 폴더 복사로 마이그레이션)

## 5. Filesystem Layout (Single Source of Truth)

```
<project_root>/                     ← 시스템 코드
├── editor.py                       # 통합 HTTP 서버
├── video.py                        # ffmpeg 호출 wrapper
├── storyboard.py                   # .storyboard.md 파서
├── providers.py                    # OpenAI 이미지 호출
├── config.py                       # .env 로딩
├── picker.html, sync.html, motion.html
├── .env                            # OPENAI_API_KEY (Dropbox 외부)
├── .venv/                          # Python + ffmpeg 격리
└── run.sh                          # venv·env·실행 한 줄

<output_dir>/                       ← 한 영상 프로젝트당 하나
├── <cut_id_1>/                     # 예: 01_ragdoll
│   ├── prompt.txt                 # 이미지 프롬프트 (텍스트)
│   ├── _meta.txt                  # 생성 메타 (model, size 등)
│   ├── gpt_001.png, gpt_002.png   # 이미지 후보
│   └── <any>.mp4                  # 외부 영상 후보 (옵션, 사용자 직접 드롭)
├── <cut_id_2>/ ...
├── <cut_id_N>/ ...
├── selections.json                 # cut_id → 선택된 파일명
├── subtitle_timings.json           # cut_id → {start, text, motion: {...}}
├── .selections_backup/             # 자동 백업 (최근 10 버전, timestamp)
│   └── selections_YYYYMMDD_HHMMSS.json
└── video/                          # 렌더 산출물 (RESERVED — cut 아님)
    ├── <cut_id>_tts.mp3            # TTS 캐시 (재사용)
    ├── <cut_id>_base.mp4           # 모션+TTS, 자막 없음 (sync preview)
    ├── <cut_id>.mp4                # 최종 컷 (자막 burn)
    └── final.mp4                   # 전체 concat
```

## 6. Domain Concepts

| Term | Definition |
|---|---|
| **Cut** | 영상의 한 구간 (3~수십초). 한 폴더 = 한 cut. |
| **Cut ID** | 폴더 이름. 알파벳 정렬이 영상 순서. 컨벤션 `01_`, `02_` prefix |
| **Media** | 컷 폴더 안의 이미지 또는 영상 후보 파일 |
| **Candidate** | 한 컷의 후보 미디어 (보통 N=3장) |
| **Selection** | 사용자가 픽한 미디어의 파일명 |
| **Selected Type** | image / video — 확장자로 결정 |
| **Subtitle** | 컷에 burn-in 되는 자막. 시각·텍스트 사용자 조정 |
| **Motion** | 이미지 컷의 Ken Burns 류 효과 (type/focal/effect_amount) |
| **External Video Cut** | 선택된 미디어가 영상인 컷. 모션 적용 안 됨, TTS 안 입힘 |

## 7. Storyboard 입력 계약

마크다운 파일. 첫 번째 표가 cuts 의 source. 표 외 텍스트는 무시.

```markdown
| #호흡 | 시간          | 메인 장면 | 시각 컷 분할                          | 대사·내레이션      | BGM·SFX | 클립 프롬프트 (한국어) | 클립 프롬프트 (English) |
|------|---------------|-----------|---------------------------------------|--------------------|---------|------------------------|--------------------------|
| 1    | 0:00~0:03.3   | ...       | 0~2.3s 메인: ...<br>2.3~3.3s 자막: "..."| 래그돌은 ...      | ...     | ...                    | ...                      |
| 2    | 0:03.3~0:06.6 | ...       | ...                                   | ...                | ...     | ...                    | ...                      |
```

- 컷 번호 (1, 2, ...) ↔ output_dir 의 알파벳 정렬된 컷 폴더 1:1 매칭
- "시각 컷 분할" 안 `자막: "..."` 패턴 → default 자막 텍스트·시작 시각
- "대사·내레이션" 컬럼 → TTS 입력 텍스트

## 8. Components

### 8.1 Picker (`/picker`)
- **목적**: 컷별 후보 미디어 중 하나 픽
- **레이아웃**: 사이드바 (컷 목록 + 스토리보드 미니뷰) + 메인 (후보 grid)
- **카드**:
  - 이미지 후보 → `<img>` 썸네일
  - 영상 후보 → `<video preload="metadata">` 첫 프레임 + 우상단 `▶ VIDEO` 초록 뱃지
- **상호작용**:
  - 카드 클릭 = 선택 토글
  - 사이드바 / 스토리보드 미니뷰 클릭 = 컷 이동
  - 키보드: ↑↓/WS 컷 이동, ←→/AD 후보 cursor, Space/1~9 선택, Esc 해제
- **자동 저장**: 클릭 즉시 POST /save → selections.json
- **알림 (실수 인지)**:
  - ✓ 선택 → ok 토스트 1.5초 자동 사라짐
  - ⚠ 해제 → warn 토스트 **sticky** (클릭 닫음)
  - 저장 실패 → err 토스트 sticky
- **모든 이미지 prefetch**: 페이지 로드시 `new Image()` 백그라운드 → 컷 전환 즉시
- **DOM 재생성 X**: cursor/선택 토글 시 클래스만 갱신 → 깜빡임 0

### 8.2 Sync (`/sync`)
- **목적**: 컷별 자막 시각·텍스트 조정
- **레이아웃**: 사이드바 + 메인 (비디오 플레이어 + 자막 오버레이 + 컨트롤)
- **비디오 소스**:
  - 이미지 컷 → `<cut_id>_base.mp4` (lazy 생성)
  - 영상 컷 → 외부 mp4 직접 서빙
- **자막 오버레이**: 비디오 위 절대 위치 `<div>` — 실제 burn 결과 스타일과 일치
- **컨트롤**: 자막 시각 (숫자 + "현재 시각으로"), 자막 텍스트, 컷 이동
- **타임라인 바**: 회색 + 진행 (파랑) + **자막 시작 마커 (노랑)**
- **키보드**: Space 재생/정지, ←→ ±0.5s scrub, ↑↓ 컷 이동, S = 현재 시각을 자막 시작으로
- **자동 저장**: 0.5초 debounce → POST /save_timings (motion 필드 보존 merge)
- **빈 컷 안내**: 일부 미선택 → 상단 노란 배너 `미선택: <ids> → 픽커` + 외부 영상 컷 별도 표시

### 8.3 Motion (`/motion`)
- **목적**: 이미지 컷의 모션 + 초점 설정
- **레이아웃**: 사이드바 + 메인 (이미지 미리보기 + 컨트롤)
- **미리보기**:
  - 소스 이미지 + 초점 마커 (노란 십자) + 진행 progress bar (초록, 하단 3px)
  - **CSS rAF 애니메이션** — `transform: scale` + `transform-origin` 으로 매 프레임 직접 설정
  - 모드 토글 (우상단 오버레이 버튼, `stopPropagation` 적용):
    - 🔁 반복 (default, ease-in-out alternate infinite — 편집 친화)
    - ▶ 1회 (linear 1회, 끝에 정지 — 실제 영상 매칭)
- **모션 타입** (6):
  1. `zoom_in` — 초점으로 줌인
  2. `zoom_out` — 초점에서 줌아웃
  3. `pan_lr` — 왼→오 슬라이드
  4. `pan_rl` — 오→왼 슬라이드
  5. `shake` — 좌우 흔들기
  6. `static` — 정지
- **효과 강도 슬라이더** (0~0.30): 타입별 의미 (단위 자동 변환)
  - zoom: 변화량 (%)
  - pan: 속도 (x — cycles per duration)
  - shake: 진동수 (Hz)
- **초점**: 미리보기 이미지 클릭 → focal_x, focal_y (정규화 0~1)
- **외부 영상 컷**: 사이드바에서 제외 + 배너에 안내
- **자동 저장**: POST /save_motion (subtitle 필드 보존 merge)
- **키보드**: 1~6 모션 타입, ↑↓/WS 컷 이동, C 초점 중앙으로

## 9. Render Pipeline

POST /render — 동기 실행, 5~30초. modal 로 로그 + 결과 표시.

```
for each cut_id in sorted(selections.keys()):
    selected_file = selections[cut_id]
    if selected_file.ext in (.mp4, .mov, .webm, .mkv):
        # 외부 영상 흐름
        _normalize_video(source) → cut_id_base.mp4   # 1920x1080 30fps h264 AAC
        _burn_subtitle(base, sub_text, sub_start) → cut_id.mp4
    else:
        # 이미지 흐름
        ensure TTS exists (gpt-4o-mini-tts → fallback tts-1)
        duration = max(3.0, tts_duration + 0.3)
        make_motion_clip(image, duration, motion_type, focal_x, focal_y, effect_amount)
        mux motion + tts (no subtitle) → cut_id_base.mp4
        _burn_subtitle(base, sub_text, sub_start) → cut_id.mp4
    
    append cut_id.mp4 to cut_paths

concat_demuxer(cut_paths) → final.mp4
```

### 9.1 정규화 (외부 영상 → 표준 포맷)

```
ffmpeg -i <input> \
  -vf "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30" \
  -c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p -movflags +faststart \
  -c:a aac -b:a 192k -ar 44100 -ac 2 \
  <cut_id>_base.mp4
```

이유: concat demuxer 안전성. 입력의 코덱/fps/오디오 다를 수 있음.

### 9.2 모션 클립 생성 (ffmpeg zoompan)

입력: 임의 해상도. cover-fit 1920×1080 후 zoompan 적용.

| Type | z (scale) | x (top-left X in scaled image) | y |
|---|---|---|---|
| zoom_in | `1.0 + a·on/(N-1)` | `clip(ow·(fx·z-0.5), 0, iw·z-ow)` | `clip(oh·(fy·z-0.5), 0, ih·z-oh)` |
| zoom_out | `(1+a) - a·on/(N-1)` | (위 동일) | (위 동일) |
| pan_lr | `1.10` 고정 | `(iw·z-ow) · min(speed·on/(N-1), 1)` | y_focal |
| pan_rl | `1.10` 고정 | `(iw·z-ow) · (1 - min(speed·on/(N-1), 1))` | y_focal |
| shake | `1.05` 고정 | `clip(ow·(fx·z-0.5) + (iw·z-ow)/2·sin(2π·freq·on/fps), 0, iw·z-ow)` | y_focal |
| static | `1.0` | `0` | `0` |

- `a` = effect_amount (zoom 용)
- `speed = max(0.1, effect_amount · 10)` (pan)
- `freq = max(0.5, effect_amount · 20)` (shake)
- `N` = round(duration · fps), fps=30
- `fx, fy` ∈ [0, 1]

CSS 미리보기는 동일 수학을 `transform-origin` + `scale` 로 rAF 매 프레임 → 시각적 매칭.

### 9.3 자막 burn (ffmpeg drawtext)

```
drawtext=fontfile=<korean_font>:textfile=<sub.txt>:
  fontsize=54:fontcolor=white:
  borderw=4:bordercolor=black@0.85:
  box=1:boxcolor=black@0.35:boxborderw=18:
  x=(w-text_w)/2:y=h-180:enable='gte(t,<start>)'
```

폰트 우선: `/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc`. fallback chain.

### 9.4 TTS

- **모델**: `gpt-4o-mini-tts` (1순위) → `tts-1` (fallback)
- **voice**: `nova` default
- **포맷**: mp3
- **캐시**: `<cut_id>_tts.mp3` 재사용 (같은 나레이션은 1회만 호출)
- **컷 길이 적응**: TTS 가 컷보다 길면 컷 자동 연장 (말 잘리지 않게)

## 10. REST API

| Method | Path | Body / Behavior |
|---|---|---|
| GET | `/` | 302 → `/picker` |
| GET | `/picker` | picker.html |
| GET | `/sync` | sync.html |
| GET | `/motion` | motion.html |
| GET | `/data` | 통합 JSON (아래 schema) |
| GET | `/img/<cut_id>/<filename>` | 후보 미디어. 이미지/영상 모두. 영상은 Range 지원 |
| GET | `/image/<cut_id>` | 선택된 소스 이미지 (이미지 컷만) |
| GET | `/video/<cut_id>` | base.mp4 (sync preview). Range 지원 |
| POST | `/save` | `{selections: {cut_id: filename}}` → selections.json (+백업 rotate) |
| POST | `/save_timings` | `{timings: {cut_id: {start, text}}}` → motion 보존 merge |
| POST | `/save_motion` | `{cut_id, motion: {type, focal_x, focal_y, zoom_amount}}` → subtitle 보존 merge |
| POST | `/render` | final.mp4 재렌더 + `{ok, final_path, duration, size_mb, log}` |

### 10.1 `/data` Response Schema

```jsonc
{
  "title": "스토리보드 제목",
  "output_dir": "<abs path>",
  "selections": { "<cut_id>": "<filename>", ... },
  "storyboard_cuts": [
    { "index": 1, "time": "0:00~0:03.3", "scene": "...",
      "subcut": "...", "narration": "...", "bgm": "...",
      "prompt_kr": "...", "prompt_en": "..." }
  ],
  "cuts": [
    {
      "cut_id": "01_ragdoll",
      "prompt": "...",
      "media": [
        {"name": "gpt_001.png", "type": "image"},
        {"name": "intro.mp4",   "type": "video"}
      ],
      "selected": "gpt_002.png",       // 또는 null
      "selected_type": "image",        // 또는 "video" / null
      "is_external_video": false,

      // 아래 필드들은 selected 있을 때만
      "video_url": "/video/01_ragdoll",   // 영상 컷이면 "/img/01_ragdoll/intro.mp4"
      "image_url": "/image/01_ragdoll",   // 영상 컷이면 null
      "duration": 4.17,
      "subtitle_start": 2.3,
      "subtitle_text": "...",
      "narration": "TTS 원문",
      "motion": { "type":"zoom_in", "focal_x":0.5, "focal_y":0.5, "zoom_amount":0.10 }
        // 영상 컷이면 null
    }
  ]
}
```

### 10.2 selections.json schema

```jsonc
{
  "output_dir": "<abs path>",
  "storyboard": "<abs path>",
  "updated_at": "ISO-8601",
  "selections": { "<cut_id>": "<filename>" }
}
```

### 10.3 subtitle_timings.json schema

```jsonc
{
  "updated_at": "ISO-8601",
  "timings": {
    "<cut_id>": {
      "start": 2.3,
      "text": "자막 텍스트",
      "motion": {
        "type": "zoom_in" | "zoom_out" | "pan_lr" | "pan_rl" | "shake" | "static",
        "focal_x": 0.5,
        "focal_y": 0.5,
        "zoom_amount": 0.10
      }
    }
  }
}
```

## 11. External Dependencies

| Dep | Version | Purpose | 격리 |
|---|---|---|---|
| Python | 3.11+ | Server runtime | venv |
| openai | 2.x+ | gpt-image-1, gpt-4o-mini-tts | venv |
| python-dotenv | 1.x | .env 로딩 | venv |
| imageio-ffmpeg | 0.5+ | ffmpeg 7.x 바이너리 vendoring | venv (시스템 설치 X) |
| AppleSDGothicNeo.ttc | macOS 기본 | 한국어 자막 폰트 | 시스템 |

## 12. Non-functional Requirements (NFR)

| 항목 | 요구 |
|---|---|
| **자동 저장** | 모든 변경 즉시 또는 0.5초 debounce 후 디스크 (Save 버튼 없음) |
| **데이터 손실 방지** | selections.json 저장 시 직전 버전 `.selections_backup/` 에 timestamp 자동 백업 (최근 10개 rotate) |
| **에러 가시성** | 잡히지 않은 JS error / promise rejection → sticky 빨간 토스트. 클릭으로 닫음 |
| **액션 피드백** | 선택/해제 시 다른 색·sticky 정책. 해제는 ⚠ warn sticky (실수 인지) |
| **미선택 가시화** | sync/motion 진입 시 선택 안 된 컷 ID 배너 + 픽커 직링크 |
| **DOM 깜빡임 0** | 같은 컷 내 cursor·선택 토글 시 DOM 재생성 X |
| **이미지 prefetch** | 페이지 로드시 모든 이미지 background fetch |
| **포트 fallback** | default 8765 점유 시 +20 까지 자동 fallback |
| **graceful 종료** | Ctrl+C 시 진행 중 저장 완료 후 종료 |
| **외부 도구 격리** | ffmpeg venv 내. 시스템 brew 설치 의존 X |
| **단일 사용자 가정** | auth 없음, localhost 바인딩만 |

## 13. Keyboard UX Standard

탭 간 일관된 키 매핑.

| Key | Picker | Sync | Motion |
|---|---|---|---|
| ↑ ↓ / W S | 컷 이동 | 컷 이동 | 컷 이동 |
| ← → / A D | 후보 cursor | 0.5s scrub | (없음) |
| Space | 현재 cursor 토글 | 재생/정지 | (없음) |
| 1~9 | N번째 후보 선택 | (없음) | 1~6 모션 타입 |
| Esc | 현재 컷 해제 | (없음) | (없음) |
| S | (없음) | 현재 시각 → 자막 시작 | (없음) |
| C | (없음) | (없음) | 초점 중앙으로 |
| ? hover | cheat sheet popover (헤더 우상단) | 동일 | 동일 |

## 14. Visual / Theme

- **다크**: bg #1a1a1a, sidebar #0d0d0d, card bg #222, border #3a3a3a
- **액센트**:
  - 파랑 #1e3a5f → active 컷, 자막 마커 진행
  - 초록 #4ade80 → 선택 완료, progress bar
  - 노랑 #fbbf24 → cursor outline, 자막 시작 마커
  - 호박 #78350f / #fde68a → 미선택 배너, warn 토스트
  - 빨강 #7f1d1d → 에러 토스트
- **폰트**: -apple-system, BlinkMacSystemFont
- **일관 탭 nav**: 우상단 `🖼 픽커 · 🎬 싱크 · 🎯 모션` (active 강조, 동일 origin 상대 URL)

## 15. Out of Scope (V1 NOT 빌드)

의도적으로 포함 안 함 — 외주 시 추가 요구 차단:

- BGM / 사운드 트랙 (NLE 에서 추가)
- 트랜지션 (cut↔cut 사이 — concat 만, fade 없음)
- 다중 사용자 / auth
- 클라우드 저장 / 동기화
- 모바일 UI
- 영상 트리밍 / split / 4K
- 컬러 그레이딩 / 필터
- 자동 BGM 매칭
- 멀티 트랙 (오디오 mix)
- 영상 컷에 TTS 오버레이 (영상 자기 오디오만 사용)
- 영상 컷에 모션 적용 (이미 모션 있음 가정)
- 자동 컷 분할 / 자동 자막 생성
- 영상 컷에 자체 자막 트랙 추출 (자막은 이 도구에서 새로 입힘)

## 16. Acceptance Criteria

V1 완성 기준 (외주 verification):

1. **이미지-only smoke**: 3 컷 (이미지만) 영상 → 자막+TTS+모션 burn 된 final.mp4. 10~15초, ~5MB.
2. **외부 영상 통합**: 한 컷이 mp4 인 영상 → 정규화+자막 burn. 영상 자체 오디오 보존.
3. **자동 저장 영속**: 픽커 클릭 → 브라우저 새로고침 → 선택 유지.
4. **데이터 복구**: 실수 해제 → `.selections_backup/` 에서 직전 파일 복사 → 새로고침 → 복원.
5. **자막 싱크 정확도**: t=1.0s 에 자막 시작 설정 → Final → 1.0s 부터 자막 노출 (±1 프레임).
6. **모션 미리보기 정확도**: 미리보기 1회 모드의 끝 프레임 ≈ Final mp4 의 끝 프레임 (시각적 유사).
7. **키보드 전용 작업**: 마우스 안 만지고 모든 컷 픽·자막 조정·모션 변경 → 정상.
8. **3 탭 일관**: 픽커에서 컷 선택 → 싱크·모션이 즉시 반영 (서버 재시작 불필요).
9. **에러 가시성**: API 강제 실패 (네트워크 차단 등) → sticky 에러 토스트, 클릭으로 닫음.
10. **백업 rotate**: save 11회 → `.selections_backup/` 안에 정확히 10 파일 (오래된 거 자동 삭제).

## 17. Known Trade-offs

| 결정 | 이유 | 향후 재고 조건 |
|---|---|---|
| ffmpeg 정규화 매 외부 영상마다 | concat demuxer 안전성, 코덱 불일치 회피 | 동일 코덱·해상도 입력 보장되면 demuxer 직결 가능 |
| CSS rAF 미리보기 (픽셀 정확 X) | ffmpeg 매 변경 호출은 너무 무거움 | 정확 미리보기 필요 시 quick low-res render 별도 |
| 폴더 sort 순서 = 영상 순서 | 컨벤션 (`00_`, `01_`) | 명시 ordering 필요 시 storyboard 의 순서를 권위로 |
| selections.json 전체 replace | 클라이언트가 일관 상태 보장 | Esc·toggle 실수는 backup rotation 으로 보완 |
| 단일 사용자 가정 | auth 없음 | 팀 사용 시 인증/권한 |
| 한국어 폰트 hardcoded path | macOS 가정 | 다른 OS 지원 시 폰트 체인 확장 |
| TTS 모델 fallback chain | 신모델 출시 / 구모델 retire 모두 대응 | 모델 변경 잦으면 .env 변수화 (현재도 가능) |
| 이미지 모델 ID env 오버라이드 | 모델 변경 시 코드 수정 X | gpt-image-2 등 출시 시 `OPENAI_IMAGE_MODEL` 한 줄 |
| `video/` 폴더 RESERVED 이름 | 렌더 산출물 cut 으로 잘못 잡힘 방지 | 폴더 명명 컨벤션 명시 (cut 폴더에 `video` 금지) |

## 18. Build Roadmap (외주 추정)

풀타임 한 명 기준.

| Phase | 산출 | 일수 |
|---|---|---|
| 1 | 데이터 모델 + 파일 디스커버리 + storyboard 파서 (discover_cuts, parse_storyboard, selections load/save) | 1~2 |
| 2 | 통합 HTTP 서버 + 라우트 + 정적 HTML 서빙, Range support | 1~2 |
| 3 | 픽커 UI (이미지 only) + 자동 저장 + 키보드 + 깜빡임 0 | 2~3 |
| 4 | ffmpeg 모션 + TTS 통합 (make_motion_clip, mux_cut) | 2~3 |
| 5 | 싱크 UI (HTML5 video + 자막 오버레이 + 타임라인) | 2~3 |
| 6 | 모션 UI (CSS rAF + 6 타입 + 효과 강도 단위 변환) | 2~3 |
| 7 | 렌더 파이프라인 (concat + 자막 burn) | 1~2 |
| 8 | 외부 영상 컷 통합 (media 객체 통합 + 분기) | 1~2 |
| 9 | 백업/에러 UX/단축키 cheat sheet/미선택 배너 | 1~2 |
| 10 | QA + 사용자 피드백 반영 | 2~3 |
| **합계** | | **15~25일** |

## 19. Test Fixture (외주 자동 검증)

리포에 동봉:

```
fixture/
├── short.storyboard.md            # 3컷 스토리보드 (10초 영상용)
├── short_cuts/
│   ├── 01_a/
│   │   ├── gpt_001.png .. gpt_003.png   # 사전 생성 이미지
│   │   └── prompt.txt
│   ├── 02_b/ ...
│   └── 03_c/ ...
└── sample_external.mp4            # 외부 영상 통합 검증용 (5초)
```

자동 테스트 시나리오: `fixture/short_cuts/` 를 output_dir 로 시작 → headless browser 또는 직접 API 호출로 Acceptance Criteria 10항 자동 검증.

## 20. Maintenance / Operations

| 상황 | 처리 |
|---|---|
| selections.json 깨졌을 때 | `.selections_backup/` 에서 timestamp 보고 복원 |
| 이미지 모델 변경 (gpt-image-2 출시 등) | `.env` 의 `OPENAI_IMAGE_MODEL` 수정 |
| TTS voice 변경 | `OPENAI_TTS_VOICE` 환경변수 또는 코드 default |
| 폰트 경로 변경 | `video.py` `KOREAN_FONT_CANDIDATES` |
| 포트 변경 | 실행 시 `--port` 옵션 |
| 디스크 차지 | `<output_dir>/video/` 통째 삭제 후 재렌더 (TTS 캐시는 보존하려면 `_tts.mp3` 제외) |

## 21. Security / Privacy

- **로컬 only**: 127.0.0.1 바인딩. 외부 노출 X.
- **OpenAI API key**: `.env` 파일에서 로딩. Dropbox/git 외부 권장.
- **이미지 데이터**: OpenAI 정책에 따라 모델 학습 사용 가능 (필요 시 API key 의 organization 설정에서 opt-out)
- **TTS 텍스트**: OpenAI 로 전송됨. 민감 정보 입력 금지.
- **로컬 파일 접근**: 서버는 `<output_dir>` 트리만 접근. Path traversal 방어 (`..` reject).

---

## 21+. Glossary (외주 새 개발자 onboarding)

| 약어 / 용어 | 의미 |
|---|---|
| Ken Burns | 정적 이미지에 줌·팬으로 모션 부여하는 다큐멘터리 기법 |
| rAF | requestAnimationFrame — 브라우저 60fps 콜백 |
| zoompan | ffmpeg 필터. 입력 이미지를 scale 후 crop window 이동 |
| burn-in | 자막을 픽셀로 영상에 굽기 (별도 트랙 X) |
| concat demuxer | ffmpeg concat 방식. 같은 코덱·해상도 필요 |
| TTS | Text-to-Speech |
| NLE | Non-Linear Editor (캡컷, Premiere 등) |
| storyboard | 영상의 컷별 계획 문서 |

---

**End of Spec.** 추가 질문은 maintainer 에게.
