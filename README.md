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
| `airflow/dags/` | 수집·학습 파이프라인 DAG (`plugins/`, `logs/`, `config/` 도 컨테이너에 마운트) |
| `web/` | 에이전트와 대화하는 간단한 웹페이지 |
| `db/init/` | Postgres 최초 실행 시 적용되는 SQL (`raw`, `analytics` 스키마) |
| `db/mlops/` | MLflow·Airflow 전용 DB, MinIO 버킷을 만드는 초기화 스크립트 |
| `docker/` | MLflow·MinIO·Airflow 컨테이너 이미지 (Dockerfile)와 Airflow 초기화 스크립트 |
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

## MLOps 인프라 (MLflow + MinIO + Airflow)

모델 학습 기록, 학습 데이터 스냅샷, 파이프라인 스케줄링을 위한 컨테이너입니다. 학습 코드·DAG 는 아직 없고 인프라만 있습니다.

```
학습 코드 / Airflow 태스크 ──▶ MLflow 서버 ──┬─▶ PostgreSQL 의 mlflow DB    (실험·파라미터·지표·모델 레지스트리)
                                           └─▶ MinIO 버킷 mlflow-artifacts (모델 파일 등 아티팩트)
학습 코드 / Airflow 태스크 ──▶ MinIO (S3 API) ──▶ 버킷 kbo-snapshots        (학습 데이터 스냅샷, 버전 관리 켜짐)
Airflow (api-server · scheduler · dag-processor) ──▶ PostgreSQL 의 airflow DB (메타데이터)
```

| 컨테이너 | 역할 | 주소 | 메모리 제한 |
|---|---|---|---|
| `kbo-mlflow` | MLflow 3.18 추적 서버 + 웹 UI | http://localhost:5001 | 1GB |
| `kbo-minio` | S3 호환 저장소 | API http://localhost:9000 · 콘솔 http://localhost:9001 | 512MB |
| `kbo-airflow-apiserver` | Airflow 3.4 웹 UI + REST API (Airflow 3 에서 webserver 를 대체) | http://localhost:8080 | 1GB |
| `kbo-airflow-scheduler` | 스케줄링 + LocalExecutor (태스크가 이 컨테이너 안에서 실행) | | 1.5GB |
| `kbo-airflow-dag-processor` | `airflow/dags/` 의 DAG 파일을 읽어 등록 | | 768MB |
| `kbo-minio-init` | 버킷 생성 후 종료 (1회성) | | |
| `kbo-mlops-db-init` | 기존 Postgres 안에 `mlflow`·`airflow` 계정·DB 생성 후 종료 (1회성) | | |
| `kbo-airflow-init` | Airflow 메타DB 마이그레이션, 관리자 비밀번호, Connections 등록 후 종료 (1회성) | | |

실제 사용량(유휴 상태)은 MLflow 약 600MB, Airflow 3개 합계 약 900MB, MinIO 약 100MB 입니다 (Docker 메모리 3.8GB 기준).

**설계 메모**
- MLflow·Airflow 메타데이터는 KBO 데이터와 같은 Postgres 컨테이너의 **별도 DB `mlflow`, `airflow`** (각각 전용 계정, 다른 계정은 접속 불가)에 들어갑니다. `kbo` DB 와 섞이지 않습니다.
- 포트는 모두 `127.0.0.1` 에만 열립니다. macOS 는 5000번 포트를 AirPlay 수신이 쓰므로 MLflow 는 **5001** 입니다.
- MinIO 공식 Docker 이미지 배포가 중단되어(2025) Chainguard 가 소스에서 빌드한 `cgr.dev/chainguard/minio` 를 씁니다.
- MLflow 의 GenAI 평가용 백그라운드 작업(`MLFLOW_SERVER_ENABLE_JOB_EXECUTION`)은 메모리를 2GB 가까이 써서 껐습니다. 일반 실험 추적·모델 레지스트리에는 영향이 없습니다.
- Airflow 는 LocalExecutor, 동시 태스크 4개(`AIRFLOW__CORE__PARALLELISM`), 예시 DAG 끔, 새 DAG 는 일시정지 상태로 등록됩니다.
  triggerer(Deferrable 연산자용)는 메모리를 아끼려고 넣지 않았습니다.

**Airflow 에 미리 등록된 것**

| 종류 | 이름 | 내용 |
|---|---|---|
| Connection | `kbo_postgres` | KBO 데이터 DB (`PostgresHook(postgres_conn_id="kbo_postgres")`) |
| Connection | `mlflow_http` | MLflow 서버 `http://mlflow:5000` (`HttpHook`) |
| Connection | `minio_s3` | MinIO (`S3Hook(aws_conn_id="minio_s3")`, endpoint `http://minio:9000`) |
| 환경변수 | `MLFLOW_TRACKING_URI` | `http://mlflow:5000` (태스크 안에서 `mlflow` 를 바로 쓰면 이 서버로 기록) |
| 환경변수 | `MLFLOW_ARTIFACT_BUCKET`, `SNAPSHOT_BUCKET` | 버킷 이름 |
| 패키지 | postgres·amazon·http 프로바이더, boto3, mlflow-skinny 3.18 | `docker/airflow/Dockerfile` |

