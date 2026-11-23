"""에이전트 API (FastAPI).

실행: uv run kbo-agent-api   → http://localhost:8000 (웹페이지), http://localhost:8000/docs (API 문서)

- POST /ask          질문 → 답변 + 근거(SQL, 결과 표) + 지표(응답시간, 토큰)
- POST /feedback     답변에 👍/👎
- GET  /monitoring   최근 N일 사용 요약 (건수, 오류율, 응답시간, 토큰, 질문 분류, 피드백)
- GET  /health       DB 연결 확인
"""

import os
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from kbo_ai_platform.agent.graph import KBOAgent
from kbo_ai_platform.agent.run_log import save_feedback, save_run, summary
from kbo_ai_platform.db.connection import PROJECT_ROOT, get_agent_engine, get_engine

app = FastAPI(title="KBO AI Agent", version="0.1.0")


@lru_cache(maxsize=1)
def agent() -> KBOAgent:
    return KBOAgent(get_agent_engine())


@lru_cache(maxsize=1)
def admin_engine():
    return get_engine()


class TurnIn(BaseModel):
    question: str
    answer: str


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500, examples=["2026 시즌 홈런 1위는?"])
    history: list[TurnIn] = Field(default_factory=list, max_length=10)


class FeedbackIn(BaseModel):
    run_id: str
    score: Literal[1, -1]


@app.post("/ask")
async def ask(body: AskIn) -> dict:
    out = await run_in_threadpool(agent().ask, body.question, [t.model_dump() for t in body.history])
    try:
        await run_in_threadpool(save_run, admin_engine(), out, "api")
    except Exception as e:  # 로그 저장 실패가 답변을 막지는 않는다
        out["log_error"] = str(e)[:200]
    return out


@app.post("/feedback")
def feedback(body: FeedbackIn) -> dict:
    if not save_feedback(admin_engine(), body.run_id, body.score):
        raise HTTPException(404, "해당 실행 기록이 없습니다.")
    return {"ok": True}


@app.get("/monitoring")
def monitoring(days: int = 7) -> dict:
    return summary(admin_engine(), days)


@app.get("/health")
def health() -> dict:
    with agent().engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "model": agent().llm.model}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "web" / "index.html")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=os.getenv("AGENT_HOST", "127.0.0.1"), port=int(os.getenv("AGENT_PORT", "8000")))


if __name__ == "__main__":
    main()
