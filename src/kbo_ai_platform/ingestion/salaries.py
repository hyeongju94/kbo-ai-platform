"""KBO 선수 연봉 (2000~2026) → raw.player_salary, raw.team_salary.

출처는 KBO 공식 사이트 두 곳뿐이다 (기록 사이트는 쓰지 않는다).
1) KBO 보도자료 (koreabaseball.com/MediaNews/Notice) — 매년 2~3월 '소속선수 등록 현황 / 평균 연봉'
   발표와 12월 '구단별 연봉 상위 40명 합계' 발표. 본문과 첨부(xls·xlsx·docx)의 표를 읽는다.
   - 억대 연봉 선수 현황: 2005~2021 은 연도별 전체 명단(구단·위치·연봉), 2000~2004 는
     '억대 연봉 현황(1985~2004)' 요약표(이름·연봉만, 구단은 기록 테이블로 채움).
   - 구단별 연봉 상위 5걸(2026 은 3걸), 부문별 상위 10걸, 위치별 상위, 연차별 최고액 (2014~2026)
   - 외국인선수 현황 (계약금·연봉, 달러) (2014~2026), 아시아쿼터 (2026)
   - 구단별 평균 연봉 비교(신인·외국인 제외 소속선수), 상위 27~29명 기준, 상위 40명 합계 → raw.team_salary
2) KBO 공식 사이트 선수 정보 (raw.player_profile 의 연봉) — 지금 페이지에 보이는 값이라
   현재 10개 구단 소속이면서 2026 시즌 기록·등록 명단에 있는 선수만 2026 연봉으로 넣는다.

2022~2025 는 KBO 가 억대 연봉 전체 명단을 더 이상 첨부하지 않아서 상위 선수·외국인만 있다.
연봉 단위: 국내 선수 만원(salary_manwon), 외국인 달러(salary_usd). 확인되지 않는 값은 NULL.

실행:
    uv run python -m kbo_ai_platform.ingestion.salaries            # 보도자료 받기(캐시) + 적재
    uv run python -m kbo_ai_platform.ingestion.salaries --offline  # 저장된 파일만 다시 파싱
"""

from __future__ import annotations

import argparse
import html as html_lib
import logging
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import lxml.html
import openpyxl
import pandas as pd
import requests
import xlrd
from sqlalchemy import TEXT, Engine, text
from sqlalchemy.dialects.postgresql import ARRAY

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine

log = logging.getLogger(__name__)

SCHEMA = "raw"
PLAYER_TABLE = "player_salary"
TEAM_TABLE = "team_salary"
SITE = "https://www.koreabaseball.com"
LIST_URL = f"{SITE}/MediaNews/Notice/List.aspx"
VIEW_URL = f"{SITE}/MediaNews/Notice/View.aspx?bdSe={{}}"
CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "kbo_salary"
FILES_DIR = CACHE_DIR / "files"
DOC_HTML_DIR = CACHE_DIR / "doc_html"
HEADERS = {"User-Agent": "Mozilla/5.0 (kbo-ai-platform portfolio data collection)"}
REQUEST_GAP_SEC = 0.5
SEASONS = range(2000, 2027)

SEARCH_WORDS = ["연봉", "소속선수", "등록"]
TITLE_RE = re.compile(r"(소속\s*선수|등록선수|선수\s*\d*명?\s*등록|평균\s*연봉|연봉\s*상위|연봉\s*현황)")
TITLE_SKIP_RE = re.compile(r"(조정|올림픽|대표팀|심판|아카데미|앱|입찰|최초)")
FORM_PREFIX = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$"

TEAMS = {
    "LG", "KT", "SSG", "SK", "NC", "KIA", "두산", "롯데", "삼성", "한화", "키움", "넥센", "히어로즈",
    "우리", "현대", "해태", "쌍방울",
}
TOTAL_LABELS = {"계", "총계", "합계", "총합계/평균", "총 합계/평균", "계/평균", "전체"}
HERO_ALIASES = {"우리", "히어로즈", "넥센", "키움"}
PROFILE_TEAMS = {  # player_profile.team_name → 2026 구단 약칭
    "LG 트윈스": "LG", "KT 위즈": "KT", "SSG 랜더스": "SSG", "NC 다이노스": "NC", "KIA 타이거즈": "KIA",
    "두산 베어스": "두산", "롯데 자이언츠": "롯데", "삼성 라이온즈": "삼성", "한화 이글스": "한화",
    "키움 히어로즈": "키움",
}

# 같은 선수·시즌이 여러 표에 나오면 앞의 표 값을 쓴다 (전체 명단 > 상위 선수 표 > 선수 정보 페이지)
SOURCE_PRIORITY = [
    "억대 연봉 선수 현황", "외국인선수 현황", "아시아쿼터 선수 현황", "구단별 선수연봉 상위", "부문별 연봉 상위",
    "위치별 연봉 상위", "연차별 연봉 최고액", "억대 연봉 현황(1985~2004)", "KBO 선수 정보(현재 연봉)",
]


# ---------------------------------------------------------------- 수집 (보도자료 + 첨부)

@dataclass
class Release:
    bd_se: int
    title: str
    season: int
    url: str
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (원래 파일명, 저장 경로)


def _hidden_form(doc) -> dict:
    return {i.get("name"): i.get("value", "") for i in doc.xpath("//input[@type='hidden']") if i.get("name")}


def search_notices(session: requests.Session, word: str, max_pages: int = 6) -> dict[int, str]:
    """보도자료 게시판 제목 검색. {bdSe: 제목}"""
    doc = lxml.html.fromstring(session.get(LIST_URL, timeout=30).text)
    form = _hidden_form(doc)
    form.update({FORM_PREFIX + "ddlWord": "TT", FORM_PREFIX + "txtSearchWord": word,
                 "__EVENTTARGET": FORM_PREFIX + "btnSearch"})
    found: dict[int, str] = {}
    for page in range(1, max_pages + 1):
        doc = lxml.html.fromstring(session.post(LIST_URL, data=form, timeout=30).text)
        links = doc.xpath("//a[contains(@href, 'Notice/View.aspx?bdSe=')]")
        new = 0
        for a in links:
            bd = int(re.search(r"bdSe=(\d+)", a.get("href")).group(1))
            if bd not in found:
                found[bd] = a.text_content().strip()
                new += 1
        if not new:
            break
        form = _hidden_form(doc)
        nxt = page + 1
        target = f"ucPager$btnNo{(nxt - 1) % 5 + 1}" if (nxt - 1) % 5 else "ucPager$btnNext"
        form.update({FORM_PREFIX + "ddlWord": "TT", FORM_PREFIX + "txtSearchWord": word,
                     "__EVENTTARGET": FORM_PREFIX + target})
        time.sleep(REQUEST_GAP_SEC)
    return found


