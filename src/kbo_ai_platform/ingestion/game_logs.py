"""선수 경기별(게임로그) 기록 수집: KBO 기록실 '일자별 기록' 페이지.

타자: /Record/Player/HitterDetail/Daily.aspx?playerId=...
투수: /Record/Player/PitcherDetail/Daily.aspx?playerId=...

페이지는 ASP.NET 폼이라 시즌·시리즈를 바꾸려면 포스트백이 필요하지만, 선택 상자 두 개뿐이라
브라우저 없이 requests로 GET(폼 값 받기) → POST(시즌·정규시즌 선택) 두 번이면 된다.
표는 월별로 나뉘어 있고 날짜는 'MM.DD' 형태라 시즌 연도를 붙여 날짜로 만든다.

대상: raw.hitter_basic·runner_basic(타자) / raw.pitcher_basic(투수)에 나온 (시즌, playerId).
재시작: 끝난 (종류, 시즌, 시리즈, playerId)는 raw.game_log_progress 에 기록하고 다음 실행에서 건너뛴다.
기록이 0경기인 선수도 progress에 남기므로 다시 요청하지 않는다. --refresh 로 다시 받을 수 있다.
사이트의 시즌 선택은 2001년부터라 2000 시즌은 받을 수 없다.

예) uv run python -m kbo_ai_platform.ingestion.game_logs --season 2001-2026 --type all
"""

import argparse
import gzip
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import requests
from lxml import html as lxml_html
from sqlalchemy import Engine, inspect, text

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine
from kbo_ai_platform.ingestion.scrapers.record_table import BASE_URL

log = logging.getLogger("kbo_game_logs")

HEADERS = {"User-Agent": "Mozilla/5.0 (kbo-ai-platform portfolio project)"}
FORM_PREFIX = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$"
SERIES_REGULAR = "0"
FIRST_SEASON = 2001  # 일자별 기록 페이지의 시즌 선택 상자는 2001년부터 있다
PROGRESS_TABLE = "game_log_progress"


@dataclass(frozen=True)
class LogSpec:
    table: str
    detail: str               # URL 경로의 HitterDetail / PitcherDetail
    source_tables: tuple[str, ...]  # 대상 선수를 고를 raw 테이블
    columns: dict[str, tuple[str, str]]  # 화면 머리글 → (열 이름, SQL 타입)


HITTER_COLUMNS = {
    "AVG1": ("game_avg", "DOUBLE PRECISION"),   # 그 경기 타율
    "PA": ("pa", "INTEGER"),
    "AB": ("ab", "INTEGER"),
    "R": ("r", "INTEGER"),
    "H": ("h", "INTEGER"),
    "2B": ("doubles", "INTEGER"),
    "3B": ("triples", "INTEGER"),
    "HR": ("hr", "INTEGER"),
    "RBI": ("rbi", "INTEGER"),
    "SB": ("sb", "INTEGER"),
    "CS": ("cs", "INTEGER"),
    "BB": ("bb", "INTEGER"),
    "HBP": ("hbp", "INTEGER"),
    "SO": ("so", "INTEGER"),
    "GDP": ("gdp", "INTEGER"),
    "AVG2": ("season_avg", "DOUBLE PRECISION"),  # 그 경기까지 시즌 누적 타율
}
PITCHER_COLUMNS = {
    "구분": ("role", "TEXT"),        # 선발 / 구원
    "결과": ("decision", "TEXT"),    # 승 / 패 / 세 / 홀 / 빈칸
    "ERA1": ("game_era", "DOUBLE PRECISION"),
    "TBF": ("tbf", "INTEGER"),
    "IP": ("ip_text", "TEXT"),       # 예: '5 2/3'
    "H": ("h", "INTEGER"),
    "HR": ("hr", "INTEGER"),
    "BB": ("bb", "INTEGER"),
    "HBP": ("hbp", "INTEGER"),
    "SO": ("so", "INTEGER"),
    "R": ("r", "INTEGER"),
    "ER": ("er", "INTEGER"),
    "ERA2": ("season_era", "DOUBLE PRECISION"),  # 그 경기까지 시즌 누적 ERA
}

