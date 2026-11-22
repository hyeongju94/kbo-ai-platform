"""KBO FA 계약 데이터셋 (2000~2026) → raw.fa_contracts.

출처: 나무위키 'KBO 리그/역대 FA' 문서(전체 연도 요약 표)와 연도별 하위 문서
'KBO 리그/역대 FA/<연도>'(2017년부터, 계약금·연봉·옵션 세부 내역).

연도(fa_year)는 나무위키 표기를 따른다. 예: fa_year=2025 는 2024 시즌이 끝난 뒤
(2024년 11월~2025년 초) 맺은 계약, 즉 2025 시즌부터 적용되는 계약이다.

금액은 모두 '원' 단위 정수로 바꿔 저장하고, 원문 표기도 *_text 열에 그대로 남긴다.
표에서 확인되지 않는 값은 비워 둔다(NULL). 지어내지 않는다.

실행:
    uv run python -m kbo_ai_platform.ingestion.fa_contracts            # 수집 + 적재
    uv run python -m kbo_ai_platform.ingestion.fa_contracts --offline  # 저장된 HTML만 다시 파싱
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import quote

import lxml.html
import pandas as pd
import requests
from sqlalchemy import Engine, text

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine

SCHEMA = "raw"
TABLE = "fa_contracts"
BASE_DOC = "KBO 리그/역대 FA"
YEAR_DOCS = range(2017, 2027)  # 연도별 하위 문서가 있는 해
CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "namu_fa"
REQUEST_GAP_SEC = 6  # 사이트 부담을 줄이기 위한 요청 간격
HEADERS = {"User-Agent": "Mozilla/5.0 (kbo-ai-platform portfolio data collection)"}

TEAM_ALIASES = {
    "kt": "KT", "KT 위즈": "KT", "kt wiz": "KT", "KT wiz": "KT",
    "SK": "SK", "SK 와이번스": "SK", "SSG 랜더스": "SSG",
    "두산 베어스": "두산", "LG 트윈스": "LG", "삼성 라이온즈": "삼성",
    "롯데 자이언츠": "롯데", "한화 이글스": "한화", "NC 다이노스": "NC",
    "KIA 타이거즈": "KIA", "기아": "KIA", "해태 타이거즈": "해태",
    "키움 히어로즈": "키움", "넥센 히어로즈": "넥센", "우리 히어로즈": "우리",
    "현대 유니콘스": "현대", "히어로즈": "히어로즈",
}


# ---------------------------------------------------------------- 가져오기


def doc_url(title: str) -> str:
    return "https://namu.wiki/w/" + quote(title)


def fetch_docs(offline: bool) -> dict[str, Path]:
    """문서 HTML을 캐시 폴더에 저장하고 {문서제목: 파일경로}를 돌려준다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    titles = [BASE_DOC] + [f"{BASE_DOC}/{y}" for y in YEAR_DOCS]
    paths: dict[str, Path] = {}
    for i, title in enumerate(titles):
        path = CACHE_DIR / (title.replace("/", "__").replace(" ", "_") + ".html")
        if not offline:
            if i:
                time.sleep(REQUEST_GAP_SEC)
            resp = requests.get(doc_url(title), headers=HEADERS, timeout=30)
            resp.raise_for_status()
            path.write_bytes(resp.content)
            print(f"받음: {title} ({len(resp.content):,} bytes)")
        if path.exists():
            paths[title] = path
    return paths


# ---------------------------------------------------------------- 표 해석


def cell_text(td) -> str:
    for junk in td.xpath(".//noscript|.//style"):
        junk.drop_tree()
    txt = re.sub(r"\s+", " ", td.text_content()).strip()
    txt = re.sub(r"\[\d+\]", "", txt).strip()  # 각주 번호 제거
    if not txt:  # 팀 로고만 있는 칸: 링크 title 로 팀명을 얻는다
        titles = td.xpath(".//a/@title")
        if titles:
            txt = titles[0]
    return txt


