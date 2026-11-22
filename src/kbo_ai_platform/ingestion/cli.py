"""수집 명령어.

예) uv run kbo-collect --season 2026 --type hitter
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine
from kbo_ai_platform.ingestion.browser import chrome
from kbo_ai_platform.ingestion.loader import load_records
from kbo_ai_platform.ingestion.parsers import parse_pages
from kbo_ai_platform.ingestion.scrapers import scrape_record_pages
from kbo_ai_platform.ingestion.scrapers.record_table import SERIES_REGULAR

log = logging.getLogger("kbo_collect")

# 기록 종류별 설정. sort_col: 기본 정렬은 규정타석/규정이닝 선수만 보여 주므로 경기수로 정렬해 전체를 받는다.
RECORD_TYPES = {
    "hitter": {"path": "/Record/Player/HitterBasic/Basic1.aspx", "table": "hitter_basic", "sort_col": "GAME_CN"},
    "pitcher": {"path": "/Record/Player/PitcherBasic/Basic1.aspx", "table": "pitcher_basic", "sort_col": "GAME_CN"},
}


def _backup(pages, record_type: str, season: int, series: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PROJECT_ROOT / "data" / "raw" / "kbo" / record_type / f"{season}_series{series}" / stamp
    out.mkdir(parents=True, exist_ok=True)
    for p in pages:
        (out / f"page_{p.page_no:02d}.html").write_text(p.html, encoding="utf-8")
    return out


def collect(record_type: str, season: int, series: str, headless: bool, delay: float) -> int:
    cfg = RECORD_TYPES[record_type]
    with chrome(headless=headless) as driver:
        pages = scrape_record_pages(
            driver, cfg["path"], season, series=series, sort_col=cfg["sort_col"], delay=delay
        )
    backup_dir = _backup(pages, record_type, season, series)
    log.info("원본 HTML %d페이지 저장: %s", len(pages), backup_dir)

    df = parse_pages([p.html for p in pages])
    n = load_records(get_engine(), df, cfg["table"], season, series)
    log.info("raw.%s 에 %d명 적재 (시즌 %d, 시리즈 %s)", cfg["table"], n, season, series)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="KBO 기록실 데이터를 수집해 PostgreSQL에 넣습니다.")
    parser.add_argument("--season", type=int, required=True, help="시즌 연도 (예: 2026)")
    parser.add_argument("--type", choices=[*RECORD_TYPES, "all"], default="hitter", help="기록 종류")
    parser.add_argument("--series", default=SERIES_REGULAR, help="시리즈 코드 (0=정규시즌)")
    parser.add_argument("--delay", type=float, default=1.5, help="요청 사이 대기 시간(초)")
    parser.add_argument("--show-browser", action="store_true", help="브라우저 창을 띄워서 실행")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    types = list(RECORD_TYPES) if args.type == "all" else [args.type]
    for t in types:
        collect(t, args.season, args.series, headless=not args.show_browser, delay=args.delay)


if __name__ == "__main__":
    main()
