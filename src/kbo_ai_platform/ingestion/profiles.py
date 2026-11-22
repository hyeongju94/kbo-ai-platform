"""선수 기본정보(생년월일·포지션·투타·키·몸무게·연봉·입단연도 등) 수집.

선수 상세 페이지는 시즌 선택 같은 포스트백이 필요 없는 일반 페이지라서 requests로 가볍게 받는다.
대상: raw 스키마 선수 기록 테이블에 나온 모든 playerId 중 아직 정보가 없는 선수.
주의: 연봉·소속은 '지금' 페이지에 보이는 최신 값이다(시즌별 연봉 이력이 아님).
"""

import logging
import re
import time
from datetime import date, datetime

import requests
from lxml import html as lxml_html
from sqlalchemy import Engine, inspect, text

from kbo_ai_platform.db.connection import PROJECT_ROOT
from kbo_ai_platform.ingestion.scrapers.record_table import BASE_URL

log = logging.getLogger(__name__)

TABLE = "player_profile"
HEADERS = {"User-Agent": "Mozilla/5.0 (kbo-ai-platform portfolio project)"}
FIELDS = {  # 페이지의 span id 끝부분 → 원문 열 이름
    "Name": "player_name",
    "BackNo": "back_no",
    "Birthday": "birthday_text",
    "Position": "position_text",
    "HeightWeight": "height_weight_text",
    "Career": "career",
    "Payment": "signing_bonus_text",
    "Salary": "salary_text",
    "Draft": "draft",
    "JoinInfo": "join_info",
}

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS raw.{TABLE} (
    player_id          BIGINT PRIMARY KEY,
    player_name        TEXT,
    team_name          TEXT,      -- 상세 페이지 상단의 현재(마지막) 소속
    back_no            TEXT,
    birthday           DATE,
    position           TEXT,      -- 투수, 포수, 내야수, 외야수
    throws_bats        TEXT,      -- 예: 우투좌타
    height_cm          INTEGER,
    weight_kg          INTEGER,
    career             TEXT,      -- 출신 학교·팀
    signing_bonus_text TEXT,
    salary_text        TEXT,
    salary_manwon      BIGINT,    -- 연봉(만원), 숫자로 읽을 수 있을 때만
    draft              TEXT,
    join_info          TEXT,      -- 예: 12삼성
    join_year          INTEGER,   -- join_info 앞 두 자리로 만든 입단 연도
    birthday_text      TEXT,
    position_text      TEXT,
    height_weight_text TEXT,
    scraped_at         TIMESTAMPTZ
)
"""

PLAYER_TABLES = ["hitter_basic", "hitter_extra", "runner_basic", "defense_basic", "pitcher_basic", "pitcher_extra"]
PITCHER_TABLES = {"pitcher_basic", "pitcher_extra"}


def _to_int(pattern: str, s: str | None) -> int | None:
    m = re.search(pattern, s or "")
    return int(m.group(1)) if m else None


def parse_profile(page_html: str, player_id: int) -> dict:
    doc = lxml_html.fromstring(page_html)
    row: dict = {"player_id": player_id}
    for suffix, col in FIELDS.items():
        found = doc.xpath(f"//span[contains(@id, 'playerProfile_lbl{suffix}')]")
        row[col] = found[0].text_content().strip() or None if found else None
    if not row["player_name"]:
        raise ValueError("선수 정보가 없는 페이지입니다")
    team = doc.xpath("//h4[@id='h4Team']")
    row["team_name"] = team[0].text_content().strip() or None if team else None

    b = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", row["birthday_text"] or "")
    row["birthday"] = date(int(b.group(1)), int(b.group(2)), int(b.group(3))) if b else None
    pos = re.match(r"\s*([^()]*)\(([^)]*)\)", row["position_text"] or "")
    row["position"] = (pos.group(1).strip() or None) if pos else row["position_text"]
    row["throws_bats"] = pos.group(2).strip() if pos else None
    row["height_cm"] = _to_int(r"(\d+)\s*cm", row["height_weight_text"])
    row["weight_kg"] = _to_int(r"(\d+)\s*kg", row["height_weight_text"])
    row["salary_manwon"] = _to_int(r"^(\d+)\s*만원", (row["salary_text"] or "").replace(",", ""))
    yy = _to_int(r"^(\d{2})", row["join_info"])
    row["join_year"] = None if yy is None else (1900 + yy if yy >= 80 else 2000 + yy)
    row["scraped_at"] = datetime.now().astimezone()
    return row


def _targets(conn, refresh: bool) -> list[tuple[int, bool]]:
    """(player_id, 투수 기록에만 나오는지) 목록."""
    existing = set(inspect(conn).get_table_names(schema="raw"))
    hitters: set[int] = set()
    pitchers: set[int] = set()
    for t in PLAYER_TABLES:
        if t in existing:
            ids = {r[0] for r in conn.execute(text(f"SELECT DISTINCT player_id FROM raw.{t}"))}
            (pitchers if t in PITCHER_TABLES else hitters).update(ids)
    done = set()
    if not refresh:
        done = {r[0] for r in conn.execute(text(f"SELECT player_id FROM raw.{TABLE}"))}
    ids = sorted((hitters | pitchers) - done)
    return [(pid, pid not in hitters) for pid in ids]


def _upsert(conn, rows: list[dict]) -> None:
    cols = list(rows[0])
    conn.execute(
        text(f"DELETE FROM raw.{TABLE} WHERE player_id = ANY(:ids)"),
        {"ids": [r["player_id"] for r in rows]},
    )
    conn.execute(
        text(f"INSERT INTO raw.{TABLE} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
        rows,
    )


def collect_profiles(engine: Engine, delay: float = 1.0, refresh: bool = False, batch: int = 50) -> int:
    with engine.begin() as conn:
        conn.execute(text(CREATE_SQL))
        targets = _targets(conn, refresh)
    log.info("선수 정보 수집 대상: %d명", len(targets))

    backup = PROJECT_ROOT / "data" / "raw" / "kbo" / "profile"
    backup.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    rows: list[dict] = []
    saved, failed = 0, []
    for i, (pid, pitcher_only) in enumerate(targets, 1):
        kind = "PitcherDetail" if pitcher_only else "HitterDetail"
        url = f"{BASE_URL}/Record/Player/{kind}/Basic.aspx?playerId={pid}"
        try:
            res = session.get(url, timeout=20)
            res.raise_for_status()
            (backup / f"{pid}.html").write_text(res.text, encoding="utf-8")
            rows.append(parse_profile(res.text, pid))
        except Exception as e:
            log.warning("playerId %d 실패: %s", pid, e)
            failed.append(pid)
        if len(rows) >= batch or (i == len(targets) and rows):
            with engine.begin() as conn:
                _upsert(conn, rows)
            saved += len(rows)
            rows = []
            log.info("선수 정보 %d/%d 저장", saved, len(targets))
        time.sleep(delay)
    if failed:
        log.warning("실패한 playerId %d개: %s", len(failed), failed[:50])
    return saved
