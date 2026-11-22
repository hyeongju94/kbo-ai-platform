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

## 기술 스택

Python 3.12 · uv · PostgreSQL · Airflow · MLflow · FastAPI · LangGraph · Docker

## 진행 상황

- [x] 프로젝트 뼈대, PostgreSQL docker compose
- [ ] 데이터 수집
- [ ] 모델 학습과 MLflow
- [ ] FastAPI 서빙
- [ ] Airflow 파이프라인
- [ ] LangGraph 에이전트
- [ ] 웹페이지
