#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec python3 /opt/cyberpod/bin/desktop.py run