def table_grid(table) -> list[list[str]]:
    """rowspan/colspan 을 펼친 2차원 표."""
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(table.xpath("./tbody/tr|./tr|./thead/tr")):
        row: list[str] = []
        c = 0
        cells = tr.xpath("./td|./th")
        idx = 0
        while idx < len(cells) or (r, c) in pending:
            if (r, c) in pending:
                row.append(pending.pop((r, c)))
                c += 1
                continue
            td = cells[idx]
            idx += 1
            val = cell_text(td)
            rs = int(td.get("rowspan", "1") or 1)
            cs = int(td.get("colspan", "1") or 1)
            for dc in range(cs):
                row.append(val)
                for dr in range(1, rs):
                    pending[(r + dr, c + dc)] = val
                c += 1
        grid.append(row)
    return grid


# 헤더 글자 → 표준 열 이름 (앞에서부터 먼저 맞는 것)
HEADER_RULES = [
    ("fa_year", lambda h: h == "연도"),
    ("player_name", lambda h: h in ("이름", "선수")),
    ("orig_team", lambda h: h in ("원 소속", "원소속팀", "원 소속팀", "소속팀", "소속")),
    ("new_team", lambda h: h in ("계약 구단", "계약팀", "이적팀")),
    ("contract_years_text", lambda h: h in ("계약기간", "계약 기간")),
    ("total_text", lambda h: h == "총액" or h.startswith("계약 규모") or h == "계약 결과"),
    ("signing_bonus_text", lambda h: h == "계약금"),
    ("salary_total_text", lambda h: h in ("총연봉", "연봉 총액")),
    ("annual_salary_text", lambda h: h == "연봉"),
    ("option_text", lambda h: h in ("옵션", "옵션 총액")),
    ("announced_text", lambda h: h in ("발표", "계약일시")),
    ("move_type", lambda h: h in ("이적 여부", "결과")),
    ("compensation_player", lambda h: h == "보상선수"),
    ("compensation_money_text", lambda h: h == "보상금"),
    ("grade", lambda h: h == "등급"),
    ("prev_salary_text", lambda h: re.fullmatch(r"\d{4}년 연봉.*", h) is not None),
]

SKIP_TABLE_WORDS = ("비FA", "FA 외 계약", "최근 3시즌", "구단별", "은퇴", "타임라인", "sWAR당", "평가", "인정년수")
NAME_RE = re.compile(r"^[가-힣]{2,5}$|^[A-Za-z][A-Za-z .'-]+$")


