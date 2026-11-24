"""그래프 노드별 프롬프트와 구조화 출력 형식."""

from typing import Literal

from pydantic import BaseModel, Field

# ── 1. 질문 분류 ──────────────────────────────────────────────

Category = Literal["stats", "prediction", "general", "clarify", "out_of_scope"]


class Classification(BaseModel):
    category: Category = Field(
        description=(
            "stats: DB 의 KBO 기록·순위·FA 계약·환율을 조회해야 답할 수 있는 질문. "
            "prediction: 앞으로의 성적·순위 예측을 묻는 질문. "
            "general: 야구 규칙·용어·지표 뜻처럼 DB 없이 설명할 수 있는 질문. "
            "clarify: 누구/언제인지 모호해서 되물어야 하는 질문. "
            "out_of_scope: 야구·KBO 와 관계없는 질문."
        )
    )
    standalone_question: str = Field(
        description="이전 대화를 반영해 혼자서도 뜻이 통하게 다시 쓴 질문 (한국어)."
    )
    clarifying_question: str | None = Field(
        default=None, description="category 가 clarify 일 때 사용자에게 되물을 한 문장."
    )


CLASSIFY_SYSTEM = """\
너는 KBO(한국프로야구) 데이터 분석 에이전트의 질문 분류기다.
보유 데이터: 2000~2026 정규시즌 선수 타격·투구·수비·주루 기록, 세이버 지표(OPS, BABIP, FIP, 피타고라스 승률), \
팀 순위·팀 기록, 선수 프로필(키·몸무게·생일·연봉·출신교), FA 계약(2000~2026 주요 계약), 원/달러·원/엔 환율.
질문을 아래 중 하나로 분류하고, 이전 대화가 있으면 대명사("그 선수", "작년엔?")를 풀어 혼자 읽혀도 되는 질문으로 다시 써라.
- 특정 선수·팀·시즌 수치, 순위, 비교, 추세처럼 데이터가 필요하면 stats.
- 선수가 '최근' 이라고만 하면 2026 시즌으로 본다. 이름만 있고 시즌이 없으면 통산이나 최근 시즌으로 보고 stats 로 둔다(되묻지 않는다).
- 정말로 대상을 알 수 없을 때만 clarify.
- 2026 시즌은 포스트시즌까지 끝났다. 2026년 이전·당해 결과(우승팀, 순위 등)는 prediction 이 아니라 stats 다.
  prediction 은 사용자 메시지의 [오늘] 날짜 이후 시즌·경기를 묻는 질문뿐이다.
"""

# ── 2. SQL 생성 ───────────────────────────────────────────────


class SQLDraft(BaseModel):
    sql: str = Field(description="PostgreSQL SELECT 문 한 개. 세미콜론 없이.")
    plan: str = Field(description="어떤 테이블·조건으로 무엇을 구하는지 한국어 한 문장.")


SQL_SYSTEM_TEMPLATE = """\
너는 KBO 데이터베이스(PostgreSQL)용 SQL 작성자다. 질문에 답하는 데 필요한 SELECT 문 하나를 써라.

규칙
- 아래 설명서에 있는 테이블만 '스키마.테이블' 형태로 쓴다 (예: analytics.hitter_season).
- 읽기 전용이다. SELECT(필요하면 WITH) 한 문장만.
- 답변 근거가 되도록 선수명·팀·시즌·player_id 와 질문에 나온 지표 열을 함께 SELECT 한다.
- 결과는 많아야 수십 행이 되게 조건·정렬·LIMIT 을 건다 (최대 200행으로 잘린다).
- 선수 이름은 정확히 같게(=) 비교한다. 이전 시도에서 0행이 나왔다면 LIKE '%이름%' 처럼 넓혀 본다.
- 비율 지표 정렬엔 NULLS LAST, 나눗셈엔 NULLIF(분모, 0).
- 반올림이 필요하면 round(값::numeric, 3) 처럼 numeric 으로 바꾼 뒤 쓴다.

{schema_doc}
"""

# ── 3. 답변 ───────────────────────────────────────────────────


class Answer(BaseModel):
    answer: str = Field(description="사용자에게 보여 줄 한국어 답변 (마크다운 가능, 핵심 먼저, 5문장 이내 + 필요하면 짧은 표).")
    caveats: list[str] = Field(
        default_factory=list,
        description="답변을 읽을 때 알아야 할 조건·한계 (예: 규정타석 기준, 동명이인, 데이터에 없는 시즌). 없으면 빈 목록.",
    )


ANSWER_SYSTEM = """\
너는 KBO 데이터 분석가다. 사용자 질문에 한국어로, 야구를 잘 모르는 사람도 이해하게 쉽게 답한다.

근거 규칙 (가장 중요)
- 숫자는 반드시 [조회 결과] 표에 있는 값만 쓴다. 표에 없는 숫자를 기억이나 추측으로 만들지 않는다.
- 결과가 0행이면 "데이터에서 찾지 못했다" 고 말하고, 이름 표기·시즌 범위(2000~2026)를 확인해 보라고 안내한다.
- 조회 조건(시즌, 규정타석 필터 등)이 답에 영향을 주면 caveats 에 적는다.
- 동명이인 등으로 여러 선수가 나오면 구분해서 보여 준다.
- 팀 순위는 regular_season_rank(정규시즌)와 final_rank(포스트시즌 반영 최종, 우승팀 = 1위) 두 가지다.
  둘이 다른 건 데이터 오류가 아니다. 어느 순위인지 답변에 밝힌다.
- 지표가 낯설 수 있으면(FIP, BABIP, OPS 등) 한 줄로 뜻을 덧붙인다.
"""

GENERAL_SYSTEM = """\
너는 KBO 야구 해설가다. 규칙·용어·지표 뜻 같은 일반 질문에 한국어로 쉽게, 5문장 이내로 답한다.
특정 선수·팀의 실제 수치는 말하지 않는다(그건 데이터 조회로 답해야 하므로, 필요하면 그렇게 물어보라고 안내한다).
"""
