# KBO AI Platform

KBO 선수 데이터를 기반으로 한 MLOps 파이프라인과 LangGraph LLM 에이전트 포트폴리오 프로젝트입니다.

## 전체 흐름

```
데이터 수집 ──▶ PostgreSQL ──▶ 학습 (MLflow 기록) ──▶ FastAPI 서빙
     ▲                                                  │
     └──────────── Airflow로 스케줄링 ───────────────────┘
                                                        ▼
                     LangGraph 에이전트 (DB 조회 + 모델 예측) ──▶ 웹페이지
```

## 폴더 구조

| 경로 | 역할 |
|---|---|
| `src/kbo_ai_platform/ingestion/` | KBO 선수·경기 기록 수집 후 PostgreSQL 적재 |
| `src/kbo_ai_platform/db/` | DB 접속 설정과 스키마 |
| `src/kbo_ai_platform/training/` | 모델 학습, MLflow 실험 추적 |
| `src/kbo_ai_platform/serving/` | FastAPI 예측 API |
| `src/kbo_ai_platform/agent/` | LangGraph 기반 LLM 에이전트 |
| `airflow/dags/` | 수집·학습 파이프라인 DAG |
| `web/` | 에이전트와 대화하는 간단한 웹페이지 |
| `db/init/` | Postgres 최초 실행 시 적용되는 SQL (`raw`, `analytics` 스키마) |
| `notebooks/` | 탐색적 분석 |
| `data/` | 로컬 데이터 (git에 올리지 않음) |
| `tests/` | 테스트 |

## 시작하기

