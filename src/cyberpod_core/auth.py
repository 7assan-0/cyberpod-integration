import hashlib
import json
from pathlib import Path

from .models import Principal


class StaticTokens:
    """Bootstrap/service credentials. Production can inject an OIDC IdentityProvider.

    Headers containing user IDs are never accepted as identity. Tokens are not logged.
    """

    def __init__(self, tokens: dict[str, Principal]):
        if not tokens or any(len(token) < 24 for token in tokens):
            raise ValueError("At least one bearer token of 24+ characters is required")
        self._tokens = {hashlib.sha256(token.encode()).digest(): p.model_copy(deep=True)
                        for token, p in tokens.items()}

    @classmethod
    def from_file(cls, path: Path):
        data = json.loads(Path(path).read_text())
        tokens = {}
        for entry in data:
            token = entry["token"]
            if token in tokens:
                raise ValueError("Duplicate token")
            tokens[token] = Principal.model_validate({"subject": entry["subject"], "scopes": entry["scopes"]})
        return cls(tokens)

    async def authenticate(self, bearer_token):
        principal = self._tokens.get(hashlib.sha256(bearer_token.encode()).digest())
        return principal.model_copy(deep=True) if principal else None

