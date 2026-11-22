"""DataFrame → PostgreSQL raw 스키마.

같은 (시즌, 시리즈)를 다시 수집하면 그 범위의 기존 행을 지우고 새로 넣는다.
한 트랜잭션 안에서 처리하므로 중간에 실패해도 예전 데이터가 그대로 남고, 중복도 생기지 않는다.
"""

import pandas as pd
from sqlalchemy import Engine, inspect, text

SCHEMA = "raw"


def _sql_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    return "TEXT"


def _add_missing_columns(conn, df: pd.DataFrame, table: str) -> None:
    """시즌마다 화면 항목이 조금씩 다를 수 있다. 테이블에 없는 열이 오면 열을 추가한다.

    반대로 이번 시즌에 없는 열은 to_sql이 알아서 NULL로 채운다.
    """
    existing = {c["name"] for c in inspect(conn).get_columns(table, schema=SCHEMA)}
    for col in df.columns:
        if col not in existing:
            conn.execute(text(f'ALTER TABLE {SCHEMA}."{table}" ADD COLUMN "{col}" {_sql_type(df[col])}'))


def load_records(
    engine: Engine,
    df: pd.DataFrame,
    table: str,
    season: int,
    series: str,
    key_cols: tuple[str, ...] = ("player_id",),
) -> int:
    if df.empty:
        raise ValueError("적재할 행이 없습니다")
    df = df.copy()
    df.insert(0, "series", series)
    df.insert(0, "season", season)
    df["scraped_at"] = pd.Timestamp.now(tz="Asia/Seoul")

    with engine.begin() as conn:
        if not inspect(conn).has_table(table, schema=SCHEMA):
            # 첫 실행: 열 구조만 가진 빈 테이블을 만들고 기본키를 건다
            df.head(0).to_sql(table, conn, schema=SCHEMA, index=False)
            pk = ", ".join(["season", "series", *key_cols])
            conn.execute(text(f'ALTER TABLE {SCHEMA}."{table}" ADD PRIMARY KEY ({pk})'))
        else:
            _add_missing_columns(conn, df, table)
        conn.execute(
            text(f'DELETE FROM {SCHEMA}."{table}" WHERE season = :season AND series = :series'),
            {"season": season, "series": series},
        )
        df.to_sql(table, conn, schema=SCHEMA, index=False, if_exists="append", method="multi")
    return len(df)
