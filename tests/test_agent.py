"""에이전트 단위 테스트. LLM 은 가짜(FakeLLM)로 바꿔 API 키 없이 돈다.

DB 가 필요한 테스트는 로컬 Postgres(kbo_agent 계정)가 없으면 건너뛴다.
"""

import pytest

from kbo_ai_platform.agent import prompts
from kbo_ai_platform.agent.tools import UnsafeSQLError, check_sql
from kbo_ai_platform.evaluation.run_eval import execution_match, grounding


# ── SQL 안전 검사 ─────────────────────────────────────────
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM raw.fa_contracts",
        "DROP TABLE raw.fa_contracts",
        "SELECT 1; DROP TABLE raw.fa_contracts",
        "SELECT * FROM ops.agent_runs",
        "SELECT * FROM pg_catalog.pg_user",
        "SELECT pg_sleep(30)",
        "SELECT * FROM raw.hitter_basic",  # 허용 목록 밖 (analytics 뷰를 쓰게 함)
        "UPDATE raw.fa_contracts SET total_krw = 0",
    ],
)
def test_check_sql_blocks(sql):
    with pytest.raises(UnsafeSQLError):
        check_sql(sql)


def test_check_sql_adds_limit_and_allows_cte():
    out = check_sql("WITH t AS (SELECT * FROM analytics.team_season) SELECT * FROM t")
    assert out.endswith("LIMIT 200")
    assert check_sql("SELECT hr FROM analytics.hitter_season LIMIT 5").endswith("LIMIT 5")
    assert check_sql("SELECT hr FROM analytics.hitter_season LIMIT 100000").endswith("LIMIT 200")


# ── 채점 함수 ────────────────────────────────────────────
def test_execution_match():
    gold = [["김도영", 45]]
    assert execution_match(gold, [["김도영", "KIA", 45, 2026]]) == 1.0
    assert execution_match(gold, [["힐리어드", 44]]) == 0.0
    assert execution_match([["a", 1.025]], [["a", 1.0250001]]) == 1.0


def test_grounding_flags_made_up_numbers():
    rows = [["김도영", "KIA", 45, 0.347]]
    assert grounding("김도영이 45개로 1위, 타율 .347", "2026 홈런 1위", rows) == 1.0
    assert grounding("김도영이 52개로 1위", "2026 홈런 1위", rows) == 0.0
    assert grounding("FA 총액 152억 원", "", [["양의지", 15_200_000_000]]) == 1.0


# ── 그래프 (가짜 LLM) ─────────────────────────────────────
class FakeLLM:
    model = fast_model = "fake"

    def __init__(self, category="stats", sqls=("SELECT 1",)):
        self.category = category
        self.sqls = list(sqls)
        self.sql_calls = 0

    def structured(self, system, user, schema, usage, **kw):
        usage.calls += 1
        if schema is prompts.Classification:
            return prompts.Classification(category=self.category, standalone_question=user.split("[새 질문]\n")[-1])
        if schema is prompts.SQLDraft:
            sql = self.sqls[min(self.sql_calls, len(self.sqls) - 1)]
            self.sql_calls += 1
            return prompts.SQLDraft(sql=sql, plan="테스트")
        return prompts.Answer(answer="답변", caveats=[])


@pytest.fixture(scope="module")
def engine():
    try:
        from sqlalchemy import text

        from kbo_ai_platform.db.connection import get_agent_engine

        e = get_agent_engine()
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return e
    except Exception:
        pytest.skip("로컬 Postgres / kbo_agent 계정 없음")


def make_agent(engine, llm):
    from kbo_ai_platform.agent.graph import KBOAgent

    return KBOAgent(engine, llm=llm)


def test_stats_flow_returns_evidence(engine):
    llm = FakeLLM(sqls=["SELECT player_name, hr FROM analytics.hitter_season WHERE season = 2026 ORDER BY hr DESC LIMIT 1"])
    out = make_agent(engine, llm).ask("2026 홈런 1위?")
    assert out["category"] == "stats"
    assert out["evidence"]["row_count"] == 1
    assert out["sql_attempts"] == 1


def test_bad_sql_is_retried(engine):
    llm = FakeLLM(sqls=["SELECT nope FROM analytics.hitter_season", "SELECT max(hr) FROM analytics.hitter_season"])
    out = make_agent(engine, llm).ask("최다 홈런?")
    assert out["sql_attempts"] == 2
    assert out["evidence"]["rows"][0][0] > 0


def test_unsafe_sql_never_runs(engine):
    llm = FakeLLM(sqls=["DELETE FROM raw.fa_contracts"])
    out = make_agent(engine, llm).ask("FA 지워")
    assert out["evidence"] is None
    assert out["sql_attempts"] == 3


def test_out_of_scope_skips_db(engine):
    out = make_agent(engine, FakeLLM(category="out_of_scope")).ask("저녁 메뉴?")
    assert out["evidence"] is None and "KBO" in out["answer"]
