"""KBO 기록실 표(선수·팀 기록, 팀 순위)를 시즌·시리즈별로 전부 넘기며 HTML을 모은다."""

import logging
import time
from dataclasses import dataclass

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

log = logging.getLogger(__name__)

BASE_URL = "https://www.koreabaseball.com"

# 드롭다운 id 끝부분 (전체 id는 cphContents_..._ddlSeason_ddlSeason 처럼 길다)
# 선수 기록은 ddlSeason, 팀 순위는 ddlYear 라는 이름을 쓴다
SEASON_SELECT = "select[id$='ddlSeason'], select[id$='ddlYear']"
SERIES_SELECT = "select[id$='ddlSeries']"
# 선수 기록 표는 tData01, 팀 기록·순위 표는 tData. 첫 번째 표가 본 기록이다.
TABLE = "table.tData01, table.tData"
PAGER_LINK = "div.paging a[id*='ucPager_btnNo']"
PAGER_NEXT = "div.paging a[id$='ucPager_btnNext']"

# 시리즈 코드: 기록실 기본값이 '한국시리즈'일 때가 있으므로 꼭 직접 고른다
SERIES_REGULAR = "0"


class SeasonNotAvailable(Exception):
    """이 화면의 드롭다운에 해당 시즌(또는 시리즈)이 없다. 예: 팀 수비기록은 2001년부터."""


@dataclass
class RecordPage:
    page_no: int
    html: str  # 페이지 전체 HTML (백업 + 파싱용)


def _wait_reload(driver: WebDriver, old_table, timeout: int = 20) -> None:
    """포스트백 후 예전 표가 사라지고 새 표가 나타날 때까지 기다린다."""
    WebDriverWait(driver, timeout).until(EC.staleness_of(old_table))
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, TABLE))
    )


def _select(driver: WebDriver, css: str, value: str, delay: float) -> None:
    """드롭다운 값을 바꾸고, 바뀌었다면 포스트백이 끝날 때까지 기다린다."""
    select = Select(driver.find_element(By.CSS_SELECTOR, css))
    if select.first_selected_option.get_attribute("value") == value:
        return
    if value not in {o.get_attribute("value") for o in select.options}:
        raise SeasonNotAvailable(f"드롭다운에 '{value}' 값이 없습니다")
    table = driver.find_element(By.CSS_SELECTOR, TABLE)
    select.select_by_value(value)
    _wait_reload(driver, table)
    time.sleep(delay)


def _sort(driver: WebDriver, col: str, delay: float) -> None:
    """표 머리글 정렬(페이지 안의 JS sort 함수)을 호출한다.

    기본 정렬(타율, 평균자책점)은 규정타석/규정이닝을 채운 선수만 보여 주기 때문에,
    경기수 같은 다른 열로 정렬해서 전체 선수를 받는다.
    """
    current = driver.find_element(By.CSS_SELECTOR, "input[id$='hfOrderByCol']").get_attribute("value")
    if current == col:
        return
    table = driver.find_element(By.CSS_SELECTOR, TABLE)
    table.find_element(By.CSS_SELECTOR, f"thead a[href*=\"sort('{col}')\"]").click()
    _wait_reload(driver, table)
    time.sleep(delay)


def _current_page_no(driver: WebDriver) -> int:
    on = driver.find_elements(By.CSS_SELECTOR, "div.paging a.on")
    return int(on[0].text) if on else 1


def _click_next_page(driver: WebDriver) -> bool:
    """다음 페이지로 이동. 더 없으면 False."""
    current = _current_page_no(driver)
    target = None
    for link in driver.find_elements(By.CSS_SELECTOR, PAGER_LINK):
        if link.text.strip() == str(current + 1):
            target = link
            break
    if target is None:  # 1~5 다음 묶음(6~10)으로 넘어가는 '>' 버튼
        try:
            target = driver.find_element(By.CSS_SELECTOR, PAGER_NEXT)
        except NoSuchElementException:
            return False
        if not target.get_attribute("href"):
            return False
    table = driver.find_element(By.CSS_SELECTOR, TABLE)
    target.click()
    _wait_reload(driver, table)
    return _current_page_no(driver) == current + 1


def scrape_record_pages(
    driver: WebDriver,
    path: str,
    season: int,
    series: str = SERIES_REGULAR,
    sort_col: str | None = None,
    entry_path: str | None = None,
    delay: float = 1.5,
    max_pages: int = 50,
) -> list[RecordPage]:
    """기록실 한 화면(path)에서 시즌·시리즈를 고르고 모든 페이지의 HTML을 돌려준다."""
    driver.get(BASE_URL + (entry_path or path))
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, TABLE)))
    time.sleep(delay)

    _select(driver, SEASON_SELECT, str(season), delay)
    _select(driver, SERIES_SELECT, series, delay)

    if entry_path:
        # 시즌 선택은 서버 세션에 남아 있으므로 '다음' 버튼으로 목표 화면에 들어간다
        table = driver.find_element(By.CSS_SELECTOR, TABLE)
        driver.find_element(By.CSS_SELECTOR, f".more_record a[href$='{path}']").click()
        _wait_reload(driver, table)
        time.sleep(delay)
        if not driver.current_url.split("?")[0].endswith(path):
            raise RuntimeError(f"{path} 로 이동하지 못했습니다: {driver.current_url}")
    if sort_col:
        _sort(driver, sort_col, delay)

    # 선택이 제대로 됐는지 다시 확인 (조용히 다른 시즌을 긁는 일을 막는다)
    got_season = Select(driver.find_element(By.CSS_SELECTOR, SEASON_SELECT)).first_selected_option
    got_series = Select(driver.find_element(By.CSS_SELECTOR, SERIES_SELECT)).first_selected_option
    if got_season.get_attribute("value") != str(season) or got_series.get_attribute("value") != series:
        raise RuntimeError(f"드롭다운 선택 실패: {got_season.text} / {got_series.text}")

    pages = [RecordPage(_current_page_no(driver), driver.page_source)]
    log.info("%s page %d 수집", path, pages[-1].page_no)
    while len(pages) < max_pages:
        time.sleep(delay)
        if not _click_next_page(driver):
            break
        pages.append(RecordPage(_current_page_no(driver), driver.page_source))
        log.info("%s page %d 수집", path, pages[-1].page_no)
    return pages
