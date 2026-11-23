"""에이전트 실행 기록(ops.agent_runs) 저장과 모니터링 요약."""

import json

from sqlalchemy import Engine, text


def save_run(engine: Engine, out: dict, source: str) -> None:
    ev = out.get("evidence") or {}
    m = out["metrics"]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO ops.agent_runs (run_id, question, category, sql, sql_attempts, row_count, answer,
                    error, latency_ms, input_tokens, output_tokens, model, source)
                VALUES (:run_id, :question, :category, :sql, :sql_attempts, :row_count, :answer,
                    :error, :latency_ms, :input_tokens, :output_tokens, :model, :source)
                """
            ),
            {
                "run_id": out["run_id"],
                "question": out["question"],
                "category": out.get("category"),
                "sql": ev.get("sql"),
                "sql_attempts": out.get("sql_attempts"),
                "row_count": ev.get("row_count"),
                "answer": out.get("answer"),
                "error": out.get("error"),
                "latency_ms": m["latency_ms"],
                "input_tokens": m["input_tokens"],
                "output_tokens": m["output_tokens"],
                "model": json.dumps(m["models"]),
                "source": source,
            },
        )


def save_feedback(engine: Engine, run_id: str, score: int) -> bool:
    with engine.begin() as conn:
        n = conn.execute(
            text("UPDATE ops.agent_runs SET feedback = :s WHERE run_id = :r"), {"s": score, "r": run_id}
        ).rowcount
    return n == 1


def summary(engine: Engine, days: int = 7) -> dict:
    """최근 N일 실행 요약: 건수, 오류율, 응답시간 p50/p95, 토큰, 분류 비율, 피드백."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT count(*) AS runs,
                       round(avg((error IS NOT NULL)::int)::numeric, 3) AS error_rate,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                       round(avg(input_tokens)) AS avg_input_tokens,
                       round(avg(output_tokens)) AS avg_output_tokens,
                       round(avg(sql_attempts) FILTER (WHERE category = 'stats')::numeric, 2) AS avg_sql_attempts,
                       count(*) FILTER (WHERE feedback = 1) AS thumbs_up,
                       count(*) FILTER (WHERE feedback = -1) AS thumbs_down
                FROM ops.agent_runs
                WHERE created_at >= now() - make_interval(days => :d) AND source <> 'eval'
                """
            ),
            {"d": days},
        ).mappings().one()
        cats = conn.execute(
            text(
                """
                SELECT coalesce(category, 'unknown') AS category, count(*) AS n FROM ops.agent_runs
                WHERE created_at >= now() - make_interval(days => :d) AND source <> 'eval'
                GROUP BY 1 ORDER BY 2 DESC
                """
            ),
            {"d": days},
        ).all()
        recent_errors = conn.execute(
            text(
                """
                SELECT created_at, question, error FROM ops.agent_runs
                WHERE error IS NOT NULL ORDER BY created_at DESC LIMIT 5
                """
            )
        ).mappings().all()
    return {
        "days": days,
        **{k: (float(v) if v is not None and not isinstance(v, int) else v) for k, v in row.items()},
        "categories": {c: n for c, n in cats},
        "recent_errors": [
            {"at": r["created_at"].isoformat(), "question": r["question"], "error": r["error"]} for r in recent_errors
        ],
    }