SPECS = {
    "hitter": LogSpec("hitter_game_log", "HitterDetail", ("hitter_basic", "runner_basic"), HITTER_COLUMNS),
    "pitcher": LogSpec("pitcher_game_log", "PitcherDetail", ("pitcher_basic",), PITCHER_COLUMNS),
}


def _create_sql(spec: LogSpec) -> str:
    stat_cols = ",\n    ".join(f"{name} {sqltype}" for name, sqltype in spec.columns.values())
    extra = "ip_outs INTEGER,  -- ip_text를 아웃카운트로 바꾼 값 (5 2/3 → 17)\n    " if spec.table.startswith("pitcher") else ""
    return f"""
CREATE TABLE IF NOT EXISTS raw.{spec.table} (
    season      INTEGER NOT NULL,
    series      TEXT    NOT NULL,   -- '0' = KBO 정규시즌
    player_id   BIGINT  NOT NULL,
    game_date   DATE    NOT NULL,
    game_no     INTEGER NOT NULL,   -- 같은 날 두 번째 경기(더블헤더)면 2
    opponent    TEXT,               -- 상대 팀 (당시 이름)
    {stat_cols},
    {extra}extra       JSONB,              -- 옛 시즌 등에서 위 목록에 없는 항목이 나오면 원문 그대로
    scraped_at  TIMESTAMPTZ,
    PRIMARY KEY (season, series, player_id, game_date, game_no)
)
"""


PROGRESS_SQL = f"""
CREATE TABLE IF NOT EXISTS raw.{PROGRESS_TABLE} (
    record_type TEXT    NOT NULL,   -- hitter / pitcher
    season      INTEGER NOT NULL,
    series      TEXT    NOT NULL,
    player_id   BIGINT  NOT NULL,
    n_games     INTEGER NOT NULL,   -- 받은 경기 수
    expected_g  INTEGER,            -- 시즌 기본기록의 경기 수 (비교용)
    scraped_at  TIMESTAMPTZ,
    PRIMARY KEY (record_type, season, series, player_id)
)
"""


def ip_to_outs(ip: str | None) -> int | None:
    """'5 2/3' → 17, '1/3' → 1, '7' → 21."""
    if not ip:
        return None
    m = re.fullmatch(r"\s*(?:(\d+))?\s*(?:(\d)/3)?\s*", ip)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 3 + int(m.group(2) or 0)


def _value(s: str, sqltype: str):
    s = s.strip()
    if s in ("", "-"):
        return None
    if sqltype == "INTEGER":
        return int(s.replace(",", ""))
    if sqltype == "DOUBLE PRECISION":
        try:
            return float(s)
        except ValueError:  # 예: 무한대 ERA
            return None
    return s


def selected(doc, name: str) -> str | None:
    opt = doc.xpath(f"//select[contains(@name, '{name}')]/option[@selected]")
    return opt[0].get("value") if opt else None


def parse_game_log(page_html: str, spec: LogSpec, season: int, player_id: int) -> list[dict]:
    """일자별 기록 페이지 → 경기 한 줄씩. 표는 월마다 하나이고 합계 줄(tfoot)은 버린다."""
    doc = lxml_html.fromstring(page_html)
    rows: list[dict] = []
    per_day: dict[date, int] = {}
    for table in doc.xpath("//table[contains(@class, 'tbl')]"):
        heads = [th.text_content().strip() for th in table.xpath("./thead//th")]
        if len(heads) < 3 or not heads[0].endswith("월") or heads[1] != "상대":
            continue
        for tr in table.xpath("./tbody/tr"):
            cells = [td.text_content().strip() for td in tr.xpath("./td")]
            if len(cells) != len(heads) or not re.fullmatch(r"\d{1,2}\.\d{1,2}", cells[0]):
                continue  # '기록이 없습니다' 같은 줄
            month, day = (int(x) for x in cells[0].split("."))
            game_date = date(season, month, day)
            per_day[game_date] = per_day.get(game_date, 0) + 1
            row: dict = {
                "season": season, "player_id": player_id, "game_date": game_date,
                "game_no": per_day[game_date], "opponent": cells[1] or None,
            }
            extra: dict = {}
            for head, cell in zip(heads[2:], cells[2:]):
                if head in spec.columns:
                    col, sqltype = spec.columns[head]
                    row[col] = _value(cell, sqltype)
                else:
                    extra[head] = cell
            for col, _ in spec.columns.values():
                row.setdefault(col, None)
            if "ip_text" in row:
                row["ip_outs"] = ip_to_outs(row["ip_text"])
            row["extra"] = json.dumps(extra, ensure_ascii=False) if extra else None
            rows.append(row)
    return rows


