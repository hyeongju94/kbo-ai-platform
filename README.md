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

## 기술 스택

Python 3.12 · uv · PostgreSQL · Airflow · MLflow · FastAPI · LangGraph · Docker

## 진행 상황

- [x] 프로젝트 뼈대, PostgreSQL docker compose
- [x] 데이터 수집: 타자·투수 기본기록 (Selenium)
- [x] 데이터 수집: 2000~2026 선수(타격·투구·주루·수비)·팀 기록, 팀 순위, 선수 기본정보, 계산용 뷰
  - 사이트에 없는 것: 2000~2001 타자 2페이지(출루율 등), 2000년 팀 타격·투수 기록, 2000년 주루·수비
- [ ] 모델 학습과 MLflow
- [ ] FastAPI 서빙
- [ ] Airflow 파이프라인
- [ ] LangGraph 에이전트
- [ ] 웹페이지
