"""에이전트가 쓰는 도구.

1. run_sql: LLM 이 만든 SQL 을 검사한 뒤 읽기 전용 계정으로 실행한다.
   - 검사(sqlglot): SELECT 1문장만, 허용된 테이블만, 행 수 제한(LIMIT) 강제.
   - DB 계정 자체도 SELECT 권한만 있고 10초 넘는 쿼리는 끊긴다 (kbo-agent-setup).
2. predict_player: 예측 모델 API 를 부르는 자리. 모델이 아직 없어서 '준비 중' 을 돌려준다.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlalchemy import Engine, text
from sqlglot import exp

from kbo_ai_platform.agent.schema_doc import allowed_tables

MAX_ROWS = 200

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Merge, exp.TruncateTable, exp.Grant,
)
# 파일·네트워크·시스템에 닿는 함수는 막는다 (읽기 전용 계정이라도 이중으로).
FORBIDDEN_FUNCTIONS = {"pg_read_file", "pg_ls_dir", "pg_sleep", "dblink", "lo_import", "lo_export", "set_config"}


class UnsafeSQLError(ValueError):
    """실행하면 안 되는 SQL."""


def check_sql(sql: str, max_rows: int = MAX_ROWS) -> str:
    """SQL 을 검사하고, LIMIT 이 없거나 너무 크면 max_rows 로 바꾼 SQL 을 돌려준다."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQLError(f"SQL 문법 오류: {e}") from e
    if len(statements) != 1:
        raise UnsafeSQLError("SQL 은 한 문장만 실행할 수 있습니다.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQLError("SELECT 문만 실행할 수 있습니다.")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQLError(f"허용되지 않는 구문: {type(node).__name__}")
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise UnsafeSQLError(f"허용되지 않는 함수: {name}")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed = allowed_tables()
    for table in tree.find_all(exp.Table):
        if not table.name:  # generate_series(...) 같은 테이블 함수
            continue
        if not table.db and table.name.lower() in cte_names:
            continue
        full = f"{table.db}.{table.name}".lower()
        if full not in allowed:
            raise UnsafeSQLError(f"조회할 수 없는 테이블: {table.sql()} (스키마.테이블 이름으로 허용된 것만 사용)")

    limit = tree.args.get("limit")
    current = None
    if limit is not None:
        try:
            current = int(limit.expression.name)
        except (AttributeError, ValueError):
            current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows)
    return tree.sql(dialect="postgres")


@dataclass
class SQLResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_markdown(self, max_rows: int = 50) -> str:
        if not self.rows:
            return "(결과 0행)"
        head = "| " + " | ".join(self.columns) + " |"
        sep = "|" + "---|" * len(self.columns)
        body = ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in self.rows[:max_rows]]
        more = [f"... 외 {len(self.rows) - max_rows}행"] if len(self.rows) > max_rows else []
        return "\n".join([head, sep, *body, *more])


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def run_sql(engine: Engine, sql: str, max_rows: int = MAX_ROWS) -> SQLResult:
    safe_sql = check_sql(sql, max_rows)
    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            result = conn.execute(text(safe_sql))
            columns = list(result.keys())
            rows = [[_jsonable(v) for v in row] for row in result.fetchall()]
    return SQLResult(sql=safe_sql, columns=columns, rows=rows, truncated=len(rows) >= max_rows)


def predict_player(player_name: str, season: int | None = None, target: str | None = None) -> dict:
    """예측 모델 API 호출 자리. 모델 서빙(serving/)이 생기면 여기서 HTTP 로 부른다."""
    return {
        "status": "not_available",
        "message": "예측 모델은 아직 준비 중입니다. 지금은 과거 기록 조회만 할 수 있어요.",
        "request": {"player_name": player_name, "season": season, "target": target},
    }
