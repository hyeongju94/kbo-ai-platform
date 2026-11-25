#!/usr/bin/env bash
# airflow-init: 메타DB 마이그레이션, 관리자 비밀번호 파일, 연결정보(Connections) 등록. 여러 번 실행해도 안전.
set -euo pipefail

airflow db migrate

# SimpleAuthManager 로그인 계정 (admin / .env.mlops 의 AIRFLOW_ADMIN_PASSWORD)
python - <<'PY'
import json, os
path = os.environ["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE"]
with open(path, "w") as f:
    json.dump({"admin": os.environ["AIRFLOW_ADMIN_PASSWORD"]}, f)
os.chmod(path, 0o600)
PY

set_conn() {  # 이미 있으면 지우고 다시 등록 (.env 값이 바뀌어도 반영되도록)
  local id="$1"; shift
  airflow connections delete "$id" >/dev/null 2>&1 || true
  airflow connections add "$id" "$@" >/dev/null
  echo "connection: $id"
}

# KBO 데이터 DB (kbo)
set_conn kbo_postgres --conn-type postgres --conn-host postgres --conn-port 5432 \
  --conn-login "$POSTGRES_USER" --conn-password "$POSTGRES_PASSWORD" --conn-schema "$POSTGRES_DB"
# MLflow 추적 서버
set_conn mlflow_http --conn-type http --conn-host mlflow --conn-port 5000 --conn-schema http
# MinIO (S3 호환): S3Hook(aws_conn_id="minio_s3")
set_conn minio_s3 --conn-type aws --conn-login "$MINIO_ROOT_USER" --conn-password "$MINIO_ROOT_PASSWORD" \
  --conn-extra '{"endpoint_url": "http://minio:9000", "region_name": "us-east-1"}'
