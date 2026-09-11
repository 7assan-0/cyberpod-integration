FROM python:3.12-slim
RUN useradd --create-home --uid 1000 --shell /bin/bash student && mkdir -p /opt/cyberpod/bin && chown student:student /home/student
COPY desktop.py /opt/cyberpod/bin/desktop.py
ENV HOME=/home/student USER=student CYBERPOD_NOVNC_PORT=6080 TARGET_URL=http://target:8080
USER student
WORKDIR /home/student
EXPOSE 6080
HEALTHCHECK --interval=5s --timeout=3s --retries=8 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6080/health', timeout=1)"]
CMD ["python", "/opt/cyberpod/bin/desktop.py"]
