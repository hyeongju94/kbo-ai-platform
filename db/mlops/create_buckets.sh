#!/bin/sh
# MinIO 버킷을 만든다 (여러 번 실행해도 안전). compose 의 minio-init 서비스가 실행한다.
set -eu
export MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000"
mc ready local
mc mb --ignore-existing "local/${MLFLOW_ARTIFACT_BUCKET}"   # MLflow 아티팩트(모델 파일 등)
mc mb --ignore-existing "local/${SNAPSHOT_BUCKET}"          # 학습 데이터 스냅샷
mc version enable "local/${SNAPSHOT_BUCKET}"                # 같은 경로에 덮어써도 이전 버전 보존
mc ls local
