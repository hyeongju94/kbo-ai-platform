-- 에이전트용 설정. 실행: uv run kbo-agent-setup (여러 번 실행해도 안전)
-- 1) 에이전트가 만든 SQL은 읽기 전용 계정(kbo_agent)으로만 실행한다.
--    LLM이 실수로 DELETE·DROP 을 만들어도 DB 권한에서 한 번 더 막힌다.
-- 2) 질문·SQL·응답시간·토큰 수를 ops.agent_runs 에 남겨 모니터링한다.

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.agent_runs (
    run_id         uuid PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    question       text NOT NULL,
    category       text,
    sql            text,
    sql_attempts   integer,
    row_count      integer,
    answer         text,
    error          text,
    latency_ms     integer,
    input_tokens   integer,
    output_tokens  integer,
    model          text,
    source         text,          -- api / eval / cli
    feedback       smallint       -- 웹페이지 👍(1) / 👎(-1)
);
CREATE INDEX IF NOT EXISTS agent_runs_created_at ON ops.agent_runs (created_at);
