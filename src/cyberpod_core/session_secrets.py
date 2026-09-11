from __future__ import annotations
import hashlib, hmac, secrets

class SessionVault:
    """Per-session training secrets. Student APIs never receive the plaintext flag."""
    def __init__(self):
        self._plain = {}
        self._hash = {}

    def issue(self, session_id: str, generation: int) -> dict:
        flag = 'CYBERPOD{' + secrets.token_hex(8) + '}'
        password = secrets.token_urlsafe(8)
        record = {'username': 'admin', 'password': password, 'flag': flag, 'generation': generation}
        key = (session_id, generation)
        self._plain[key] = record
        self._hash[key] = hashlib.sha256(flag.encode()).hexdigest()
        return {'username': 'admin'}

    def check(self, session_id: str, generation: int, value: str) -> bool:
        expected = self._hash.get((session_id, generation))
        if not expected:
            return False
        digest = hashlib.sha256((value or '').encode()).hexdigest()
        return hmac.compare_digest(digest, expected)

    def target_config(self, session_id: str, generation: int) -> dict | None:
        """Runtime-only injection. Do not attach this to student session JSON."""
        return self._plain.get((session_id, generation))

    def drop(self, session_id: str) -> None:
        drop = [key for key in list(self._plain) if key[0] == session_id]
        for key in drop:
            self._plain.pop(key, None)
            self._hash.pop(key, None)
