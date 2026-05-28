# SSCUT

SSCUT은 콘티와 소스 이미지/영상이 이미 준비된 상태에서 쓰는 로컬 편집 앱이다.
외부 도구나 Claude Skill로 콘티를 만들고, 이미지/영상 생성 모델로 소스를 만든 뒤,
이 앱에서 후보 선택, 자막 싱크, 이미지 모션, 최종 렌더 확인을 한다.

편집 중 특정 컷의 이미지가 더 필요할 때만 앱 안에서 `+ 1장 생성`으로 같은 프롬프트를 다시 호출한다.
픽커의 커스텀 프롬프트 칸에 내용을 넣으면 그 1장만 별도 프롬프트로 생성한다.
BGM은 여러 컷을 가로지르므로 컷 콘티가 아니라 최종 렌더 이후의 후처리 단계에서 입힌다.

## 앱의 범위

SSCUT 편집 앱이 하는 일:

- 로컬 프로젝트 폴더 열기
- 콘티 원문 확인 및 수정
- 컷별 이미지/외부 영상 후보 비교
- 컷별 기준 프롬프트 확인 및 1회성 커스텀 프롬프트 이미지 생성
- 선택 결과 저장
- 자막 시작점과 문구 조정
- 이미지 컷의 모션/초점 조정
- 외부 영상 컷을 편집 흐름 안에서 미리보기
- 최종 렌더 생성
- 최종 렌더에 BGM 믹스
- 편집 중 필요한 이미지 1장 추가 생성
- Final Cut / CapCut용 내보내기 산출물 생성

SSCUT 편집 앱이 기본 목표로 삼지 않는 일:

- 긴 대본 작성
- 콘티 자동 생성 전체 파이프라인
- 대량 이미지/영상 생성 오케스트레이션
- 원격 SaaS 배포

현재 구현은 로컬 파일 시스템과 `.env`를 직접 쓰는 로컬 서버 앱이다. Vercel 같은 원격 배포로 옮기려면 업로드/오브젝트 스토리지, 작업 큐, 렌더 worker, 사용자별 secret 저장소를 분리해야 한다.

## 실행

처음 받았다면 `sample/sample-cat/` 이 완성된 SSCUT 프로젝트의 모습이다. 고양이 3컷 (래그돌·벵갈·샴) 의 콘티·픽 후보·자막·TTS 가 들어 있어, 앱을 켜고 `sample/sample-cat/` 을 열면 컷·픽커·싱크·모션 탭이 어떻게 채워지는지 바로 볼 수 있다.

macOS에서는 다운로드한 폴더 안의 `SSCUT.command`를 더블클릭한다.

동작:

1. 최근 SSCUT 프로젝트가 있으면 바로 열지 물어본다.
2. 없거나 새로 고르려면 Finder 폴더 선택창을 띄운다.
3. 선택한 프로젝트에 `SSCUT.json`이 있는지 확인한다.
4. 로컬 서버를 켜고 브라우저를 자동으로 연다.

첫 실행 때는 `.venv`와 `.env`를 만들고 필요한 패키지를 설치하므로 시간이 조금 걸릴 수 있다.

터미널에서 직접 실행할 수도 있다.

```bash
cd ~/Sponge_test/SSCUT
cp .env.example .env
# .env에서 OPENAI_API_KEY 등을 설정
./run.sh -m img_gen.editor /path/to/sscut-project --port 8765
```

브라우저:

```text
http://127.0.0.1:8765/files
```

탭:

- `파일`: 로컬 프로젝트 탐색 및 프로젝트 폴더 선택
- `콘티`: `CONTI.md` 편집
- `설정`: `.env` 편집
- `픽커`: 컷별 이미지/영상 후보 선택, 이미지 추가 생성
- `모션`: 이미지 컷 모션 편집, 외부 영상 컷 미리보기
- `싱크`: 자막 시작점과 텍스트 편집
- `BGM`: `RENDERS/final.mp4`에 BGM 믹스, Final Cut / CapCut 내보내기

단축키는 물리 키 기준이라 한영 입력 상태와 무관하게 동작한다.

