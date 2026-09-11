# CyberPod Integration

Auth + `/api/v1` + Infra adapter + image definitions for Kali and Hydra target.

## الصور

```bash
bash images/build.sh
```

يبني `cyberpod/kali-desktop:dev` و `cyberpod/hydra-target:dev`. في هذه البيئة البناء محجوب (لا Docker) ويخرج 77.

التحقق بدون Docker:

```bash
python3 -m unittest tests.test_image_contracts tests.test_hydra_target -v
```

## ما لم يُنجز بعد

Gateway لـ noVNC/WebSocket، عزل مستخدمين حي، حذف موارد مثبت، HTTPS، E2E كامل.