def _hidden_fields(page_html: str) -> dict[str, str]:
    doc = lxml_html.fromstring(page_html)
    return {i.get("name"): i.get("value", "") for i in doc.xpath("//input[@type='hidden']") if i.get("name")}


def fetch_page(session: requests.Session, spec: LogSpec, player_id: int, season: int, series: str) -> str:
    url = f"{BASE_URL}/Record/Player/{spec.detail}/Daily.aspx?playerId={player_id}"
    res = session.get(url, timeout=20)
    res.raise_for_status()
    form = _hidden_fields(res.text)
    form.update({
        "__EVENTTARGET": FORM_PREFIX + "ddlYear",
        "__EVENTARGUMENT": "",
        FORM_PREFIX + "ddlYear": str(season),
        FORM_PREFIX + "ddlSeries": series,
    })
    res = session.post(url, data=form, timeout=20)
    res.raise_for_status()
    doc = lxml_html.fromstring(res.text)
    got = (selected(doc, "ddlYear"), selected(doc, "ddlSeries"))
    if got != (str(season), series):
        raise RuntimeError(f"시즌·시리즈 선택 실패: 요청 {(season, series)}, 페이지 {got}")
    return res.text


def _targets(conn, spec: LogSpec, record_type: str, season: int, series: str, refresh: bool) -> list[tuple[int, int | None]]:
    """(playerId, 기본기록 경기 수) 목록. 이미 받은 선수는 뺀다."""
    existing = set(inspect(conn).get_table_names(schema="raw"))
    expected: dict[int, int | None] = {}
    for t in spec.source_tables:
        if t not in existing:
            continue
        for pid, g in conn.execute(
            text(f"SELECT player_id, g FROM raw.{t} WHERE season = :s AND series = :series"),
            {"s": season, "series": series},
        ):
            expected.setdefault(pid, None if g is None else int(g))  # 앞 테이블(기본기록)의 G를 우선
    if not refresh:
        done = {r[0] for r in conn.execute(
            text(f"SELECT player_id FROM raw.{PROGRESS_TABLE} WHERE record_type = :t AND season = :s AND series = :series"),
            {"t": record_type, "s": season, "series": series},
        )}
        expected = {k: v for k, v in expected.items() if k not in done}
    return sorted(expected.items())