```text
Z / X      이전 / 다음 탭
W / S      이전 / 다음 컷
A / D      후보 좌우 이동 또는 영상 0.5초 이동
Space      선택/재생 토글
1~9        후보 직접 선택
Q          싱크 스냅
C          모션 초점 중앙
```

## SSCUT v1 파일 시스템

프로젝트 루트에는 반드시 `SSCUT.json`을 둔다. 이 파일이 VASP의 `INCAR/POSCAR`처럼 프로젝트를 여는 기준 파일이다.

```text
my_project/
  SSCUT.json
  CONTI.md
  SCRIPT.md
  PICKS.json
  TIMELINE.json
  BGM.json

  CUTS/
    01_ragdoll/
      CUT.json
      PROMPT.md
      images/
        openai/
          gpt-image-1/
            gpt_001.png
            gpt_002.png
            _meta.txt
      videos/
        external/
          external_test.mp4

    02_bengal/
      CUT.json
      PROMPT.md
      images/
      videos/

  AUDIO/
    BGM/
      main_theme.mp3

  CACHE/
    video/
      01_ragdoll_base.mp4
      02_bengal_tts.mp3

  RENDERS/
    final.mp4
    final_bgm.mp4

  EXPORTS/
    finalcut/
      SSCUT.fcpxml
      SSCUT.srt
      manifest.json
    capcut/
      SSCUT/
        media/
        audio/
        subtitles.srt
        timeline.csv
        manifest.json
        README.txt
```

역할:

- `SSCUT.json`: 프로젝트 manifest. 앱이 가장 먼저 읽는 파일
- `CONTI.md`: 사람이 읽고 수정하는 콘티
- `SCRIPT.md`: 원 대본이나 입력 스크립트
- `CUTS/<cut_id>/`: 컷별 모든 소스와 메타데이터
- `CUT.json`: 컷 단위 고정 메타데이터
- `PROMPT.md`: 해당 컷의 기준 프롬프트
- `images/<provider>/<model>/`: 모델별 이미지 후보
- `videos/<provider>/`: 외부 영상 또는 모델별 영상 후보
- `PICKS.json`: 현재 선택된 이미지/영상
- `TIMELINE.json`: 자막, 컷 타이밍, 모션, duration 같은 편집 정보
- `BGM.json`: 최종 렌더에 입힐 BGM 설정
- `AUDIO/BGM/`: 프로젝트에서 쓰는 BGM 원본 파일
- `CACHE/`: 편집 중 생성되는 base video, TTS, preview 등 재생성 가능한 캐시
- `RENDERS/`: 최종 출력물
- `EXPORTS/`: 외부 NLE로 넘길 내보내기 산출물

`CONTI.md` 규칙:

앱은 `CONTI.md`의 첫 번째 markdown 표를 콘티 표로 읽는다. 표는 아래 컬럼명을 기준으로 동작한다.

```markdown
| 컷 번호 | 컷 ID | 시간 | 메인 장면 | 컷 구성 | 내레이션 |
|---|---|---|---|---|---|
| 1 | 01_ragdoll | 0:00~0:03.3 | ... | ... | ... |
```

필수 규칙:

- `컷 ID`는 `CUTS/<cut_id>/` 폴더명과 같아야 한다.
- `컷 번호`는 사람이 보는 순서 번호다.
- `SSCUT.json`의 `cut_order`와 `컷 ID` 순서를 맞춘다.
- 이미지 추가 생성은 `CUTS/<cut_id>/PROMPT.md`를 기준 프롬프트로 사용한다.
- BGM과 SFX는 컷 카드가 아니라 `AUDIO/BGM/`과 `BGM.json`에서 관리한다.

`SSCUT.json` 예시:

```json
{
  "schema": "sscut-project-v1",
  "name": "test2_cat",
  "title": "테스트 스크립트 고양이",
  "conti": "CONTI.md",
  "cuts_dir": "CUTS",
  "cut_order": ["01_ragdoll", "02_bengal", "03_siamese"],
  "picks": "PICKS.json",
  "timeline": "TIMELINE.json",
  "bgm": "BGM.json",
  "audio_dir": "AUDIO",
  "renders_dir": "RENDERS",
  "cache_dir": "CACHE"
}
```

