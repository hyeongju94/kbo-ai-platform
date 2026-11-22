"""KBO 기록실 페이지별 수집기."""

from kbo_ai_platform.ingestion.scrapers.record_table import RecordPage, SeasonNotAvailable, scrape_record_pages

__all__ = ["RecordPage", "SeasonNotAvailable", "scrape_record_pages"]