def _save(engine: Engine, spec: LogSpec, record_type: str, season: int, series: str,
          player_id: int, rows: list[dict], expected_g: int | None) -> None:
    """한 선수·시즌을 한 트랜잭션으로: 기존 줄 지우고 넣고 progress 기록."""
    now = datetime.now().astimezone()
    with engine.begin() as conn:
        key = {"s": season, "series": series, "pid": player_id}
        conn.execute(text(f"DELETE FROM raw.{spec.table} WHERE season = :s AND series = :series AND player_id = :pid"), key)
        if rows:
            for r in rows:
                r["series"] = series
                r["scraped_at"] = now
            cols = list(rows[0])
            conn.execute(
                text(f"INSERT INTO raw.{spec.table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                rows,
            )
        conn.execute(text(f"DELETE FROM raw.{PROGRESS_TABLE} WHERE record_type = :t AND season = :s AND series = :series AND player_id = :pid"),
                     {**key, "t": record_type})
        conn.execute(
            text(f"INSERT INTO raw.{PROGRESS_TABLE} VALUES (:t, :s, :series, :pid, :n, :exp, :now)"),
            {**key, "t": record_type, "n": len(rows), "exp": expected_g, "now": now},
        )


def collect_game_logs(engine: Engine, record_type: str, season: int, series: str = SERIES_REGULAR,
                      delay: float = 0.3, refresh: bool = False, session: requests.Session | None = None) -> dict:
    spec = SPECS[record_type]
    with engine.begin() as conn:
        conn.execute(text(_create_sql(spec)))
        conn.execute(text(PROGRESS_SQL))
        targets = _targets(conn, spec, record_type, season, series, refresh)
    log.info("%s %d: 대상 %d명", record_type, season, len(targets))

    backup = PROJECT_ROOT / "data" / "raw" / "kbo" / "game_log" / record_type / f"{season}_series{series}"
    backup.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()
    session.headers.update(HEADERS)

    stats = {"players": 0, "games": 0, "mismatch": 0, "failed": []}
    for i, (pid, expected_g) in enumerate(targets, 1):
        try:
            page = None
            for attempt in range(3):
                try:
                    page = fetch_page(session, spec, pid, season, series)
                    break
                except (requests.RequestException, RuntimeError) as e:
                    if attempt == 2:
                        raise
                    log.info("playerId %d 재시도 (%s)", pid, e)
                    time.sleep(5 * (attempt + 1))
            with gzip.open(backup / f"{pid}.html.gz", "wt", encoding="utf-8") as f:
                f.write(page)
            rows = parse_game_log(page, spec, season, pid)
            _save(engine, spec, record_type, season, series, pid, rows, expected_g)
            stats["players"] += 1
            stats["games"] += len(rows)
            if expected_g is not None and len(rows) != expected_g:
                stats["mismatch"] += 1
                log.debug("playerId %d: 경기 %d줄, 기본기록 G=%d", pid, len(rows), expected_g)
        except Exception as e:
            log.warning("%s %d playerId %d 실패: %s", record_type, season, pid, e)
            stats["failed"].append(pid)
        if i % 100 == 0:
            log.info("%s %d: %d/%d명, %d경기", record_type, season, i, len(targets), stats["games"])
        time.sleep(delay)
    log.info("%s %d 완료: %d명 %d경기, G 불일치 %d명, 실패 %d명",
             record_type, season, stats["players"], stats["games"], stats["mismatch"], len(stats["failed"]))
    return stats


def main(argv: list[str] | None = None) -> None:
    from kbo_ai_platform.ingestion.cli import parse_seasons

    ap = argparse.ArgumentParser(description="KBO 선수 경기별 기록 수집 (재시작 가능)")
    ap.add_argument("--season", default=f"{FIRST_SEASON}-2026", help="예: 2026, 2001-2026 (최근 시즌부터 진행)")
    ap.add_argument("--type", default="all", choices=["hitter", "pitcher", "all"])
    ap.add_argument("--series", default=SERIES_REGULAR, help="0=정규시즌 (기본값)")
    ap.add_argument("--delay", type=float, default=0.3, help="선수 사이 대기(초)")
    ap.add_argument("--refresh", action="store_true", help="이미 받은 선수도 다시 받기")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    seasons = sorted(parse_seasons(args.season), reverse=True)
    if any(s < FIRST_SEASON for s in seasons):
        log.warning("%d 이전 시즌은 사이트에서 고를 수 없어 건너뜁니다", FIRST_SEASON)
        seasons = [s for s in seasons if s >= FIRST_SEASON]
    types = ["hitter", "pitcher"] if args.type == "all" else [args.type]
    engine = get_engine()
    session = requests.Session()
    summary = []
    for season in seasons:
        for t in types:
            st = collect_game_logs(engine, t, season, args.series, args.delay, args.refresh, session)
            summary.append((season, t, st["players"], st["games"], st["mismatch"], len(st["failed"])))
    print("시즌 종류 선수 경기 G불일치 실패")
    for row in summary:
        print(*row)


if __name__ == "__main__":
    main()
