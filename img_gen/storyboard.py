"""CONTI.md 의 첫 markdown 표 → 구조화된 Cut 리스트."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 컬럼 헤더 매칭 키워드 (소문자 부분일치)
INDEX_KEYS = ("컷 번호", "컷 no", "#호흡", "호흡", "index", "cut number", "cut no", "#")
CUT_ID_KEYS = ("컷 id", "컷 아이디", "cut_id", "cut id")
NARRATION_KEYS = ("대사", "내레이션", "narration", "voice")
SCENE_KEYS = ("메인 장면", "장면", "scene")
PROMPT_KR_KEYS = ("프롬프트 (한국어)", "프롬프트 (kr)", "prompt (kr)")
PROMPT_EN_KEYS = ("프롬프트 (english)", "프롬프트 (en)", "prompt (en)")
TIME_KEYS = ("시간", "time", "duration")
SUBCUT_KEYS = ("컷 구성", "컷 분할", "시각 컷", "sub-cut", "subcut")
BGM_KEYS = ("bgm", "sfx", "음악", "사운드")


@dataclass
class Cut:
    index: int
    cut_id: str = ""
    time: str = ""
    scene: str = ""
    subcut: str = ""
    narration: str = ""
    bgm: str = ""
    prompt_kr: str = ""
    prompt_en: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Storyboard:
    title: str
    cuts: list[Cut]
    raw_text: str


def _matches(header: str, keys: tuple[str, ...]) -> bool:
    h = header.lower().strip()
    return any(k in h for k in keys)


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    if not s.startswith("|"):
        return False
    return bool(re.match(r"^\|[\s:|\-]+\|?$", s))


def parse_storyboard(path: Path) -> Storyboard:
    text = path.read_text(encoding="utf-8")

    # 제목 = 첫 # 라인
    title_match = re.search(r"^#\s+(.+?)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem

    lines = text.splitlines()

    # 첫 markdown 표 찾기: |...| 다음 줄이 separator
    table_start = None
    for i in range(len(lines) - 1):
        cur = lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if cur.startswith("|") and not _is_table_separator(cur) and _is_table_separator(nxt):
            table_start = i
            break

    if table_start is None:
        return Storyboard(title=title, cuts=[], raw_text=text)

    # 헤더 파싱
    header_cells = [c.strip() for c in lines[table_start].strip().strip("|").split("|")]

    def col_index(keys: tuple[str, ...]) -> int | None:
        for idx, h in enumerate(header_cells):
            if _matches(h, keys):
                return idx
        return None

    idx_time = col_index(TIME_KEYS)
    idx_index = col_index(INDEX_KEYS)
    idx_cut_id = col_index(CUT_ID_KEYS)
    idx_scene = col_index(SCENE_KEYS)
    idx_subcut = col_index(SUBCUT_KEYS)
    idx_narr = col_index(NARRATION_KEYS)
    idx_bgm = col_index(BGM_KEYS)
    idx_kr = col_index(PROMPT_KR_KEYS)
    idx_en = col_index(PROMPT_EN_KEYS)

    def get(cells: list[str], i: int | None) -> str:
        if i is None or i >= len(cells):
            return ""
        return cells[i].replace("<br>", "\n").strip()

    cuts: list[Cut] = []
    for j in range(table_start + 2, len(lines)):
        s = lines[j].strip()
        if not s.startswith("|"):
            break
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        index_src = get(cells, idx_index) if idx_index is not None else cells[0]
        idx_match = re.search(r"\d+", index_src)
        if not idx_match:
            continue
        cut = Cut(
            index=int(idx_match.group()),
            cut_id=get(cells, idx_cut_id),
            time=get(cells, idx_time),
            scene=get(cells, idx_scene),
            subcut=get(cells, idx_subcut),
            narration=get(cells, idx_narr),
            bgm=get(cells, idx_bgm),
            prompt_kr=get(cells, idx_kr),
            prompt_en=get(cells, idx_en),
            raw=dict(zip(header_cells, cells)),
        )
        cuts.append(cut)

    return Storyboard(title=title, cuts=cuts, raw_text=text)