def _safe_name(name: str) -> str:
    return re.sub(r"[/\\:\s]+", "_", name)


def _extract_zip(path: Path) -> list[Path]:
    out_dir = path.with_suffix("")
    out_dir.mkdir(exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw = info.filename.encode() if info.flag_bits & 0x800 else info.filename.encode("cp437")
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                name = raw.decode("cp949", errors="replace")
            target = out_dir / _safe_name(Path(name).name)
            if not target.exists():
                target.write_bytes(zf.read(info))
            out.append(target)
    return out


def _docx_to_html(path: Path) -> Path | None:
    """macOS textutil 로 .doc/.docx 를 HTML 로 바꾼다 (표 파싱용). 없으면 건너뛴다."""
    DOC_HTML_DIR.mkdir(parents=True, exist_ok=True)
    out = DOC_HTML_DIR / (path.stem + ".html")
    if out.exists():
        return out
    try:
        subprocess.run(["textutil", "-convert", "html", "-output", str(out), str(path)],
                       check=True, capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.SubprocessError):
        log.warning("textutil 변환 실패: %s", path.name)
        return None
    return out


def fetch_releases(offline: bool) -> list[Release]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    pages = sorted(CACHE_DIR.glob("notice_*.html"))
    if not offline:
        titles: dict[int, str] = {}
        for w in SEARCH_WORDS:
            titles.update(search_notices(session, w))
        for bd, title in sorted(titles.items()):
            if not TITLE_RE.search(title) or TITLE_SKIP_RE.search(title):
                continue
            p = CACHE_DIR / f"notice_{bd}.html"
            if not p.exists():
                p.write_text(session.get(VIEW_URL.format(bd), timeout=30).text, encoding="utf-8")
                time.sleep(REQUEST_GAP_SEC)
        pages = sorted(CACHE_DIR.glob("notice_*.html"))

    releases: list[Release] = []
    for p in pages:
        m = re.fullmatch(r"notice_(\d+)\.html", p.name)
        if not m:
            continue
        bd = int(m.group(1))
        doc = lxml.html.fromstring(p.read_text(encoding="utf-8"))
        view = doc.xpath("//div[@class='view']")
        if not view:
            continue
        title = " ".join(view[0].xpath(".//h4|.//h3|.//strong")[0].text_content().split()) if view[0].xpath(
            ".//h4|.//h3|.//strong") else ""
        if not title:
            title = " ".join(view[0].text_content().split())[:80]
        ym = re.search(r"(20\d\d)", title)
        if not ym or int(ym.group(1)) not in SEASONS:
            continue
        rel = Release(bd, title, int(ym.group(1)), VIEW_URL.format(bd))
        for a in doc.xpath("//div[@class='attatch']//a[contains(@href, 'FileDownload')]"):
            name = html_lib.unescape(a.text_content().strip())
            local = FILES_DIR / f"{bd}__{_safe_name(name)}"
            if not local.exists():
                if offline:
                    continue
                r = session.get(SITE + a.get("href"), timeout=60)
                r.raise_for_status()
                local.write_bytes(r.content)
                time.sleep(REQUEST_GAP_SEC)
            rel.attachments.append((name, str(local)))
        releases.append(rel)
    log.info("보도자료 %d건, 첨부 %d개", len(releases), sum(len(r.attachments) for r in releases))
    return releases


# ---------------------------------------------------------------- 표 읽기

@dataclass
class Grid:
    rows: list[list[str]]
    release: Release
    file_name: str
    sheet: str


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _excel_grids(path: Path, rel: Release, name: str) -> list[Grid]:
    out = []
    if path.suffix.lower() == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            if ws.sheet_state != "visible":
                continue
            rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
            out.append(Grid(rows, rel, name, ws.title))
    else:
        wb = xlrd.open_workbook(path)
        for sh in wb.sheets():
            if sh.visibility:
                continue
            rows = [[_cell(c) for c in sh.row_values(i)] for i in range(sh.nrows)]
            out.append(Grid(rows, rel, name, sh.name))
    return out


def _html_grids(path: Path, rel: Release, name: str) -> list[Grid]:
    doc = lxml.html.fromstring(path.read_text(encoding="utf-8", errors="ignore"))
    out = []
    for i, t in enumerate(doc.xpath("//table")):
        rows = []
        for tr in t.xpath(".//tr"):
            row = []
            for td in tr.xpath("./td|./th"):
                txt = "\n".join(s.strip() for s in td.itertext() if s.strip())
                row.append(txt)
                row.extend([""] * (int(td.get("colspan", "1") or 1) - 1))
            rows.append(row)
        out.append(Grid(rows, rel, name, f"table{i + 1}"))
    return out


def load_grids(releases: list[Release]) -> list[Grid]:
    grids: list[Grid] = []
    for rel in releases:
        files: list[tuple[str, Path]] = []
        for name, local in rel.attachments:
            p = Path(local)
            if p.suffix.lower() == ".zip":
                files.extend((x.name, x) for x in _extract_zip(p))
            else:
                files.append((name, p))
        for name, p in files:
            suf = p.suffix.lower()
            try:
                if suf in (".xls", ".xlsx"):
                    grids.extend(_excel_grids(p, rel, name))
                elif suf in (".docx", ".doc"):  # .doc 는 표가 머리글만 남는 경우가 많다(그러면 결과 없음)
                    h = _docx_to_html(p)
                    if h:
                        grids.extend(_html_grids(h, rel, name))
            except Exception as exc:  # 첨부 하나가 깨져도 나머지는 계속
                log.warning("첨부 읽기 실패 %s: %s", name, exc)
    return grids


# ---------------------------------------------------------------- 값 정리

def norm_name(s: str) -> str:
    """'최  정' → '최정', 줄바꿈·별표 제거. 외국인 '케이시 켈리\\n(Casey Kelly)' 는 한글 부분만."""
    s = s.replace("\n", " ").replace("*", "").strip()
    tag = re.search(r"\((\d+)\)", s)  # '이병규(9)' 처럼 같은 팀 동명이인을 등번호로 구분한 표기는 남긴다
    s = re.sub(r"\(.*?\)", "", s).strip()
    toks = s.split()
    if len(toks) > 1 and re.fullmatch(r"[가-힣 ]+", s) and sum(len(t) > 1 for t in toks) <= 1:
        s = s.replace(" ", "")  # '최  정', '켈  리', '손 혁' (표에서 칸을 맞추려 띄운 이름)
    s = re.sub(r"\s+", " ", s)
    return f"{s}({tag.group(1)})" if tag else s


def name_key(s: str) -> str:
    """동일인 비교용: 공백·등번호 표기를 뺀 이름."""
    return re.sub(r"\s+|\(\d+\)", "", norm_name(s))


def norm_team(s: str) -> str:
    s = s.replace("\n", " ").strip()
    return {"kt": "KT", "Kt": "KT", "K T": "KT", "기아": "KIA"}.get(s, s)


def split_names(cell: str) -> list[str]:
    """한 칸에 여러 이름(공동 순위): '김광현, 최정', '홍건희\n김재호, 양석환'.
    docx 변환본은 '최\n정' 처럼 한 이름이 글자마다 줄바꿈되기도 해서 한 글자 조각은 이어 붙인다."""
    if re.search(r"외\s*\d+\s*명", cell):
        return []
    pieces = [p.strip() for p in re.split(r"[,·\n]", cell) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in pieces:
        if len(p) == 1 and re.fullmatch(r"[가-힣]", p):
            buf += p
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(p)
    if buf:
        out.append(buf)
    return out


def pair_names_teams(name_cell: str, team_cell: str) -> list[tuple[str, str]]:
    """이름 칸·구단 칸을 (이름, 구단) 쌍으로. 원문에서 두 칸이 뒤바뀐 줄(2023 부문별 투수)은 바로잡는다."""
    if norm_team(name_cell) in TEAMS and norm_team(team_cell) not in TEAMS:
        name_cell, team_cell = team_cell, name_cell
    names = split_names(name_cell)
    teams = [norm_team(t) for t in re.split(r"[,\n\s]+", team_cell) if t.strip()]
    if not names or any(t not in TEAMS for t in teams):
        return []
    if len(teams) == 1:
        return [(n, teams[0]) for n in names]
    if len(teams) == len(names):
        return list(zip(names, teams))
    log.debug("이름·구단 수가 안 맞아 건너뜀: %r / %r", name_cell, team_cell)
    return []


def to_int(s) -> int | None:
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    m = re.match(r"^-?\d+(\.\d+)?$", t)
    if not m:
        m2 = re.match(r"^(\d+)", t)
        return int(m2.group(1)) if m2 else None
    return int(round(float(t)))


def parse_kor_manwon(s: str) -> int | None:
    """'4억 7천' → 47000, '1억8천7백5십' → 18750, '1억4백84만원' → 10484, '3억' → 30000 (만원)."""
    t = re.sub(r"\s|만원|원", "", s)
    m = re.match(r"^(\d+)억(.*)$", t)
    if not m:
        return to_int(t)
    total = int(m.group(1)) * 10000
    mult = {"천": 1000, "백": 100, "십": 10, "": 1}
    for num, unit in re.findall(r"(\d+)(천|백|십)?", m.group(2)):
        total += int(num) * mult[unit or ""]
    return total


def parse_eok_text(s: str) -> int | None:
    """'132억 700만원' → 1320700 (만원)."""
    t = re.sub(r"\s", "", s)
    m = re.match(r"^(\d+)억(?:([\d,]+)만)?", t)
    if m:
        return int(m.group(1)) * 10000 + (to_int(m.group(2)) or 0)
    m = re.match(r"^([\d,]+)만", t)
    return to_int(m.group(1)) if m else None


def _year_in(rows: list[list[str]], n: int = 4) -> int | None:
    for r in rows[:n]:
        for c in r:
            m = re.search(r"(20\d\d)\s*년?\s*억대", c) or re.search(r"(20\d\d)", c)
            if m and int(m.group(1)) in SEASONS:
                return int(m.group(1))
    return None


def _is_title(row: list[str]) -> bool:
    c = row[0] if row else ""
    return bool(re.match(r"^\s*\d+\)\s*", c))


def _row_text(row: list[str]) -> str:
    return " ".join(c for c in row if c)


# ---------------------------------------------------------------- 표 종류별 파서 (선수 연봉 행)

def _rec(season, team, name, *, grid: Grid, source_table: str, position=None, manwon=None, usd=None,
         bonus_usd=None, is_foreign=False, rank=None, note=None) -> dict:
    return {
        "season": season, "team_name": norm_team(team) if team else None, "player_name": norm_name(name),
        "player_name_raw": name.replace("\n", " ").strip(), "position": position or None,
        "salary_manwon": manwon, "salary_usd": usd, "signing_bonus_usd": bonus_usd, "is_foreign": is_foreign,
        "rank_in_table": rank, "note": note or None, "source_type": "kbo_press_release",
        "source_table": source_table, "source_title": grid.release.title, "source_url": grid.release.url,
        "source_file": grid.file_name, "source_sheet": grid.sheet, "release_season": grid.release.season,
    }


def parse_eokdae_list(g: Grid) -> list[dict]:
    """억대 연봉 선수 현황: 순위|구단|위치|선수명|연봉|비고 두 묶음."""
    out = []
    rows = g.rows
    season = _year_in(rows) or g.release.season
    for i, r in enumerate(rows):
        idx = [j for j, c in enumerate(r) if c.replace(" ", "") == "선수명"]
        if not idx or not any("연봉" in c for c in r) or not any(c.startswith("구단") for c in r):
            continue
        blocks = []
        for j in idx:
            def find(label, lo, hi):
                for k in range(lo, hi):
                    if k < len(r) and r[k].replace(" ", "").startswith(label):
                        return k
                return None
            lo, hi = max(0, j - 3), min(len(r), j + 3)
            blocks.append({"rank": find("순위", lo, j), "team": find("구단", lo, j), "pos": find("위치", lo, j),
                           "name": j, "sal": find("연봉", j, hi), "note": find("비고", j, hi)})
        for r2 in rows[i + 1:]:
            if _is_title(r2):
                break
            for b in blocks:
                if b["sal"] is None or b["name"] >= len(r2):
                    continue
                name, sal = r2[b["name"]], to_int(r2[b["sal"]] if b["sal"] < len(r2) else None)
                if not name or sal is None or name.replace(" ", "") == "선수명":
                    continue
                def get(k, row=r2):
                    return row[k] if k is not None and k < len(row) else ""
                out.append(_rec(season, get(b["team"]), name, grid=g, source_table="억대 연봉 선수 현황",
                                position=get(b["pos"]), manwon=sal, rank=to_int(get(b["rank"])), note=get(b["note"])))
        break
    return out


def parse_eokdae_summary(g: Grid) -> list[dict]:
    """억대 연봉 현황(1985~2004): '00년(31명) | 정민태(3억1천), 이승엽(3억), ...' → 2000~2004."""
    out = []
    for r in g.rows:
        if len(r) < 2:
            continue
        m = re.match(r"^\s*(\d\d)년\s*\((\d+)명\)", r[0])
        if not m:
            continue
        yy = int(m.group(1))
        season = 2000 + yy if yy < 50 else 1900 + yy
        if season not in SEASONS:
            continue
        body = " ".join(r[1:])
        items = re.findall(r"([가-힣][가-힣 ]{0,5}?)\s*\(([^()]*\d[^()]*)\)", body)
        for rank, (name, amount) in enumerate(items, start=1):
            team = None
            parts = [p.strip() for p in amount.split(",")]
            amt = parts[-1]
            if len(parts) > 1:
                team = parts[-1].split()[0] if len(parts[-1].split()) > 1 else None
                amt = parts[-1].split()[-1]
            out.append(_rec(season, team, name, grid=g, source_table="억대 연봉 현황(1985~2004)",
                            manwon=parse_kor_manwon(amt), rank=rank, note=f"요약표 {int(m.group(2))}명 중"))
    return out


def parse_team_top(g: Grid) -> list[dict]:
    """구단별 선수연봉 상위 5걸/3걸: 팀|선수명|1위..; 다음 줄 |연봉|금액.."""
    out = []
    rows = g.rows
    for i, r in enumerate(rows[:-1]):
        if len(r) < 3 or r[1].replace(" ", "") != "선수명" or rows[i + 1][1].replace(" ", "") != "연봉":
            continue
        team, sal_row = r[0], rows[i + 1]
        for j in range(2, len(r)):
            names = split_names(r[j])
            sal = to_int(sal_row[j]) if j < len(sal_row) else None
            if not names or sal is None:
                continue
            for n in names:
                out.append(_rec(g.release.season, team, n, grid=g, source_table="구단별 선수연봉 상위",
                                manwon=sal, rank=j - 1))
    return out


def parse_category_top(g: Grid) -> list[dict]:
    """부문별 연봉 상위 10걸: 투수 부문 / 타자 부문 (외국인 선수 열은 원화 환산+계약금 포함이라 뺀다).
    묶음 위치는 하위 머리글의 '선수명' 열로 정하고, 부문 이름은 위 줄에 나온 순서대로 붙인다
    (docx 변환본은 위 줄 병합 셀이 풀리지 않아 열 위치가 어긋난다)."""
    out = []
    rows = g.rows
    for i, r in enumerate(rows[:-1]):
        cats = [c.replace("\n", " ") for c in r if re.search(r"(투수|타자|외국인)\s*(부문|선수)", c)]
        if not any(re.search(r"(투수|타자)\s*부문", c) for c in cats):
            continue
        sub = [c.replace(" ", "") for c in rows[i + 1]]
        starts = [k for k, c in enumerate(sub) if c == "선수명"]
        blocks = []
        for cat, k in zip(cats, starts):
            if "외국인" in cat:
                continue
            ct = k + 1 if k + 1 < len(sub) and sub[k + 1] == "구단" else None
            cs = next((x for x in range(k + 1, min(k + 4, len(sub))) if sub[x].startswith("연봉")), None)
            if ct is not None and cs is not None:
                blocks.append((cat, k, ct, cs))
        for r2 in rows[i + 2:]:
            if _is_title(r2) or (r2 and ("위치" in r2[0] or r2[0].replace(" ", "") == "순위")):
                break
            for cat, cn, ct, cs in blocks:
                if cs >= len(r2) or not r2[cn]:
                    continue
                sal = to_int(r2[cs])
                if sal is None:
                    continue
                for name, team in pair_names_teams(r2[cn], r2[ct]):
                    out.append(_rec(g.release.season, team, name, grid=g, source_table="부문별 연봉 상위",
                                    position="투수" if "투수" in cat else None, manwon=sal,
                                    rank=to_int(r2[cn - 1]) if cn else None, note=cat))
        break
    return out


def parse_position_top(g: Grid) -> list[dict]:
    """위치별 연봉 상위: 위치|순위|선수명|구단|연봉 두 묶음 (위치는 아래로 이어진다)."""
    out = []
    rows = g.rows
    for i, r in enumerate(rows):
        keys = [c.replace(" ", "") for c in r]
        if "위치" not in keys or "선수명" not in keys or "연봉" not in keys or "투수부문" in "".join(keys):
            continue
        starts = [j for j, k in enumerate(keys) if k in ("위치", "순위") and (j == 0 or keys[j - 1] in ("", "연봉"))]
        blocks = []
        for s in starts:
            seg = {}
            for k in range(s, min(s + 5, len(keys))):
                if keys[k] in ("위치", "순위", "선수명", "구단", "연봉") and keys[k] not in seg:
                    seg[keys[k]] = k
            if {"위치", "선수명", "구단", "연봉"} <= seg.keys():
                blocks.append(seg)
        cur_pos = [""] * len(blocks)
        for r2 in rows[i + 1:]:
            if _is_title(r2):
                break
            for b_i, b in enumerate(blocks):
                if b["연봉"] >= len(r2):
                    continue
                if r2[b["위치"]]:
                    cur_pos[b_i] = r2[b["위치"]].replace("\n", "")
                sal = to_int(r2[b["연봉"]])
                if not r2[b["선수명"]] or sal is None:
                    continue
                pos = cur_pos[b_i]
                for name, team in pair_names_teams(r2[b["선수명"]], r2[b["구단"]]):
                    out.append(_rec(g.release.season, team, name, grid=g,
                                    source_table="위치별 연봉 상위", position=pos, manwon=sal,
                                    rank=to_int(r2[b["순위"]]) if "순위" in b else None, note=f"위치별 {pos}"))
        break
    return out


def parse_by_tenure(g: Grid) -> list[dict]:
    """연차별 연봉 최고액 선수: 구분|선수명|구단|금액(올해)|역대... 첫 묶음만 쓴다."""
    out = []
    rows = g.rows
    for i, r in enumerate(rows):
        keys = [c.replace(" ", "") for c in r]
        if "선수명" not in keys or "금액" not in keys or not any(
                re.match(r"^\d+년차", x[0].replace(" ", "")) for x in rows[i + 1:i + 4] if x):
            continue
        cn = keys.index("선수명")
        ct = keys.index("구단", cn) if "구단" in keys[cn:] else cn + 1
        cs = keys.index("금액", cn)
        tenure, last_sal = None, None
        for r2 in rows[i + 1:]:
            if _is_title(r2) or len(r2) <= cs:
                continue
            m = re.match(r"^(\d+)년차", r2[0].replace(" ", ""))
            if m:
                tenure, last_sal = int(m.group(1)), None
            if tenure is None or not r2[cn]:
                continue
            sal = to_int(r2[cs])
            sal = sal if sal is not None else last_sal
            last_sal = sal
            if sal is None:
                continue
            for name, team in pair_names_teams(r2[cn], r2[ct] if ct < len(r2) else ""):
                out.append(_rec(g.release.season, team, name, grid=g, source_table="연차별 연봉 최고액",
                                manwon=sal, note=f"{tenure}년차 최고"))
        break
    return out


def parse_foreign(g: Grid) -> list[dict]:
    """외국인선수 현황 / 아시아쿼터: 구단|성명|위치|...|계약금($)|연봉($). 첫 표만 ('계' 줄에서 멈춤).
    같은 시트 아래쪽에 지난 시즌 표가 남아 있는 경우가 있어 두 번째 표는 읽지 않는다."""
    out = []
    rows = g.rows
    title = " ".join(_row_text(r) for r in rows[:3])
    table = "아시아쿼터 선수 현황" if "아시아" in title else "외국인선수 현황"
    for i, r in enumerate(rows):
        keys = [c.replace(" ", "").replace("\n", "") for c in r]
        if "성명" not in keys or not any(k.startswith("연봉") for k in keys):
            continue
        c_team, c_name = keys.index("구단") if "구단" in keys else 0, keys.index("성명")
        c_pos = keys.index("위치") if "위치" in keys else None
        c_sal = next(j for j, k in enumerate(keys) if k.startswith("연봉"))
        c_bonus = next((j for j, k in enumerate(keys) if k.startswith("계약금")), None)
        team = ""
        for r2 in rows[i + 1:]:
            if len(r2) <= c_sal:
                continue
            if r2[c_team].startswith("계") or _is_title(r2) or r2[c_name].replace(" ", "") == "성명":
                break
            team = r2[c_team] or team
            usd = to_int(r2[c_sal])
            if not r2[c_name] or usd is None:
                continue
            en = re.search(r"\(([^)]*)\)", r2[c_name].replace("\n", " "))
            out.append(_rec(g.release.season, team, r2[c_name], grid=g, source_table=table,
                            position=r2[c_pos] if c_pos is not None else None, usd=usd,
                            bonus_usd=to_int(r2[c_bonus]) if c_bonus is not None else None, is_foreign=True,
                            note=f"영문 {en.group(1).strip()}" if en else None))
        break
    return out


# ---------------------------------------------------------------- 구단 연봉 표

def parse_team_salary(g: Grid) -> list[dict]:
    """구단별 소속선수 평균 연봉 비교 / 연봉 상위 N위 기준 평균 연봉 비교 (두 해 나란히)."""
    rows = g.rows
    context = " ".join(_row_text(r) for r in rows[:2])
    if "코칭" in context or "계약금" in context:
        return []
    for i, r in enumerate(rows):
        if not r or not r[0].replace(" ", "").startswith("구분"):
            continue
        head_txt = " ".join(_row_text(x) for x in rows[i:i + 3])
        if "인상률" not in head_txt or "계약금" in head_txt or "연령" in head_txt:
            continue
        data_start = next((k for k in range(i + 1, min(i + 5, len(rows)))
                           if rows[k] and norm_team(rows[k][0]) in TEAMS), None)
        if data_start is None:
            continue
        header = rows[i:data_start]
        width = max(len(x) for x in header)
        # 병합 셀이 풀리지 않은 짧은 머리글 줄(docx 변환본)은 열 위치가 어긋나므로 뺀다
        header = [h for h in header if len(h) >= width - 1]
        labels = [[h[c] if c < len(h) else "" for h in header] for c in range(width)]
        for h_i in range(len(header) - 1):  # 마지막 줄(총액/평균)은 채우지 않는다
            last = ""
            for c in range(1, width):
                if labels[c][h_i]:
                    last = labels[c][h_i]
                else:
                    labels[c][h_i] = last
        cols: dict[tuple[int, str], int] = {}
        first_total = None
        for c in range(1, width):
            lab = " ".join(labels[c])
            if "인상률" in lab:
                continue
            ym = re.findall(r"(20\d\d)", lab)
            if not ym:
                continue
            year = int(ym[-1])
            kind = "total" if "총액" in lab else "avg" if "평균" in lab else "n" if "수" in lab else None
            if kind is None and first_total is None and re.fullmatch(r"\s*20\d\d\s*", lab):
                kind = "n"  # 연도만 적힌 앞쪽 열 = 선수 수
            if kind == "total" and first_total is None:
                first_total = c
            if kind and (year, kind) not in cols:
                cols[(year, kind)] = c
        basis = "top_entry" if "상위" in head_txt or "상위" in context else "registered"
        out = []
        for r2 in rows[data_start:]:
            if not r2 or _is_title(r2):
                break
            t = norm_team(r2[0].replace(" ", ""))
            if t in TOTAL_LABELS or t.startswith("총"):
                t = "합계"
            elif t not in TEAMS:
                continue
            for year in sorted({y for y, _ in cols}):
                def get(k, row=r2, year=year):
                    c = cols.get((year, k))
                    return to_int(row[c]) if c is not None and c < len(row) else None
                total, avg, n = get("total"), get("avg"), get("n")
                if total is None and avg is None:
                    continue
                if n is None and total and avg:
                    n = round(total / avg)
                out.append({"season": year, "team_name": t, "basis": basis, "n_players": n,
                            "total_manwon": total, "avg_manwon": avg,
                            "note": "신인·외국인 제외 소속선수" if basis == "registered" else
                            f"1군 엔트리 기준 연봉 상위 {n}명 (신인·외국인 제외)" if n else "연봉 상위 N명",
                            "source_title": g.release.title, "source_url": g.release.url,
                            "source_file": g.file_name, "source_sheet": g.sheet,
                            "release_season": g.release.season})
        return out
    return []


def parse_top40(g: Grid) -> list[dict]:
    """구단별 연봉 상위 40명 합계 (경쟁균형세 기준, 2023~)."""
    rows = g.rows
    if not rows or rows[0][0].replace(" ", "") != "구단" or not any("합계" in c for c in rows[0]):
        return []
    c_total = next(j for j, c in enumerate(rows[0]) if "합계" in c)
    out = []
    for r in rows[1:]:
        t = norm_team(r[0].strip())
        total = parse_eok_text(r[c_total]) if c_total < len(r) else None
        if total is None:
            continue
        t = "합계" if t not in TEAMS else t
        out.append({"season": g.release.season, "team_name": t, "basis": "top40_cbt",
                    "n_players": 40 if t != "합계" else None, "total_manwon": total,
                    "avg_manwon": round(total / 40) if t != "합계" else None,
                    "note": "경쟁균형세 기준 연봉 상위 40명 합계 (연봉+옵션 실지급액+FA 계약금 연평균 안분, 신인·외국인 제외)"
                    + (" — 합계 행은 10개 구단 총합" if t == "합계" else ""),
                    "source_title": g.release.title, "source_url": g.release.url, "source_file": g.file_name,
                    "source_sheet": g.sheet, "release_season": g.release.season})
    return out


PLAYER_PARSERS = [parse_eokdae_list, parse_eokdae_summary, parse_team_top, parse_category_top,
                  parse_position_top, parse_by_tenure, parse_foreign]


def _grid_kind(g: Grid) -> str | None:
    head = " ".join(_row_text(r) for r in g.rows[:6])
    sheet = g.sheet
    if re.search(r"억대", head + sheet):
        if re.search(r"85|1985", sheet) or any(re.match(r"^\s*\d\d년\s*\(\d+명\)", r[0]) for r in g.rows[:8] if r):
            return "eokdae_summary"
        return "eokdae_list"
    if any(re.match(r"^\s*\d\d년\s*\(\d+명\)", r[0]) for r in g.rows[:8] if r):
        return "eokdae_summary"
    if "선수명" in head.replace(" ", "") and "순위" in head and "비고" in head and "위치" in head:
        return "eokdae_list"
    return None


def build_player_rows(grids: list[Grid]) -> pd.DataFrame:
    recs: list[dict] = []
    for g in grids:
        kind = _grid_kind(g)
        if kind == "eokdae_list":
            recs += parse_eokdae_list(g)
            continue
        if kind == "eokdae_summary":
            recs += parse_eokdae_summary(g)
            continue
        recs += parse_team_top(g) + parse_category_top(g) + parse_position_top(g) + parse_by_tenure(g)
        recs += parse_foreign(g)
    df = pd.DataFrame(recs)
    df = df[df["season"].isin(SEASONS) & df["player_name"].str.len().gt(0)]
    bad_team = df["team_name"].notna() & ~df["team_name"].isin(TEAMS)
    if bad_team.any():
        log.warning("구단 이름을 읽지 못한 %d행은 버린다: %s", bad_team.sum(),
                    df.loc[bad_team, ["season", "team_name", "player_name"]].head(5).to_dict("records"))
    df = df[~bad_team]
    # 등록 명단 첨부(구단별 선수 명단)는 연봉이 없다 → 이미 걸러짐
    return df


def build_team_rows(grids: list[Grid]) -> pd.DataFrame:
    recs: list[dict] = []
    for g in grids:
        recs += parse_team_salary(g) + parse_top40(g)
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df = df[df["season"].isin(SEASONS)]
    # 같은 (시즌, 기준)이 그 시즌 보도자료에 있으면 그것만 쓴다. 다음 해 자료의 '전년도' 열은
    # 구단 이름이 바뀐 뒤 표기(2018 넥센 → 키움, 2021 SK → SSG)라서 섞으면 중복이 생긴다.
    same = df["season"] == df["release_season"]
    has_same = df[same].groupby(["season", "basis"]).size()
    keep = same | ~df.set_index(["season", "basis"]).index.isin(has_same.index)
    df = df[keep].sort_values(["season", "team_name", "basis", "release_season"], ascending=[True, True, True, False])
    df = df.drop_duplicates(["season", "team_name", "basis"])
    return df.reset_index(drop=True)


# ---------------------------------------------------------------- 선수 ID 매칭, 프로필 연봉, 중복 정리

PLAYER_ID_TABLES = ["hitter_basic", "pitcher_basic", "hitter_extra", "pitcher_extra", "defense_basic", "runner_basic"]


def _player_index(engine: Engine) -> pd.DataFrame:
    union = " UNION ".join(f"SELECT season, player_id, player_name, team_name FROM raw.{t}" for t in PLAYER_ID_TABLES)
    with engine.connect() as conn:
        df = pd.read_sql(text(union), conn)
    df["key"] = df["player_name"].map(name_key)
    return df.drop_duplicates(["season", "player_id", "team_name"])


def _same_team(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or (a in HERO_ALIASES and b in HERO_ALIASES) or {a, b} == {"SK", "SSG"}


def match_player_ids(engine: Engine, df: pd.DataFrame) -> pd.DataFrame:
    """(시즌, 이름[, 구단])으로 kbo_player_id 를 찾는다. 같은 시즌에 없으면 앞뒤 시즌도 본다.
    외국인은 '케이시 켈리' → 기록상 '켈리'처럼 이름 일부로도 찾되, 구단이 맞을 때만 쓴다."""
    idx = _player_index(engine)
    lookup: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for r in idx.itertuples(index=False):
        lookup.setdefault((r.season, r.key), []).append((int(r.player_id), r.team_name))
    ids, methods, teams = [], [], []
    for r in df.itertuples(index=False):
        keys = [name_key(r.player_name)]
        team_name = r.team_name if isinstance(r.team_name, str) and r.team_name else None
        if r.is_foreign:
            toks = norm_name(r.player_name).split()
            keys += sorted(toks, key=len, reverse=True) if len(toks) > 1 else []
        found = None
        for delta, label in ((0, "season"), (-1, "adjacent_season"), (1, "adjacent_season")):
            for k_i, k in enumerate(keys):
                cand = lookup.get((r.season + delta, k), [])
                if not cand:
                    continue
                if team_name:
                    same = {pid: t for pid, t in cand if _same_team(t, team_name)}
                    if len(same) == 1:
                        pid, t = next(iter(same.items()))
                        found = (pid, f"{label}_team_name" + ("_partial" if k_i else ""), t)
                        break
                pids = {pid: t for pid, t in cand}
                if len(pids) == 1 and not team_name and delta == 0 and not r.is_foreign:
                    pid, t = next(iter(pids.items()))
                    found = (pid, f"{label}_name_only", t)
                    break
            if found:
                break
        ids.append(found[0] if found else None)
        methods.append(found[1] if found else None)
        teams.append(team_name or (found[2] if found else None))
    out = df.copy()
    out["kbo_player_id"] = pd.array(ids, dtype="Int64")
    out["id_match"] = methods
    out["team_from_records"] = [bool(t) and not (isinstance(o, str) and o) for t, o in zip(teams, df["team_name"])]
    out["team_name"] = teams
    return out


def profile_rows_2026(engine: Engine) -> pd.DataFrame:
    """KBO 선수 정보 페이지의 현재 연봉 → 2026 시즌. 현재 10개 구단이고 2026 기록·등록 명단에 있는 선수만."""
    sql = """
        SELECT p.player_id, p.player_name, p.team_name, p.position, p.salary_text, p.salary_manwon, p.scraped_at,
               EXISTS (SELECT 1 FROM raw.hitter_basic h WHERE h.season = 2026 AND h.player_id = p.player_id)
            OR EXISTS (SELECT 1 FROM raw.pitcher_basic q WHERE q.season = 2026 AND q.player_id = p.player_id)
            OR EXISTS (SELECT 1 FROM raw.defense_basic d WHERE d.season = 2026 AND d.player_id = p.player_id)
               AS played_2026
        FROM raw.player_profile p
        WHERE p.salary_text IS NOT NULL
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn)
    df["team"] = df["team_name"].map(PROFILE_TEAMS)
    df = df[df["team"].notna() & df["played_2026"]]
    recs = []
    for r in df.itertuples(index=False):
        usd = None
        m = re.match(r"^([\d,]+)\s*달러", r.salary_text or "")
        if m:
            usd = to_int(m.group(1))
        if r.salary_manwon is None and usd is None:
            continue
        recs.append({
            "season": 2026, "team_name": r.team, "player_name": norm_name(r.player_name),
            "player_name_raw": r.player_name, "position": r.position,
            "salary_manwon": int(r.salary_manwon) if pd.notna(r.salary_manwon) else None, "salary_usd": usd,
            "signing_bonus_usd": None, "is_foreign": usd is not None, "rank_in_table": None,
            "note": f"선수 정보 페이지 표기 '{r.salary_text}' ({r.scraped_at:%Y-%m-%d} 수집)",
            "source_type": "kbo_player_profile", "source_table": "KBO 선수 정보(현재 연봉)",
            "source_title": "KBO 선수 정보 > 선수 상세", "source_url":
                f"https://www.koreabaseball.com/Record/Player/{'Pitcher' if r.position == '투수' else 'Hitter'}Detail/Basic.aspx?playerId={r.player_id}",
            "source_file": None, "source_sheet": None, "kbo_player_id": int(r.player_id), "id_match": "profile",
            "team_from_records": False, "release_season": 2026,
        })
    return pd.DataFrame(recs)


def _value(row) -> int | None:
    v = row["salary_manwon"] if pd.notna(row["salary_manwon"]) else row["salary_usd"]
    return int(v) if pd.notna(v) else None


def _split_persons(g: pd.DataFrame) -> list[pd.DataFrame]:
    """같은 (시즌, 구단, 이름) 묶음을 사람 단위로 나눈다.
    한 표 안에 같은 이름이 서로 다른 금액으로 두 번 나오면(예: 2005 현대 전준호 둘, LG 이병규(9)/(7))
    서로 다른 선수로 보고 금액별로 나눈다. 아니면 한 사람이고, 표끼리 금액이 다르면 value_conflict 에 남긴다."""
    vals = g.apply(_value, axis=1)
    if vals.nunique(dropna=True) <= 1:
        return [g]
    grid_id = g["source_table"] + "|" + g["source_file"].fillna("") + "|" + g["source_sheet"].fillna("")
    tmp = g.assign(_v=vals, _grid=grid_id)
    multi = tmp.groupby("_grid")["_v"].nunique().max() > 1
    tags = {re.search(r"\(\d+\)", n).group(0) for n in g["player_name"] if re.search(r"\(\d+\)", n)}
    if not multi and len(tags) <= 1:
        return [g]
    return [x.drop(columns=["_v", "_grid"]) for _, x in tmp.groupby("_v", dropna=False)]


def finalize_players(df: pd.DataFrame) -> pd.DataFrame:
    """사람(시즌·구단·이름)마다 한 행. 우선순위가 높은 표(그 시즌 보도자료 먼저) 값을 쓰고,
    그 선수가 나온 표 이름을 sources 에 모은다."""
    df = df.copy()
    df["_prio"] = df["source_table"].map({s: i for i, s in enumerate(SOURCE_PRIORITY)}).fillna(99)
    df["_other_season"] = (df["release_season"] != df["season"]).astype(int)
    df["_key"] = df["player_name"].map(name_key)
    df["_team"] = df["team_name"].fillna("")
    df = df.sort_values(["season", "_team", "_key", "_prio", "_other_season", "release_season"])
    rows = []
    for (_, team, _k), g in df.groupby(["season", "_team", "_key"], sort=False):
        # 구단을 모르는 2000~2004 요약표의 동명이인(예: 2004 이승호 둘)은 금액으로 나눈다
        persons = [x for _, x in g.groupby("salary_manwon", dropna=False)] if not team else _split_persons(g)
        for gg in persons:
            best = gg.iloc[0].to_dict()
            tagged = [n for n in gg["player_name"] if re.search(r"\(\d+\)", n)]
            if tagged:
                best["player_name"] = tagged[0]
            best["sources"] = sorted(set(gg["source_table"]), key=lambda x: SOURCE_PRIORITY.index(x))
            if pd.isna(best["kbo_player_id"]):
                ids = gg["kbo_player_id"].dropna().unique()
                if len(ids) == 1:
                    best["kbo_player_id"] = ids[0]
                    best["id_match"] = gg.loc[gg["kbo_player_id"].notna(), "id_match"].iloc[0]
            if pd.isna(best.get("signing_bonus_usd")):
                b = gg["signing_bonus_usd"].dropna()
                best["signing_bonus_usd"] = int(b.iloc[0]) if len(b) else None
            if not best.get("position"):
                p = [x for x in gg["position"] if isinstance(x, str) and x]
                best["position"] = p[0] if p else None
            conflict = []
            vals_m = sorted({int(v) for v in gg["salary_manwon"].dropna()})
            vals_u = sorted({int(v) for v in gg["salary_usd"].dropna()})
            if len(vals_m) > 1:
                conflict.append("표마다 다른 값(만원): " + ", ".join(map(str, vals_m)))
            if len(vals_u) > 1:
                conflict.append("표마다 다른 값($): " + ", ".join(map(str, vals_u)))
            best["value_conflict"] = "; ".join(conflict) or None
            rows.append(best)
    out = pd.DataFrame(rows)
    out["name_seq"] = out.groupby(["season", out["team_name"].fillna(""), "player_name"]).cumcount() + 1
    out["collected_at"] = pd.Timestamp.now(tz="UTC")
    cols = ["season", "team_name", "player_name", "name_seq", "kbo_player_id", "position", "is_foreign",
            "salary_manwon", "salary_usd", "signing_bonus_usd", "note", "value_conflict", "source_type",
            "source_table", "sources", "source_title", "source_url", "source_file", "source_sheet", "rank_in_table",
            "player_name_raw", "id_match", "team_from_records", "collected_at"]
    out = out[cols].sort_values(["season", "team_name", "player_name"], na_position="last").reset_index(drop=True)
    for c in ("salary_manwon", "salary_usd", "signing_bonus_usd", "kbo_player_id", "rank_in_table"):
        out[c] = pd.array(out[c], dtype="Int64")
    return out


# ---------------------------------------------------------------- 적재

PLAYER_DDL = f"""
CREATE TABLE {SCHEMA}.{PLAYER_TABLE} (
    season            INTEGER NOT NULL,
    team_name         TEXT,              -- 구단 약칭 (기록 테이블과 같은 표기: LG, SSG, 키움 …)
    player_name       TEXT NOT NULL,
    name_seq          INTEGER NOT NULL DEFAULT 1,  -- 구단을 모르는 동명이인 구분용
    kbo_player_id     BIGINT,
    position          TEXT,
    is_foreign        BOOLEAN NOT NULL DEFAULT FALSE,
    salary_manwon     BIGINT,            -- 국내 선수 연봉 (만원)
    salary_usd        BIGINT,            -- 외국인 선수 연봉 (달러)
    signing_bonus_usd BIGINT,            -- 외국인 선수 계약금 (달러)
    note              TEXT,
    value_conflict    TEXT,              -- 표마다 값이 다를 때 다른 값들
    source_type       TEXT NOT NULL,     -- kbo_press_release | kbo_player_profile
    source_table      TEXT NOT NULL,     -- 값을 가져온 표 (억대 연봉 선수 현황, 구단별 선수연봉 상위 …)
    sources           TEXT[] NOT NULL,   -- 이 선수가 나온 모든 표
    source_title      TEXT,
    source_url        TEXT,
    source_file       TEXT,
    source_sheet      TEXT,
    rank_in_table     INTEGER,
    player_name_raw   TEXT,
    id_match          TEXT,              -- kbo_player_id 를 찾은 방법
    team_from_records BOOLEAN NOT NULL DEFAULT FALSE,  -- 원문에 구단이 없어 기록 테이블로 채웠는지
    collected_at      TIMESTAMPTZ NOT NULL
)
"""

TEAM_DDL = f"""
CREATE TABLE {SCHEMA}.{TEAM_TABLE} (
    season         INTEGER NOT NULL,
    team_name      TEXT NOT NULL,     -- '합계' = 리그 전체
    basis          TEXT NOT NULL,     -- registered | top_entry | top40_cbt
    n_players      INTEGER,
    total_manwon   BIGINT,
    avg_manwon     BIGINT,
    note           TEXT,
    source_type    TEXT NOT NULL DEFAULT 'kbo_press_release',
    source_title   TEXT,
    source_url     TEXT,
    source_file    TEXT,
    source_sheet   TEXT,
    release_season INTEGER,
    collected_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (season, team_name, basis)
)
"""


def load(engine: Engine, players: pd.DataFrame, teams: pd.DataFrame) -> None:
    """전체 교체. 한 트랜잭션이라 실패하면 예전 데이터가 남는다."""
    teams = teams.copy()
    teams["collected_at"] = pd.Timestamp.now(tz="UTC")
    teams["source_type"] = "kbo_press_release"
    for c in ("n_players", "total_manwon", "avg_manwon", "release_season"):
        teams[c] = pd.array(teams[c], dtype="Int64")
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{PLAYER_TABLE}"))
        conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{TEAM_TABLE}"))
        conn.execute(text(PLAYER_DDL))
        conn.execute(text(TEAM_DDL))
        players.to_sql(PLAYER_TABLE, conn, schema=SCHEMA, index=False, if_exists="append", method="multi",
                       chunksize=500, dtype={"sources": ARRAY(TEXT)})
        teams.to_sql(TEAM_TABLE, conn, schema=SCHEMA, index=False, if_exists="append", method="multi")
        conn.execute(text(
            f"CREATE UNIQUE INDEX ON {SCHEMA}.{PLAYER_TABLE} (season, coalesce(team_name, ''), player_name, name_seq)"))
        conn.execute(text(f"CREATE INDEX ON {SCHEMA}.{PLAYER_TABLE} (kbo_player_id, season)"))
        conn.execute(text(
            f"COMMENT ON TABLE {SCHEMA}.{PLAYER_TABLE} IS 'KBO 선수 연봉 2000~2026. 출처: KBO 보도자료(소속선수 등록·연봉 현황 첨부표) "
            "+ KBO 선수 정보 페이지(2026 현재 연봉). 2005~2021 은 억대 연봉 전체 명단, 2000~2004 는 억대 요약표, "
            "2022~2025 는 상위 선수·외국인만. 국내 만원, 외국인 달러.'"))
        conn.execute(text(
            f"COMMENT ON TABLE {SCHEMA}.{TEAM_TABLE} IS 'KBO 구단별 연봉 총액·평균. 출처: KBO 보도자료. basis: "
            "registered=신인·외국인 제외 소속선수 전체, top_entry=1군 엔트리 기준 상위 27~29명, top40_cbt=경쟁균형세 상위 40명 합계. 단위 만원.'"))


def coverage(players: pd.DataFrame, teams: pd.DataFrame) -> pd.DataFrame:
    p = players.groupby("season").agg(
        players=("player_name", "size"), domestic=("salary_manwon", "count"), foreign=("salary_usd", "count"),
        with_id=("kbo_player_id", "count"),
        min_manwon=("salary_manwon", "min"), max_manwon=("salary_manwon", "max"),
        main_source=("source_table", lambda s: s.value_counts().index[0]))
    t = teams[teams["team_name"] != "합계"].groupby("season")["basis"].agg(lambda s: ",".join(sorted(set(s))))
    return p.join(t.rename("team_basis"), how="outer").reindex(list(SEASONS))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="KBO 선수 연봉 수집 (보도자료 + 선수 정보)")
    ap.add_argument("--offline", action="store_true", help="저장된 보도자료·첨부만 다시 파싱")
    ap.add_argument("--csv", type=Path, help="선수 연봉 결과를 CSV 로도 저장할 경로")
    args = ap.parse_args(argv)

    releases = fetch_releases(args.offline)
    grids = load_grids(releases)
    engine = get_engine()
    raw_players = match_player_ids(engine, build_player_rows(grids))
    prof = profile_rows_2026(engine)
    players = finalize_players(pd.concat([raw_players, prof], ignore_index=True))
    teams = build_team_rows(grids)
    load(engine, players, teams)
    cov = coverage(players, teams)
    print(cov.to_string())
    print(f"raw.{PLAYER_TABLE}: {len(players)}행, raw.{TEAM_TABLE}: {len(teams)}행")
    if args.csv:
        players.to_csv(args.csv, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