폴더 마운트: `airflow/dags`(DAG 파일), `airflow/plugins`, `airflow/logs`(태스크 로그, git 제외), `airflow/config`(생성된 로그인 비밀번호 파일, git 제외),
`src/` → 컨테이너의 `/opt/airflow/project/src` (`PYTHONPATH` 에 포함, 읽기 전용).
DAG 파일을 `airflow/dags/` 에 저장하면 30초 안팎으로 UI 에 나타납니다.
`airflow/dags/infra_check.py` 는 세 연결(KBO DB, MLflow, MinIO)이 되는지만 보는 동작 확인용 예시입니다.

**실행**

```bash
cp .env.mlops.example .env.mlops   # 비밀번호·키 채우기 (MinIO 비밀번호는 8자 이상, 키 만드는 명령은 파일 안에)
docker compose up -d mlflow airflow-apiserver airflow-scheduler airflow-dag-processor
docker compose ps
```

순서는 자동입니다: minio → 버킷 생성 / mlflow·airflow DB 생성 → mlflow, airflow-init → Airflow 3개.
Airflow 로그인은 `admin` / `.env.mlops` 의 `AIRFLOW_ADMIN_PASSWORD` 입니다.

> 꼭 **서비스 이름을 붙여** 실행하세요. MLOps 서비스들은 postgres 에 `depends_on` 을 걸지 않아서 실행 중인 Postgres 를 건드리지 않습니다.
> 이름 없이 `docker compose up -d` 를 하면 `.env` 가 Postgres 를 띄운 뒤에 바뀌었을 경우
> (예: `kbo-agent-setup` 이 `AGENT_DB_PASSWORD` 추가) Postgres 컨테이너가 다시 만들어집니다(데이터는 볼륨에 남지만 잠시 끊김).

동작 확인 (예시 DAG 실행):

```bash
docker compose exec airflow-scheduler airflow dags unpause infra_check
docker compose exec airflow-scheduler airflow dags trigger infra_check
docker compose exec airflow-scheduler airflow dags list-runs infra_check   # state 가 success 면 정상
```

**사용 예** (로컬 학습 코드 쪽; 패키지는 필요할 때 `uv add mlflow boto3`)

```python
import mlflow
mlflow.set_tracking_uri("http://localhost:5001")   # 아티팩트도 서버를 거쳐 MinIO 로 감 → 클라이언트에 S3 키 불필요
mlflow.set_experiment("my-experiment")
with mlflow.start_run():
    mlflow.log_param("alpha", 0.1)
    mlflow.log_metric("rmse", 0.42)
```

```python
import boto3, os   # 스냅샷 버킷은 S3 API 로 직접 (키는 .env.mlops 의 MINIO_ROOT_USER / MINIO_ROOT_PASSWORD)
s3 = boto3.client("s3", endpoint_url="http://localhost:9000",
                  aws_access_key_id=os.environ["MINIO_ROOT_USER"],
                  aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"])
s3.upload_file("train.parquet", "kbo-snapshots", "2026-11-25/train.parquet")
```

**중지·정리**

```bash
docker compose stop airflow-apiserver airflow-scheduler airflow-dag-processor mlflow minio   # 중지 (데이터 유지)
docker compose rm -sf airflow-apiserver airflow-scheduler airflow-dag-processor airflow-init \
  mlflow minio minio-init mlops-db-init                                                    # 컨테이너 삭제 (볼륨·DB 유지)
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
| `ingestion/game_logs.py` | 선수 경기별(일자별) 기록 → `raw.hitter_game_log`, `raw.pitcher_game_log` |

### 경기별 기록 (게임로그)

```bash
uv run python -m kbo_ai_platform.ingestion.game_logs --season 2001-2026 --type all
```

- 기록실 선수 '일자별 기록' 페이지를 requests로 받습니다 (GET 후 시즌·정규시즌 선택 POST 한 번).
- 대상은 시즌 기본기록 테이블에 나온 선수이고, 최근 시즌부터 진행합니다.
- 끝난 선수·시즌은 `raw.game_log_progress` 에 남겨 다시 실행하면 건너뜁니다 (중단돼도 이어서 받기). `--refresh` 로 다시 받기.
- progress의 `n_games`(받은 경기 수)와 `expected_g`(기본기록 G)로 빠진 경기를 확인할 수 있습니다.
- 같은 날 두 경기(더블헤더)는 `game_no` 1, 2로 구분합니다. 투수 이닝은 `ip_text`('5 2/3')와 `ip_outs`(17) 둘 다 저장합니다.
- 사이트 시즌 선택이 2001년부터라 2000 시즌 경기별 기록은 없습니다. 원본 HTML은 `data/raw/kbo/game_log/` 에 gzip으로 백업됩니다.

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
- [x] MLOps 인프라: MLflow 추적 서버 + MinIO (아티팩트·스냅샷 버킷) + Airflow 3 (LocalExecutor) 컨테이너
- [~] 데이터 수집: 선수 경기별 기록 2011~2026 정규시즌 완료 (2001~2010은 이어서 받을 예정)
- [ ] 모델 학습과 MLflow 기록
- [ ] FastAPI 서빙
- [ ] Airflow 파이프라인 DAG
- [x] LangGraph 에이전트 (질문 분류, SQL 생성·검사·재시도, 근거 포함 답변, 평가, 모니터링)
- [x] 웹페이지 (질문, 근거 보기, 피드백, 모니터링 탭)