필요한 것: [uv](https://docs.astral.sh/uv/), Docker

```bash
cp .env.example .env        # 비밀번호 변경
docker compose up -d        # PostgreSQL 16 실행
uv sync                     # Python 3.12 가상환경
```

접속 확인:

```bash
docker compose exec postgres psql -U kbo -d kbo -c '\dn'
```

## 데이터 수집

KBO 공식 기록실(koreabaseball.com)을 헤드리스 크롬(Selenium)으로 열어 시즌과 `KBO 정규시즌`을 고르고,
페이지를 끝까지 넘기며 표를 읽어 PostgreSQL `raw` 스키마에 넣습니다. 필요한 것: Google Chrome.

```bash
uv run kbo-collect --season 2026 --type hitter    # 타자 기본기록 → raw.hitter_basic
uv run kbo-collect --season 2026 --type pitcher   # 투수 기본기록 → raw.pitcher_basic
uv run kbo-collect --season 2026 --type all       # 둘 다
uv run kbo-collect --season 2000-2025 --type all  # 여러 시즌 (2019,2021 처럼 쉼표도 가능)
```

- 선수는 이름 대신 KBO `playerId`로 구분합니다 (동명이인 구분).
- 같은 시즌을 다시 수집하면 그 시즌 행을 지우고 새로 넣으므로 중복이 생기지 않습니다.
- 기본 정렬(타율·평균자책점)은 규정타석/이닝 선수만 보여 주므로 경기수로 정렬해 전체 선수를 받습니다.
- 시즌마다 항목이 다르면 테이블에 열을 추가하고, 없는 항목은 NULL로 둡니다. 팀명은 당시 이름 그대로(해태, 현대 등) 저장합니다.
- 한 시즌이 실패해도 나머지는 계속 수집하고, 마지막에 시즌별 결과를 요약합니다.
- 원본 HTML은 `data/raw/kbo/` 에 백업됩니다 (git 제외).

| 모듈 | 역할 |
|---|---|
| `ingestion/browser.py` | 헤드리스 크롬 실행 |
| `ingestion/scrapers/record_table.py` | 시즌·시리즈 선택, 정렬, 페이지 넘기기 |
| `ingestion/parsers.py` | HTML 표 → DataFrame, playerId 추출 |
| `ingestion/loader.py` | DataFrame → PostgreSQL (트랜잭션으로 교체) |
| `ingestion/cli.py` | `kbo-collect` 명령 |

## LLM 에이전트 (LangGraph + Claude)

자연어 질문을 받아 DB를 조회하고, **답변과 함께 근거(실행한 SQL과 결과 표)** 를 돌려주는 에이전트입니다.

```
질문 ─▶ 분류 ─┬─ 기록 조회 ─▶ SQL 생성 ─▶ 안전 검사·실행 ─┬─▶ 근거 포함 답변
              │                 ▲                          │
              │                 └─ 오류·0행이면 고쳐서 재시도 ┘ (최대 3번)
              ├─ 예측 ───────▶ 예측 모델 도구 (모델 준비 전이라 '준비 중' 응답)
              ├─ 일반 지식 ──▶ 용어·규칙 설명 (DB 없이)
              └─ 되묻기 / 범위 밖
```

| 파일 | 역할 |
|---|---|
| `agent/graph.py` | LangGraph 상태 그래프 (분류 → SQL 생성 → 실행·재시도 → 답변) |
| `agent/prompts.py` | 노드별 프롬프트와 구조화 출력(Pydantic) 형식 |
| `agent/schema_doc.py` | LLM 에게 주는 DB 설명서. 열 목록은 DB에서 자동으로 읽고 사람이 쓴 설명을 붙임 |
| `agent/tools.py` | `run_sql`(SQL 검사 + 읽기 전용 실행), `predict_player`(예측 API 자리) |
| `agent/llm.py` | Claude API 호출 (구조화 출력, 프롬프트 캐싱, 토큰 집계) |
| `agent/run_log.py` | 실행 기록 저장(`ops.agent_runs`)과 모니터링 요약 |
| `serving/api.py` | FastAPI: `/ask`, `/feedback`, `/monitoring`, `/health`, 웹페이지 |
| `evaluation/run_eval.py` | 평가 세트 채점 |
| `web/index.html` | 질문·근거 보기·👍👎·모니터링 탭이 있는 웹페이지 |

**안전장치 (LLM 이 만든 SQL을 그대로 믿지 않음)**
1. `sqlglot` 으로 SQL을 파싱해 SELECT 한 문장만, 허용된 테이블만 통과시키고 LIMIT 200을 강제합니다.
2. 실제 실행은 SELECT 권한만 있는 `kbo_agent` 계정 + 읽기 전용 트랜잭션 + 10초 제한으로 합니다.
3. 실행 기록 테이블(`ops`)은 에이전트 계정이 읽을 수도 없습니다.

**실행**

```bash
# .env 에 ANTHROPIC_API_KEY 를 넣은 뒤
uv run kbo-agent-setup     # 읽기 전용 DB 계정 + 로그 테이블 (한 번만)
uv run kbo-agent-api       # http://localhost:8000  (API 문서: /docs)
uv run kbo-agent-eval      # 평가 세트 실행 → eval/results/
```

모델: 질문 분류는 `claude-haiku-5-5`, SQL 생성·답변은 `claude-sonnet-5-5` 가 기본입니다
(`.env` 의 `AGENT_FAST_MODEL`, `AGENT_MODEL` 로 변경). 인증은 `ANTHROPIC_API_KEY` 또는 `ant auth login`.

**평가** (`eval/questions.jsonl`, 25문항: 기록 조회 20 · 일반 지식 2 · 예측 1 · 범위 밖 1 · 프롬프트 인젝션 1)

| 지표 | 뜻 |
|---|---|
| 분류 정확도 | 질문 유형을 맞게 골랐는가 |
| 실행 정확도 | 정답 SQL 결과가 에이전트 조회 결과에 모두 들어 있는가 |
| 근거 일치율 | 답변 속 숫자가 조회 결과 표에 실제로 있는가 (지어낸 숫자 탐지) |
| 안전성 | "테이블 삭제해" 같은 요청에도 DB가 그대로인가 |

결과 (2026-11-24, `eval/results/`)

| 모델 구성 | 분류 | 실행 정확도 | 근거 일치율 | 안전성 | 응답시간 중앙값 / 최대 | 평균 토큰 (입력 / 출력) |
|---|---|---|---|---|---|---|
| 전 단계 Opus 5.5 (22문항) | 22/22 | 17/17 | 96.6% | 통과 | 12.7초 / 19.5초 | 6,748 / 742 |
| 분류 Haiku 5.5 + SQL·답변 Sonnet 5.5 (기본값) | 22/22 | 17/17 | 95.9% | 통과 | 7.4초 / 12.8초 | 6,725 / 599 |
| 같은 구성, 팀 순위 2종 분리 후 25문항 | 25/25 | 20/20 | 99.5% | 통과 | 7.0초 / 10.5초 | 7,270 / 591 |

근거 일치율이 100%가 아닌 건 대부분 답변이 표 값으로 직접 계산한 숫자(2위와의 차이, 도루 성공률 등)라서,
채점기가 표에 없는 숫자로 세기 때문입니다.

**모니터링**: 모든 질문의 분류·SQL·응답시간·토큰·오류·피드백이 `ops.agent_runs` 에 쌓이고,
웹페이지 "모니터링" 탭(`GET /monitoring`)에서 최근 7일 오류율, 응답시간 p50/p95, 평균 토큰, 👍/👎 를 봅니다.

## 기술 스택

Python 3.12 · uv · PostgreSQL · Airflow · MLflow · FastAPI · LangGraph · Docker

## 진행 상황

- [x] 프로젝트 뼈대, PostgreSQL docker compose
- [x] 데이터 수집: 타자·투수 기본기록 (Selenium)
- [x] 데이터 수집: 2000~2026 선수(타격·투구·주루·수비)·팀 기록, 팀 순위, 선수 기본정보, 계산용 뷰
  - 팀 순위는 두 가지: `analytics.team_season.final_rank` 는 기록실 순위(포스트시즌 반영, 우승팀 1위),
    `regular_season_rank` 는 승률로 다시 계산한 정규시즌 순위(승률 동률 5개 시즌은 결정전·상대전적 결과를 뷰에 적어 반영)
  - 사이트에 없는 것: 2000~2001 타자 2페이지(출루율 등), 2000년 팀 타격·투수 기록, 2000년 주루·수비
- [ ] 모델 학습과 MLflow
- [ ] FastAPI 서빙
- [ ] Airflow 파이프라인
- [x] LangGraph 에이전트 (질문 분류, SQL 생성·검사·재시도, 근거 포함 답변, 평가, 모니터링)
- [x] 웹페이지 (질문, 근거 보기, 피드백, 모니터링 탭)
