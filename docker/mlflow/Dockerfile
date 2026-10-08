# The upstream MLflow image has no PostgreSQL driver; add the pinned one.
FROM ghcr.io/mlflow/mlflow:v3.14.0@sha256:967de3ca34b6a5751718e380a0a383eb0ce1a6f5b484f363bd0da7559117a5f8
RUN pip install --no-cache-dir psycopg2-binary==2.9.13
