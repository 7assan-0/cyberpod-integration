# Kali + Target images

تعريفات الصور جاهزة. بناء OCI حي محجوب بدون Docker (`build.sh` يخرج 77).

| الصورة | المستخدم | المنفذ | فحص الصحة |
|---|---|---|---|
| `cyberpod/kali-desktop:dev` | student / 1000 | 6080 | `/opt/cyberpod/bin/healthcheck.sh` |
| `cyberpod/hydra-target:dev` | 1000:1000 | 8080 | `GET /health` |

```bash
bash images/build.sh
python3 -m unittest tests.test_image_contracts tests.test_hydra_target -v
```
