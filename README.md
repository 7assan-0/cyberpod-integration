# CyberPod Integration

Auth, API, infra adapter, images, gateway, isolation, session reaper.

## Cleanup

```bash
python3 -m unittest tests.test_session_reaper -v
```

`SessionReaper` يحذف الجلسة ثم يراجع: لا موارد Core، لا سجل runtime، لا تذاكر سطح المكتب، Infra CLEANED. `sweep()` يكشف اليتامة.

## ما لم يُنجز بعد

HTTPS + rate limit + سجلات، E2E كامل.
