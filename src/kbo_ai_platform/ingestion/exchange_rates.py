"""원/달러·원/100엔 환율 (2000~2026) → raw.fx_rates_monthly, raw.fx_rates_yearly.

출처: 한국은행 경제통계시스템(ECOS) Open API, 통계표 731Y004
'주요국 통화의 대원화환율' 의 매매기준율 평균자료(월평균, 연평균).
외국인 선수 계약(달러)이나 해외 이적(엔·달러) 금액을 원화로 비교할 때 쓴다.

.env 에 ECOS_API_KEY 가 있으면 그 키를 쓰고, 없으면 ECOS 공개 'sample' 키로
한 번에 10행씩 나눠 받는다(요청 사이 1초 간격).

실행:
    uv run python -m kbo_ai_platform.ingestion.exchange_rates
"""

from __future__ import annotations

import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import text

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine

SCHEMA = "raw"
STAT_CODE = "731Y004"
AVG = "0000100"  # 평균자료
CURRENCIES = {"USD": "0000001", "JPY100": "0000002"}  # 원/미국달러, 원/일본엔(100엔)
START_YEAR, END_YEAR = 2000, 2026
API = "https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/{start}/{end}/{stat}/{cycle}/{t0}/{t1}/{item}/{item2}"


def fetch_series(key: str, cycle: str, item: str) -> list[dict]:
    t0, t1 = (f"{START_YEAR}01", f"{END_YEAR}12") if cycle == "M" else (str(START_YEAR), str(END_YEAR))
    page = 1000 if key != "sample" else 10
    rows: list[dict] = []
    start = 1
    while True:
        url = API.format(key=key, start=start, end=start + page - 1, stat=STAT_CODE,
                         cycle=cycle, t0=t0, t1=t1, item=item, item2=AVG)
        data = requests.get(url, timeout=30).json()
        if "StatisticSearch" not in data:
            msg = data.get("RESULT", {}).get("MESSAGE", data)
            if rows:
                break
            raise RuntimeError(f"ECOS 응답 오류: {msg}")
        body = data["StatisticSearch"]
        rows.extend(body["row"])
        total = int(body["list_total_count"])
        start += page
        if start > total:
            break
        time.sleep(1)
    return rows


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("ECOS_API_KEY") or "sample"
    now = pd.Timestamp.now(tz="Asia/Seoul")
    frames = {"M": [], "A": []}
    for cur, item in CURRENCIES.items():
        for cycle in ("M", "A"):
            rows = fetch_series(key, cycle, item)
            df = pd.DataFrame(rows)[["TIME", "DATA_VALUE", "ITEM_NAME1"]]
            df["currency"] = cur
            frames[cycle].append(df)
            print(f"{cur} {cycle}: {len(df)}행")

    monthly = pd.concat(frames["M"])
    monthly = pd.DataFrame({
        "year": monthly["TIME"].str[:4].astype(int),
        "month": monthly["TIME"].str[4:6].astype(int),
        "currency": monthly["currency"],
        "krw_per_unit": monthly["DATA_VALUE"].astype(float),
        "item_name": monthly["ITEM_NAME1"],
    })
    yearly = pd.concat(frames["A"])
    yearly = pd.DataFrame({
        "year": yearly["TIME"].astype(int),
        "currency": yearly["currency"],
        "krw_per_unit": yearly["DATA_VALUE"].astype(float),
        "item_name": yearly["ITEM_NAME1"],
        "is_partial_year": False,
    })
    # 아직 끝나지 않은 해(ECOS 연평균이 없는 해)는 월평균의 평균으로 채우고 표시해 둔다
    have = set(zip(yearly["year"], yearly["currency"]))
    extra = (
        monthly[[(y, c) not in have for y, c in zip(monthly["year"], monthly["currency"])]]
        .groupby(["year", "currency"], as_index=False)
        .agg(krw_per_unit=("krw_per_unit", "mean"), item_name=("item_name", "first"))
    )
    extra["krw_per_unit"] = extra["krw_per_unit"].round(2)
    extra["is_partial_year"] = True
    yearly = pd.concat([yearly, extra]).sort_values(["currency", "year"]).reset_index(drop=True)

    source = f"한국은행 ECOS {STAT_CODE} 매매기준율 평균자료"
    for df in (monthly, yearly):
        df["source"] = source
        df["scraped_at"] = now

    engine = get_engine()
    with engine.begin() as conn:
        for name, df, pk in (
            ("fx_rates_monthly", monthly, "year, month, currency"),
            ("fx_rates_yearly", yearly, "year, currency"),
        ):
            conn.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.{name}"))
            df.to_sql(name, conn, schema=SCHEMA, index=False, method="multi")
            conn.execute(text(f"ALTER TABLE {SCHEMA}.{name} ADD PRIMARY KEY ({pk})"))
            conn.execute(text(
                f"COMMENT ON TABLE {SCHEMA}.{name} IS '{source}. krw_per_unit: "
                "USD 는 1달러당 원, JPY100 은 100엔당 원.'"))
    print(f"raw.fx_rates_monthly {len(monthly)}행, raw.fx_rates_yearly {len(yearly)}행 적재")


if __name__ == "__main__":
    main()
