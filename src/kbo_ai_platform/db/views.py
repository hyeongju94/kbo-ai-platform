"""db/views/*.sql 을 순서대로 실행해 analytics 스키마의 뷰를 만든다.

실행: uv run kbo-views
"""

from sqlalchemy import text

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine


def main() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS analytics"))
        for path in sorted((PROJECT_ROOT / "db" / "views").glob("*.sql")):
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            print(f"적용: {path.name}")


if __name__ == "__main__":
    main()
