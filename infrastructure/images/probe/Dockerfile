ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 1000 learner && useradd --uid 1000 --gid 1000 --no-create-home learner
COPY --chown=0:0 probe.py /probe.py
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=2s --timeout=1s --start-period=2s --retries=10 CMD ["python3", "/probe.py", "health"]
CMD ["python3", "/probe.py", "serve"]
