# syntax=docker/dockerfile:1
ARG KALI_BASE=kalilinux/kali-rolling
FROM ${KALI_BASE}
ARG DEBIAN_FRONTEND=noninteractive
ARG CYBERPOD_TOOL_PROFILE=mvp
ARG CYBERPOD_EXTRA_PACKAGES=""
COPY packages/ /opt/cyberpod/packages/
COPY scripts/install-packages.sh /opt/cyberpod/bin/install-packages.sh
RUN bash /opt/cyberpod/bin/install-packages.sh
RUN useradd --create-home --shell /bin/bash --uid 1000 student \
    && mkdir -p /opt/cyberpod/bin /opt/cyberpod/config /usr/lib/firefox-esr/distribution \
    && chown student:student /home/student
COPY scripts/ /opt/cyberpod/bin/
COPY config/xfce/ /opt/cyberpod/config/xfce/
COPY config/firefox/policies.json /usr/lib/firefox-esr/distribution/policies.json
COPY config/web/ /usr/share/novnc/
RUN chmod 0755 /opt/cyberpod/bin/*.sh /opt/cyberpod/config/xfce/xstartup \
    && test -x /usr/bin/Xtigervnc \
    && test -f /usr/share/novnc/core/rfb.js \
    && test -f /usr/share/novnc/vnc.html \
    && mkdir -p /opt/cyberpod/build \
    && dpkg-query -W > /opt/cyberpod/build/packages.tsv
ENV HOME=/home/student USER=student LOGNAME=student LANG=C.UTF-8 DISPLAY=:1 \
    CYBERPOD_NOVNC_PORT=6080 CYBERPOD_VNC_DISPLAY=1 \
    CYBERPOD_DESKTOP_WIDTH=1440 CYBERPOD_DESKTOP_HEIGHT=900 CYBERPOD_DESKTOP_DEPTH=24 \
    CYBERPOD_VNC_SHARED=false PYTHONDONTWRITEBYTECODE=1
USER student
WORKDIR /home/student
EXPOSE 6080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
    CMD ["/opt/cyberpod/bin/healthcheck.sh"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/cyberpod/bin/entrypoint.sh"]
