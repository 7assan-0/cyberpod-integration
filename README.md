# CyberPod Integration

Auth + `/api/v1` + Runtime adapter to Astra #2 (`cyberpod.infra/v1`).

## التشغيل

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m cyberpod_core --demo --labs labs
```

`--demo` يبقي MemoryRuntime. لتجربة الـ adapter بدون Docker:

```bash
export CYBERPOD_INFRA_MODE=memory
# Engine(…, runtime=cyberpod_core.infra_runtime.memory_factory())
```

عامل Linux فيه Docker + حزمة Astra #2:

```bash
export CYBERPOD_INFRA_MODE=cli
export CYBERPOD_INFRA_POLICY=/etc/cyberpod-infra/worker.json
.venv/bin/python -m cyberpod_core --runtime-factory cyberpod_core.infra_runtime:factory --tokens tokens.json --labs labs
```

## الاختبار

```bash
.venv/bin/python -m unittest tests.test_infra_adapter -v
```

الـ adapter يحوّل Lab Core إلى طلب `cyberpod.infra/v1` ويُرجع READY/CLEANED إلى RuntimeSnapshot. لا يشغّل Docker من Core.

## ما لم يُنجز بعد

بناء صور Kali/Target على عامل Docker، Gateway لـ noVNC، عزل مستخدمين حي، HTTPS، E2E كامل.
