"""에이전트용 읽기 전용 DB 계정과 로그 테이블을 만든다.

실행: uv run kbo-agent-setup
- .env 에 AGENT_DB_PASSWORD 가 없으면 무작위로 만들어 .env 끝에 추가한다.
- kbo_agent 계정은 raw·analytics 스키마를 SELECT 만 할 수 있고, 쿼리 하나가 10초를 넘으면 끊긴다.
"""

import os
import secrets

from dotenv import load_dotenv
from sqlalchemy import text

from kbo_ai_platform.db.connection import PROJECT_ROOT, get_engine

AGENT_ROLE = "kbo_agent"


def ensure_agent_password() -> str:
    env_path = PROJECT_ROOT / ".env"
    load_dotenv(env_path)
    password = os.getenv("AGENT_DB_PASSWORD")
    if password:
        return password
    password = secrets.token_urlsafe(24)
    with env_path.open("a", encoding="utf-8") as f:
        f.write(f"\n# 에이전트 읽기 전용 DB 계정 (kbo-agent-setup 이 만듦)\nAGENT_DB_PASSWORD={password}\n")
    os.environ["AGENT_DB_PASSWORD"] = password
    print(".env 에 AGENT_DB_PASSWORD 를 추가했습니다.")
    return password


def main() -> None:
    password = ensure_agent_password()
    engine = get_engine()
    db = os.environ["POSTGRES_DB"]
    with engine.begin() as conn:
        for path in sorted((PROJECT_ROOT / "db" / "agent").glob("*.sql")):
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            print(f"적용: {path.name}")
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": AGENT_ROLE}).scalar()
        verb = "ALTER" if exists else "CREATE"
        # 비밀번호는 바인딩 파라미터를 쓸 수 없어 따옴표를 이스케이프해 넣는다 (token_urlsafe 라 따옴표 없음).
        conn.exec_driver_sql(f"{verb} ROLE {AGENT_ROLE} LOGIN PASSWORD '{password.replace(chr(39), '')}'")
        conn.exec_driver_sql(f"ALTER ROLE {AGENT_ROLE} SET statement_timeout = '10s'")
        conn.exec_driver_sql(f"ALTER ROLE {AGENT_ROLE} SET default_transaction_read_only = on")
        conn.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{db}" TO {AGENT_ROLE}')
        for schema in ("raw", "analytics"):
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA {schema} TO {AGENT_ROLE}")
            conn.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {AGENT_ROLE}")
            conn.exec_driver_sql(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema} TO {AGENT_ROLE}")
            conn.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {AGENT_ROLE}"
            )
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA ops FROM {AGENT_ROLE}")
    print(f"읽기 전용 계정 {AGENT_ROLE} 준비 완료 (raw, analytics SELECT 전용)")


if __name__ == "__main__":
    main()
