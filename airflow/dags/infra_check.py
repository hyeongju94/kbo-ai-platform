"""인프라 연결 확인용 예시 DAG: KBO DB, MLflow, MinIO 에 접속되는지만 본다 (수동 실행)."""

import os

import pendulum
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.http.hooks.http import HttpHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task


@dag(schedule=None, start_date=pendulum.datetime(2026, 11, 1, tz="Asia/Seoul"), catchup=False, tags=["infra"])
def infra_check():
    @task
    def kbo_db():
        db, n_tables = PostgresHook(postgres_conn_id="kbo_postgres").get_first(
            "SELECT current_database(), count(*) FROM information_schema.tables WHERE table_schema = 'raw'"
        )
        print(f"{db}: raw 스키마 테이블 {n_tables}개")

    @task
    def mlflow_server():
        resp = HttpHook(method="GET", http_conn_id="mlflow_http").run("/health")
        print(f"MLflow /health → {resp.text}")

    @task
    def minio_buckets():
        s3 = S3Hook(aws_conn_id="minio_s3")
        for bucket in (os.environ["MLFLOW_ARTIFACT_BUCKET"], os.environ["SNAPSHOT_BUCKET"]):
            assert s3.check_for_bucket(bucket), f"bucket not found: {bucket}"
            print(f"bucket ok: {bucket}")

    kbo_db()
    mlflow_server()
    minio_buckets()


infra_check()
