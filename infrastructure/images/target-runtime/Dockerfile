# Optional Python target foundation. Astra #4 supplies application code and CMD.
# It does not build or replace an existing scenario image.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 1000 learner && useradd --uid 1000 --gid 1000 --no-create-home learner && mkdir /app && chown 1000:1000 /app
COPY --chown=0:0 http_health.py /usr/local/lib/cyberpod-http-health.py
WORKDIR /app
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=2s --start-period=10s --retries=5 CMD ["python3", "/usr/local/lib/cyberpod-http-health.py"]
