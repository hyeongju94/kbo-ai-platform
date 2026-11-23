"""LangGraph 로 짠 KBO 질의응답 에이전트.

    질문 ─▶ classify ─┬─ stats ──────▶ generate_sql ─▶ execute_sql ─┬─▶ answer ─▶ 끝
                      │                      ▲                       │
                      │                      └── 오류/0행이면 재시도 ─┘ (최대 3번)
                      ├─ prediction ─▶ predict ─▶ answer
                      ├─ general ────▶ general_answer
                      └─ clarify / out_of_scope ─▶ fixed_reply

근거: 답변과 함께 실행한 SQL, 결과 표, 조회 조건을 돌려준다(evidence).
"""

import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine

from kbo_ai_platform.agent import prompts
from kbo_ai_platform.agent.llm import ClaudeLLM, LLMRefusal, Usage
from kbo_ai_platform.agent.schema_doc import build_schema_doc
from kbo_ai_platform.agent.tools import SQLResult, UnsafeSQLError, predict_player, run_sql

MAX_SQL_ATTEMPTS = 3


class Turn(TypedDict):
    question: str
    answer: str


class AgentState(TypedDict, total=False):
    question: str
    history: list[Turn]
    category: str
    standalone_question: str
    sql_draft: str
    sql_plan: str
    sql_attempts: int
    sql_feedback: str | None
    result: SQLResult | None
    prediction: dict | None
    answer: str
    caveats: list[str]
    error: str | None


def _history_text(history: list[Turn] | None) -> str:
    if not history:
        return "(없음)"
    return "\n".join(f"Q: {t['question']}\nA: {t['answer'][:300]}" for t in history[-3:])