def map_header(header: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for i, h in enumerate(header):
        h = re.sub(r"\(.*?\)", "", h).strip()
        for col, rule in HEADER_RULES:
            if col not in used and rule(h):
                mapping[i] = col
                used.add(col)
                break
    return mapping


def extract_rows(path: Path, title: str) -> list[dict]:
    doc = lxml.html.parse(str(path)).getroot()
    page_year = int(title.rsplit("/", 1)[1]) if title != BASE_DOC else None
    out: list[dict] = []
    heads = {"h2": "", "h3": "", "h4": ""}
    section = ""
    for el in doc.iter("h2", "h3", "h4", "table"):
        if el.tag != "table":
            heads[el.tag] = re.sub(r"\[편집\]$", "", el.text_content().strip())
            if el.tag == "h2":
                heads["h3"] = heads["h4"] = ""
            elif el.tag == "h3":
                heads["h4"] = ""
            section = " > ".join(h for h in heads.values() if h)
            continue
        if el.xpath("ancestor::table"):
            continue
        grid = table_grid(el)
        # 머리글 행: '이름'/'선수' 가 들어 있고 금액 열도 있는 첫 행
        head_idx = None
        for i, row in enumerate(grid[:3]):
            m = map_header(row)
            if "player_name" in m.values() and "total_text" in m.values():
                head_idx = i
                break
        if head_idx is None:
            continue
        caption = " ".join(" ".join(r) for r in grid[: head_idx + 1])
        if any(w in caption for w in SKIP_TABLE_WORDS) or any(w in section for w in SKIP_TABLE_WORDS):
            continue
        mapping = map_header(grid[head_idx])
        table_label = (grid[0][0] if head_idx > 0 else "") or section
        for row in grid[head_idx + 1 :]:
            rec = {col: row[i] for i, col in mapping.items() if i < len(row)}
            name = re.sub(r"\(.*?\)", "", rec.get("player_name", "")).strip()
            if not NAME_RE.match(name) or name in ("없음", "합계"):
                continue
            rec["player_name"] = name
            year = page_year
            if year is None:
                m = re.match(r"(\d{4})", rec.get("fa_year", ""))
                if not m:
                    continue
                year = int(m.group(1))
            rec["fa_year"] = year
            rec["source_url"] = doc_url(title)
            label = section if table_label in ("", section) else f"{section} / {table_label}"
            rec["source_section"] = label[:200]
            out.append(rec)
    return out


# ---------------------------------------------------------------- 값 정리


def parse_krw(s: str | None) -> int | None:
    """'4년 50억', '2억 5,000만 원', '4200만', '0.75억' → 원 단위 정수. 해석 불가면 None."""
    if not s:
        return None
    s = s.replace(",", "").replace(" ", "")
    s = re.sub(r"^\d+(\+\d+)*년", "", s)  # 앞의 계약기간 제거
    s = re.sub(r"\(.*$", "", s)  # 괄호 안 부연 제거 (보장액 등)
    s = s.replace("천만", "000만")
    if any(w in s for w in ("달러", "불", "엔", "비공개", "연간")):
        return None
    m = re.match(r"^(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)천)?(?:(\d+(?:\.\d+)?)만)?(?:원)?$", s)
    if not m or not any(m.groups()):
        return None
    eok, cheon, man = (float(g) if g else 0.0 for g in m.groups())
    return int(round(eok * 1e8 + cheon * 1e7 + man * 1e4))


def parse_foreign(s: str | None) -> tuple[int | None, str | None]:
    """해외 계약 '5억 엔', '400만 달러', '310만불' → (금액, 'JPY'|'USD')."""
    if not s:
        return None, None
    t = s.replace(",", "").replace(" ", "").replace("천만", "000만")
    cur = "JPY" if "엔" in t else ("USD" if ("달러" in t or "불" in t) else None)
    if not cur:
        return None, None
    m = re.match(r"^(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)만)?", re.sub(r"^\d+년", "", t))
    if not m or not any(m.groups()):
        return None, cur
    eok, man = (float(g) if g else 0.0 for g in m.groups())
    return int(round(eok * 1e8 + man * 1e4)), cur


def parse_years(s: str | None) -> float | None:
    """'4년' → 4, '2+2년' → 4, '3+1년 14억' → 4. 해석 불가면 None."""
    if not s:
        return None
    m = re.match(r"^\s*(\d+(?:\+\d+)*)\s*년", s)
    if not m:
        return None
    return float(sum(int(x) for x in m.group(1).split("+")))


def parse_date(s: str | None) -> pd.Timestamp | None:
    if not s:
        return None
    m = re.search(r"(\d{4})\s*[년.]\s*(\d{1,2})\s*[월.]\s*(\d{1,2})", s)
    if not m:
        return None
    return pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def norm_team(s: str | None) -> str | None:
    if not s or s in ("-", ""):
        return None
    s = s.strip()
    return TEAM_ALIASES.get(s, s)


def norm_move(rec: dict) -> str | None:
    mv = rec.get("move_type") or ""
    for key in ("잔류", "이적"):
        if key in mv:
            return key
    sec = rec.get("source_section", "")
    if "해외" in sec:
        return "해외"
    if "이적" in sec:
        return "이적"
    if "잔류" in sec:
        return "잔류"
    o, n = rec.get("orig_team"), rec.get("new_team")
    if o and n:
        return "잔류" if o == n else "이적"
    return None


FIELDS = [
    "orig_team", "new_team", "contract_years_text", "total_text", "signing_bonus_text",
    "salary_total_text", "annual_salary_text", "option_text", "announced_text",
    "move_type", "compensation_player", "compensation_money_text", "grade", "prev_salary_text",
]


def build_dataset(paths: dict[str, Path]) -> pd.DataFrame:
    raw: list[dict] = []
    # 세부 내역이 많은 연도별 문서를 먼저 읽어 우선권을 준다
    for title in sorted(paths, key=lambda t: t == BASE_DOC):
        raw.extend(extract_rows(paths[title], title))

    merged: dict[tuple[int, str], dict] = {}
    for rec in raw:
        if rec.get("total_text") and not rec.get("contract_years_text"):
            m = re.match(r"^\s*(\d+(?:\+\d+)*년)", rec["total_text"])
            if m:
                rec["contract_years_text"] = m.group(1)
        key = (rec["fa_year"], rec["player_name"])
        cur = merged.setdefault(key, {"fa_year": key[0], "player_name": key[1], "sources": []})
        for f in FIELDS:
            v = (rec.get(f) or "").strip()
            if v and v not in ("-", "?") and not cur.get(f):
                cur[f] = v
        src = (rec["source_url"], rec["source_section"])
        if src not in cur["sources"]:
            cur["sources"].append(src)
        cur.setdefault("_sections", []).append(rec["source_section"])

    rows = []
    for cur in merged.values():
        cur["source_section"] = " | ".join(dict.fromkeys(s for _, s in cur["sources"]))
        orig = norm_team(cur.get("orig_team"))
        new = norm_team(cur.get("new_team"))
        cur["orig_team"], cur["new_team"] = orig, new
        move = norm_move(cur)
        if move == "잔류" and not new:
            new = orig
        pos = None
        secs = cur["source_section"]
        if "투수" in secs:
            pos = "투수"
        elif "야수" in secs:
            pos = "야수"
        fa_type = "퓨처스FA" if "퓨처스" in secs else ("해외" if move == "해외" else "FA")
        rows.append(
            {
                "fa_year": cur["fa_year"],
                "fa_type": fa_type,
                "player_name": cur["player_name"],
                "position_group": pos,
                "orig_team": orig,
                "new_team": new,
                "move_type": move,
                "grade": cur.get("grade") if cur.get("grade") in ("A", "B", "C") else None,
                "contract_years": parse_years(cur.get("contract_years_text")),
                "total_krw": parse_krw(cur.get("total_text")),
                "total_foreign_amount": parse_foreign(cur.get("total_text"))[0],
                "total_currency": parse_foreign(cur.get("total_text"))[1] or ("KRW" if parse_krw(cur.get("total_text")) else None),
                "signing_bonus_krw": parse_krw(cur.get("signing_bonus_text")),
                "salary_total_krw": parse_krw(cur.get("salary_total_text")),
                "annual_salary_krw": parse_krw(cur.get("annual_salary_text")),
                "option_krw": parse_krw(cur.get("option_text")),
                "announced_date": parse_date(cur.get("announced_text")),
                "compensation_player": cur.get("compensation_player"),
                "compensation_money_krw": parse_krw(cur.get("compensation_money_text")),
                "prev_salary_krw": parse_krw(cur.get("prev_salary_text")),
                "contract_years_text": cur.get("contract_years_text"),
                "total_text": cur.get("total_text"),
                "signing_bonus_text": cur.get("signing_bonus_text"),
                "salary_total_text": cur.get("salary_total_text"),
                "annual_salary_text": cur.get("annual_salary_text"),
                "option_text": cur.get("option_text"),
                "kbo_player_id": None,
                "source_url": " | ".join(dict.fromkeys(u for u, _ in cur["sources"])),
                "source_section": cur["source_section"][:500],
            }
        )
    df = pd.DataFrame(rows).sort_values(["fa_year", "announced_date", "player_name"], na_position="last")
    df = df[df["fa_year"].between(2000, 2026)]
    # 계약 내용도 계약 팀도 없는 행(미계약·은퇴 선수)은 계약 데이터가 아니므로 뺀다
    df = df[df["total_text"].notna() | df["new_team"].notna()].reset_index(drop=True)
    for col in [c for c in df.columns if c.endswith("_krw")] + ["total_foreign_amount"]:
        df[col] = df[col].astype("Int64")
    df["scraped_at"] = pd.Timestamp.now(tz="Asia/Seoul")
    return df


# ---------------------------------------------------------------- KBO playerId 매칭


def match_player_ids(engine: Engine, df: pd.DataFrame) -> pd.DataFrame:
    """FA 직전 시즌(fa_year-1) 기록 테이블에서 이름이 한 명만 맞으면 player_id 를 붙인다.

    다른 작업이 채우는 raw.hitter_basic / raw.pitcher_basic 을 읽기만 한다.
    동명이인이 있으면 팀명까지 맞춰 보고, 그래도 여럿이면 비워 둔다.
    """
    frames = []
    with engine.connect() as conn:
        for tbl in ("hitter_basic", "pitcher_basic"):
            exists = conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f"{SCHEMA}.{tbl}"}
            ).scalar()
            if not exists:
                continue
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=:s AND table_name=:t"), {"s": SCHEMA, "t": tbl})}
            name_col = next((c for c in ("player_name", "선수명", "name") if c in cols), None)
            team_col = next((c for c in ("team", "team_name", "팀명") if c in cols), None)
            if not name_col or "player_id" not in cols:
                continue
            team_sel = f'"{team_col}"' if team_col else "NULL"
            frames.append(pd.read_sql(text(
                f'SELECT DISTINCT season, "{name_col}" AS name, {team_sel} AS team, player_id '
                f'FROM {SCHEMA}."{tbl}"'), conn))
    if not frames:
        print("선수 기록 테이블이 아직 없어 playerId 매칭은 건너뜀")
        return df
    ref = pd.concat(frames).drop_duplicates(["season", "name", "player_id"])
    ids = []
    for _, r in df.iterrows():
        cand = ref[(ref["name"] == r["player_name"]) & ref["season"].between(r["fa_year"] - 2, r["fa_year"] - 1)]
        pids = cand["player_id"].astype(str).unique()
        if len(pids) > 1 and r["orig_team"]:
            sub = cand[cand["team"].astype(str).str.contains(str(r["orig_team"]), na=False)]
            pids = sub["player_id"].astype(str).unique()
        ids.append(pids[0] if len(pids) == 1 else None)
    df = df.copy()
    df["kbo_player_id"] = ids
    print(f"playerId 매칭: {df['kbo_player_id'].notna().sum()} / {len(df)}")
    return df


