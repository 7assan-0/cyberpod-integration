# CyberPod Integration

بنود قائمة ما قبل النشر العشر مغطاة على طبقات Core/الواجهة. بناء Docker الحي يحتاج عامل Linux.

## E2E

```bash
python3 -m unittest tests.test_e2e_login_score_cleanup -v
```

Login → قائمة المخابر → Start → علم خاطئ → علم صحيح (100/100) → Cleanup بلا موارد.
