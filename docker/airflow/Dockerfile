# Airflow 3.4 공식 이미지 + MLflow 클라이언트 (학습 태스크에서 MLflow 서버에 기록할 때 사용)
# postgres·amazon(S3/MinIO)·http 프로바이더와 boto3 는 공식 이미지에 이미 들어 있음
FROM apache/airflow:3.4.0-python3.12
RUN pip install --no-cache-dir "apache-airflow==3.4.0" "mlflow-skinny==3.18.0"