`PICKS.json` 예시:

```json
{
  "schema": "sscut-picks-v1",
  "selections": {
    "01_ragdoll": "videos/external/external_test.mp4",
    "02_bengal": "images/openai/gpt-image-1/gpt_002.png"
  }
}
```

`BGM.json` 예시:

```json
{
  "schema": "sscut-bgm-v1",
  "selected_clip_id": "bgm_001",
  "clips": [
    {
      "id": "bgm_001",
      "track": "BGM/main_theme.mp3",
      "timeline_start": 0.0,
      "source_start": 12.5,
      "duration": 8.0,
      "volume": 0.25,
      "fade_in": 1.0,
      "fade_out": 2.0,
      "loop": false
    },
    {
      "id": "bgm_002",
      "track": "BGM/bed_loop.mp3",
      "timeline_start": 8.0,
      "source_start": 0.0,
      "duration": 0.0,
      "volume": 0.18,
      "fade_in": 0.5,
      "fade_out": 2.0,
      "loop": true
    }
  ]
}
```

BGM 렌더링 규칙:

- 입력 파일은 `RENDERS/final.mp4`다.
- 출력 파일은 `RENDERS/final_bgm.mp4`다.
- 각 BGM 클립의 `track`은 `AUDIO/` 기준 상대 경로이며 현재 표준 위치는 `BGM/<filename>`이다.
- `timeline_start`는 최종 영상에서 들어갈 시각, `source_start`는 오디오 원본에서 잘라 시작할 시각이다.
- `duration`이 `0`이면 자동 길이다. `loop: true`면 최종 영상 끝까지 반복하고, `loop: false`면 원본 남은 길이만 사용한다.
- 여러 클립은 서로 겹칠 수 있으며 최종 영상의 원래 오디오와 함께 믹스된다.

## Final Cut / CapCut 내보내기

BGM 탭 오른쪽 상단에서 내보내기 형식을 선택한다.

- `Final Cut`: `EXPORTS/finalcut/*.fcpxml`과 `*.srt`를 만든다.
- `CapCut`: `EXPORTS/capcut/<project>/` 아래에 미디어 파일, TTS 오디오, `subtitles.srt`, `timeline.csv`, `manifest.json`을 만든다.

Final Cut 내보내기는 원본 이미지/영상과 TTS 오디오를 참조하는 FCPXML이다. 자막은 함께 생성된 SRT를 별도로 import한다.

CapCut은 안정적인 공개 프로젝트 타임라인 import 포맷이 없으므로, SSCUT은 CapCut이 확실히 읽을 수 있는 파일 패키지를 만든다. CapCut에서는 `media/`, `audio/`, `subtitles.srt`를 import하고 `timeline.csv`를 기준으로 컷 길이와 배치를 맞춘다.

현재 내보내기 1차 구현 범위:

- 컷 순서
- 선택된 이미지/외부 영상
- 컷별 길이
- TTS 오디오 파일
- SRT 자막
- CapCut 수동 배치용 CSV

아직 1차 구현 범위 밖:

- Final Cut/CapCut motion keyframe 자동 변환
- CapCut 비공개 draft 파일 직접 생성

## 마이그레이션 도구

기존 실험 폴더를 SSCUT v1으로 한 번 정리할 때만 사용한다. 편집 앱 자체는 SSCUT v1 구조만 읽는다.

```bash
./run.sh scripts/migrate_to_sscut.py \
  /path/to/legacy_project \
  /path/to/new_sscut_project
```

## API 키와 배포

`.env`와 `.env.local`은 개인 secret이다. Git이나 배포 산출물에 넣지 않는다.

현재 앱은 로컬 편집기를 우선한다. 여러 회사/로컬 모델을 같은 프롬프트로 비교하는 장기 목표는 `images/<provider>/<model>/`, `videos/<provider>/<model>/` 아래에 결과를 모으는 방식으로 확장한다.
