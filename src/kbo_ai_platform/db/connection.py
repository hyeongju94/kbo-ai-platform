"""PostgreSQL 연결: 프로젝트 루트의 .env 값으로 SQLAlchemy 엔진을 만든다."""

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def get_engine() -> Engine:
    load_dotenv(PROJECT_ROOT / ".env")
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    db = os.environ["POSTGRES_DB"]
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return create_engine(f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}")


def get_agent_engine() -> Engine:
    """에이전트가 만든 SQL을 실행하는 읽기 전용 계정(kbo_agent) 엔진. `uv run kbo-agent-setup` 으로 만든다."""
    load_dotenv(PROJECT_ROOT / ".env")
    password = os.environ["AGENT_DB_PASSWORD"]
    db = os.environ["POSTGRES_DB"]
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return create_engine(
        f"postgresql+psycopg://kbo_agent:{password}@{host}:{port}/{db}",
        pool_pre_ping=True,
    )
