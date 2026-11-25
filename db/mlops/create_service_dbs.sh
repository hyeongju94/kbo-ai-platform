#!/bin/sh
# MLflow·Airflow 메타데이터용 계정·DB를 기존 Postgres 안에 만든다 (여러 번 실행해도 안전).
# compose 의 mlops-db-init 서비스가 실행하며, Postgres 컨테이너는 재시작하지 않는다.
set -eu
export PGHOST=postgres PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" PGDATABASE="$POSTGRES_DB"

until pg_isready -q; do echo "waiting for postgres..."; sleep 2; done

create_db() {  # $1 = 계정, $2 = 비밀번호, $3 = DB 이름
  psql -q -v ON_ERROR_STOP=1 -v u="$1" -v p="$2" -v d="$3" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'u')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'u') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'u', :'p') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'d', :'u')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'d') \gexec
-- 이 DB 에는 해당 계정만 접속 (에이전트 계정 등은 차단)
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'d') \gexec
SQL
  echo "database ready: $3 (owner $1)"
}

create_db "$MLFLOW_DB_USER"  "$MLFLOW_DB_PASSWORD"  "$MLFLOW_DB_NAME"
create_db "$AIRFLOW_DB_USER" "$AIRFLOW_DB_PASSWORD" "$AIRFLOW_DB_NAME"