class KBOAgent:
    def __init__(self, engine: Engine, llm: ClaudeLLM | None = None):
        self.engine = engine
        self.llm = llm or ClaudeLLM()
        self._sql_system: str | None = None
        self.graph = self._build()

    @property
    def sql_system(self) -> str:
        if self._sql_system is None:
            self._sql_system = prompts.SQL_SYSTEM_TEMPLATE.format(schema_doc=build_schema_doc(self.engine))
        return self._sql_system

    # ── 노드 ────────────────────────────────────────────────
    def classify(self, state: AgentState, usage: Usage) -> AgentState:
        user = f"[이전 대화]\n{_history_text(state.get('history'))}\n\n[새 질문]\n{state['question']}"
        c = self.llm.structured(
            prompts.CLASSIFY_SYSTEM, user, prompts.Classification, usage, fast=True, effort="low", max_tokens=2000
        )
        out: AgentState = {"category": c.category, "standalone_question": c.standalone_question}
        if c.category == "clarify":
            out["answer"] = c.clarifying_question or "조금 더 구체적으로 알려 주시겠어요? (선수 이름, 시즌 등)"
        return out

    def generate_sql(self, state: AgentState, usage: Usage) -> AgentState:
        attempts = state.get("sql_attempts", 0) + 1
        user = f"질문: {state['standalone_question']}"
        if state.get("sql_feedback"):
            user += f"\n\n[이전 시도]\nSQL:\n{state.get('sql_draft')}\n문제: {state['sql_feedback']}\n이 문제를 고친 SQL 을 다시 써라."
        draft = self.llm.structured(self.sql_system, user, prompts.SQLDraft, usage, effort="medium")
        return {"sql_draft": draft.sql, "sql_plan": draft.plan, "sql_attempts": attempts, "sql_feedback": None}

    def execute_sql(self, state: AgentState) -> AgentState:
        try:
            result = run_sql(self.engine, state["sql_draft"])
        except UnsafeSQLError as e:
            return {"result": None, "sql_feedback": f"안전 검사 실패: {e}", "error": str(e)}
        except Exception as e:  # DB 오류(없는 열 등)는 메시지를 LLM 에게 돌려줘 고치게 한다
            msg = str(getattr(e, "orig", e)).split("\n")[0][:500]
            return {"result": None, "sql_feedback": f"DB 오류: {msg}", "error": msg}
        feedback = None
        if result.row_count == 0 and state.get("sql_attempts", 1) == 1:
            feedback = "결과가 0행입니다. 선수·팀 이름 표기, 시즌 범위, 조건이 너무 좁지 않은지 확인하세요."
        return {"result": result, "sql_feedback": feedback, "error": None}

    def predict(self, state: AgentState) -> AgentState:
        return {"prediction": predict_player(state["standalone_question"])}

    def answer(self, state: AgentState, usage: Usage) -> AgentState:
        q = state["standalone_question"]
        if state.get("prediction") is not None:
            user = (
                f"질문: {q}\n\n[예측 도구 결과]\n{state['prediction']}\n\n"
                "예측 모델이 아직 없다는 점을 솔직히 말하고, 대신 과거 기록 조회 질문을 예로 들어 제안하라."
            )
        elif state.get("result") is None:
            return {
                "answer": "죄송해요, 이 질문에 맞는 데이터를 조회하지 못했어요. 질문을 조금 바꿔 다시 시도해 주세요.",
                "caveats": [f"마지막 오류: {state.get('error')}"],
            }
        else:
            r = state["result"]
            user = (
                f"질문: {q}\n\n[조회 방법]\n{state.get('sql_plan')}\n\n"
                f"[조회 결과] ({r.row_count}행{', 200행에서 잘림' if r.truncated else ''})\n{r.to_markdown()}"
            )
        a = self.llm.structured(prompts.ANSWER_SYSTEM, user, prompts.Answer, usage, effort="low")
        return {"answer": a.answer, "caveats": a.caveats}

    def general_answer(self, state: AgentState, usage: Usage) -> AgentState:
        a = self.llm.structured(
            prompts.GENERAL_SYSTEM, state["standalone_question"], prompts.Answer, usage, effort="low"
        )
        return {"answer": a.answer, "caveats": ["데이터 조회 없이 일반 지식으로 답했습니다."]}

    def fixed_reply(self, state: AgentState) -> AgentState:
        if state.get("category") == "out_of_scope":
            return {"answer": "저는 KBO 야구 기록을 조회해 답하는 에이전트예요. 선수·팀 기록, 순위, FA 계약 등을 물어봐 주세요."}
        return {}

    # ── 분기 ────────────────────────────────────────────────
    @staticmethod
    def route_category(state: AgentState) -> str:
        return {
            "stats": "generate_sql",
            "prediction": "predict",
            "general": "general_answer",
        }.get(state["category"], "fixed_reply")

    @staticmethod
    def route_after_sql(state: AgentState) -> str:
        if state.get("sql_feedback") and state.get("sql_attempts", 0) < MAX_SQL_ATTEMPTS:
            return "generate_sql"
        return "answer"

    def _build(self):
        # LLM 노드는 토큰 사용량을 모으도록 config 로 Usage 를 받는다.
        def with_usage(fn):
            return lambda state, config: fn(state, config["configurable"]["usage"])

        g = StateGraph(AgentState)
        g.add_node("classify", with_usage(self.classify))
        g.add_node("generate_sql", with_usage(self.generate_sql))
        g.add_node("execute_sql", self.execute_sql)
        g.add_node("predict", self.predict)
        g.add_node("answer", with_usage(self.answer))
        g.add_node("general_answer", with_usage(self.general_answer))
        g.add_node("fixed_reply", self.fixed_reply)

        g.add_edge(START, "classify")
        g.add_conditional_edges("classify", self.route_category)
        g.add_edge("generate_sql", "execute_sql")
        g.add_conditional_edges("execute_sql", self.route_after_sql)
        g.add_edge("predict", "answer")
        g.add_edge("answer", END)
        g.add_edge("general_answer", END)
        g.add_edge("fixed_reply", END)
        return g.compile()

    # ── 실행 ────────────────────────────────────────────────
    def ask(self, question: str, history: list[Turn] | None = None) -> dict[str, Any]:
        """질문 하나를 처리하고 답변·근거·지표를 돌려준다."""
        run_id = str(uuid.uuid4())
        usage = Usage()
        started = time.perf_counter()
        error = None
        try:
            state = self.graph.invoke(
                {"question": question, "history": history or [], "sql_attempts": 0},
                config={"configurable": {"usage": usage}, "recursion_limit": 20},
            )
        except LLMRefusal as e:
            state, error = {"answer": str(e), "category": "refused"}, str(e)
        except Exception as e:
            state, error = {"answer": "처리 중 오류가 났어요. 잠시 뒤 다시 시도해 주세요.", "category": "error"}, repr(e)[:500]
        latency_ms = int((time.perf_counter() - started) * 1000)

        result: SQLResult | None = state.get("result")
        evidence = None
        if result is not None:
            evidence = {
                "sql": result.sql,
                "plan": state.get("sql_plan"),
                "columns": result.columns,
                "rows": result.rows[:50],
                "row_count": result.row_count,
                "truncated": result.truncated,
            }
        return {
            "run_id": run_id,
            "question": question,
            "category": state.get("category"),
            "standalone_question": state.get("standalone_question"),
            "answer": state.get("answer", ""),
            "caveats": state.get("caveats", []),
            "evidence": evidence,
            "sql_attempts": state.get("sql_attempts", 0),
            "error": error or (state.get("error") if result is None and state.get("category") == "stats" else None),
            "metrics": {
                "latency_ms": latency_ms,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "llm_calls": usage.calls,
                "models": sorted(usage.models),
            },
        }
