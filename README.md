# CyberPod Integration

Auth, API, infra adapter, images, gateway, isolation, reaper, hardening.

## Hardening

```bash
export CYBERPOD_REQUIRE_HTTPS=1
python3 -m unittest tests.test_hardening -v
```

يرفض HTTPS عبر `X-Forwarded-Proto` + HSTS، يحدّ من الطلبات (429 + Retry-After)، ويسجّل JSON بدون أسرار.

## ما لم يُنجز بعد

اختبار End-to-End كامل من Login إلى Score إلى Cleanup.
