# CyberPod Integration — Auth + `/api/v1`

هذه خطوة التكامل الأولى بعد نسخة العرض: عقد طالب موحّد على Core مع جلسة حقيقية.

يغطي البندين 1 و2 من قائمة ما قبل النشر:

1. Authentication موحد للواجهة: cookie HttpOnly + CSRF.
2. واجهة `/api/v1` بنفس غلاف Frontend: `{ session }`, `{ labs }`, `{ flag, expected_revision }`.

مسارات Core القديمة بـ Bearer ما زالت موجودة للخدمات الداخلية.

## التشغيل

Python 3.12+

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m cyberpod_core --demo --labs labs
```

الخادم: `http://127.0.0.1:8000`

- Email: `demo@cyberpod.local`
- Password: `CyberPodDemo123!`
- Flag: `CYBERPOD{hydra_ssh_cracked}`

```bash
curl -c cookies -b cookies -H 'content-type: application/json' \
  -d '{"email":"demo@cyberpod.local","password":"CyberPodDemo123!"}' \
  http://127.0.0.1:8000/api/v1/auth/login
```

استخدم `csrf_token` في الرأس `X-CSRF-Token` مع كل POST.

## الاختبار

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

76 اختبارًا محليًا ناجحة في هذه البيئة.

## ما لم يُنجز بعد

Docker adapter، صور Kali/Target، Gateway لـ noVNC، عزل حي، HTTPS، E2E كامل.
