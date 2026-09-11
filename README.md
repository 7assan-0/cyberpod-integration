# CyberPod Integration

طبقة العقد الطلابي `/api/v1` فوق Core: جلسة كوكي، CSRF، وDTO الآمن للفرونت.

المسارات:

- `POST /api/v1/auth/login` `POST /api/v1/auth/logout` `GET /api/v1/auth/me`
- `GET /api/v1/labs` `GET /api/v1/labs/{id}`
- `POST /api/v1/labs/{id}/sessions`
- `GET /api/v1/sessions/{id}/status`
- `POST /api/v1/sessions/{id}/start|stop|restart|flags|cleanup`

معرّفات Infra مسبوقة بـ `session_id` حتى لا تتشارك جلستان `vol-*` أو `ctr-*`.

لتشغيل الاختبارات يلزم Core Astra #1 على `PYTHONPATH` مع هذه الطبقة فوقه. Docker الحي غير مشمول هنا.
