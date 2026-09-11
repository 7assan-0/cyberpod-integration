# CyberPod Integration

Auth, API, infra adapter, images, gateway, concurrent isolation.

## عزل مستخدمين

```bash
python3 -m unittest tests.test_concurrent_isolation -v
```

طالبان يبدآن معًا: جلسات منفصلة، موارد Infra منفصلة، قائمة جلسات للمالك فقط، تذكرة سطح المكتب غير قابلة للنقل، حذف جلسة A لا يوقف B.

## ما لم يُنجز بعد

حذف موارد Docker الفعلي، HTTPS + rate limit، E2E كامل.
