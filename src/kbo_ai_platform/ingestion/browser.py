"""Selenium 크롬 드라이버 생성.

KBO 기록실은 ASP.NET 페이지라서 시즌/페이지를 바꿀 때마다 폼을 다시 전송(포스트백)한다.
그래서 단순 HTTP 요청 대신 실제 브라우저(헤드리스 크롬)로 조작한다.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from selenium import webdriver
from selenium.webdriver.remote.webdriver import WebDriver


@contextmanager
def chrome(headless: bool = True) -> Iterator[WebDriver]:
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--lang=ko-KR")
    driver = webdriver.Chrome(options=options)  # Selenium Manager가 드라이버를 자동으로 맞춰 준다
    driver.set_page_load_timeout(30)
    try:
        yield driver
    finally:
        driver.quit()
