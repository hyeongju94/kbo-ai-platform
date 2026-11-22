"""수집 명령어.

예) uv run kbo-collect --season 2026 --type hitter
    uv run kbo-collect --season 2000-2026 --type all
    uv run kbo-collect --type profile        # 수집된 선수들의 생년월일·키·연봉 등
"""

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine
from kbo_ai_platform.ingestion.browser import chrome
from kbo_ai_platform.ingestion.loader import load_records
from kbo_ai_platform.ingestion.parsers import parse_pages
from kbo_ai_platform.ingestion.scrapers import SeasonNotAvailable, scrape_record_pages
from kbo_ai_platform.ingestion.scrapers.record_table import SERIES_REGULAR
from kbo_ai_platform.ingestion.specs import GROUPS, RECORD_SPECS

log = logging.getLogger("kbo_collect")


def parse_seasons(text: str) -> list[int]:
    """'2026' / '2000-2025' / '2019,2021' 형태를 시즌 목록으로 바꾼다."""
    seasons: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            start, end = (int(x) for x in part.split("-"))
            seasons.extend(range(start, end + 1))
        else:
            seasons.append(int(part))
    return seasons


def parse_types(text: str) -> list[str]:
    """'hitter,pitcher' / 'player' / 'team' / 'all' 형태를 기록 종류 목록으로 바꾼다."""
    types: list[str] = []
    for part in text.split(","):
        part = part.strip()
        for t in GROUPS.get(part, [part]):
            if t != "profile" and t not in RECORD_SPECS:
                raise SystemExit(f"알 수 없는 기록 종류: {t}")
            if t not in types:
                types.append(t)
    return types


def _backup(pages, record_type: str, season: int, series: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PROJECT_ROOT / "data" / "raw" / "kbo" / record_type / f"{season}_series{series}" / stamp
    out.mkdir(parents=True, exist_ok=True)
    for p in pages:
        (out / f"page_{p.page_no:02d}.html").write_text(p.html, encoding="utf-8")
    return out


def collect(driver, record_type: str, season: int, series: str, delay: float) -> int:
    spec = RECORD_SPECS[record_type]
    pages = scrape_record_pages(
        driver, spec.path, season, series=series, sort_col=spec.sort_col,
        entry_path=spec.entry_path, delay=delay
    )
    backup_dir = _backup(pages, record_type, season, series)
    log.info("원본 HTML %d페이지 저장: %s", len(pages), backup_dir)

    df = parse_pages([p.html for p in pages], spec.key_cols, spec.with_player_id)
    if df.empty:  # 예: 2000년 팀 타격 기록은 사이트에 표만 있고 내용이 없다
        log.warning("%s %d: 사이트에 데이터가 없어 건너뜁니다", record_type, season)
        return 0
    n = load_records(get_engine(), df, spec.table, season, series, spec.key_cols)
    log.info("raw.%s 에 %d행 적재 (시즌 %d, 시리즈 %s)", spec.table, n, season, series)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="KBO 기록실 데이터를 수집해 PostgreSQL에 넣습니다.")
    parser.add_argument("--season", help="시즌 연도. 예: 2026, 2000-2025, 2019,2021")
    parser.add_argument(
        "--type",
        default="hitter",
        help=f"기록 종류(쉼표로 여러 개). {', '.join(RECORD_SPECS)}, profile 또는 묶음 {', '.join(GROUPS)}",
    )
    parser.add_argument("--series", default=SERIES_REGULAR, help="시리즈 코드 (0=정규시즌)")
    parser.add_argument("--delay", type=float, default=1.5, help="요청 사이 대기 시간(초)")
    parser.add_argument("--show-browser", action="store_true", help="브라우저 창을 띄워서 실행")
    parser.add_argument("--refresh", action="store_true", help="profile: 이미 받은 선수도 다시 받기")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    types = parse_types(args.type)

    if "profile" in types:
        from kbo_ai_platform.ingestion.profiles import collect_profiles

        types.remove("profile")
        if not types:
            collect_profiles(get_engine(), delay=args.delay, refresh=args.refresh)
            return
    if not args.season:
        parser.error("--season 이 필요합니다")
    seasons = parse_seasons(args.season)

    results: list[tuple[str, int, str]] = []
    failed = False
    with chrome(headless=not args.show_browser) as driver:  # 브라우저 하나로 모든 시즌을 처리
        for season in seasons:
            for t in types:
                try:
                    n = collect(driver, t, season, args.series, args.delay)
                    results.append((t, season, f"{n}행"))
                except SeasonNotAvailable:
                    log.warning("%s: %d 시즌은 사이트에서 제공하지 않아 건너뜁니다", t, season)
                    results.append((t, season, "사이트 미제공"))
                except Exception as e:  # 한 시즌이 실패해도 나머지는 계속 수집
                    log.exception("%s %d 수집 실패", t, season)
                    results.append((t, season, f"실패: {type(e).__name__}: {e}"[:200]))
                    failed = True
                time.sleep(args.delay)

    log.info("===== 요약 =====")
    for t, season, msg in results:
        log.info("%-13s %d  %s", t, season, msg)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