# ---------------------------------------------------------------- 적재


def load(engine: Engine, df: pd.DataFrame) -> None:
    """전체 교체 방식. 한 트랜잭션이라 실패하면 예전 데이터가 남는다."""
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{TABLE}"))
        df.to_sql(TABLE, conn, schema=SCHEMA, index=False, method="multi")
        conn.execute(text(f"ALTER TABLE {SCHEMA}.{TABLE} ADD PRIMARY KEY (fa_year, player_name)"))
        conn.execute(text(
            f"COMMENT ON TABLE {SCHEMA}.{TABLE} IS "
            "'KBO FA 계약 2000~2026. 출처: 나무위키 KBO 리그/역대 FA (+연도별 문서). "
            "금액 단위 원. fa_year=계약이 적용되는 첫 시즌. 확인 안 된 값은 NULL.'"
        ))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="KBO FA 계약 데이터 수집")
    ap.add_argument("--offline", action="store_true", help="저장된 HTML 만 다시 파싱")
    ap.add_argument("--csv", type=Path, help="결과를 CSV 로도 저장할 경로")
    args = ap.parse_args(argv)

    paths = fetch_docs(args.offline)
    df = build_dataset(paths)
    engine = get_engine()
    df = match_player_ids(engine, df)
    load(engine, df)
    print(f"raw.{TABLE}: {len(df)}행 적재 ({df['fa_year'].min()}~{df['fa_year'].max()})")
    if args.csv:
        df.to_csv(args.csv, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
