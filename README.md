# CyberPod Integration

Auth + `/api/v1` + Infra adapter + images + desktop Gateway.

## Gateway

```bash
PYTHONPATH=src python -m aiohttp.web -H 127.0.0.1 -P 8088 cyberpod_gateway.app:create_app
```

الطالب يأخذ `/desktop/{session}/?t=TICKET` فقط. العنوان الخلفي لـ noVNC لا يُرجع. Stop/logout يستدعي `/internal/revoke`.

```bash
python3 -m unittest tests.test_desktop_gateway -v
```

## ما لم يُنجز بعد

عزل مستخدمين على عامل Docker حي، حذف موارد مثبت، HTTPS + rate limit، E2E كامل.
