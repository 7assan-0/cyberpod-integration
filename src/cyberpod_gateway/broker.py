from __future__ import annotations
from datetime import datetime, timezone
from .app import DesktopGateway

class GatewayAccessBroker:
    def __init__(self, gateway: DesktopGateway):
        self.gateway = gateway

    async def issue(self, context, principal):
        url, expires = self.gateway.issue_url(str(context.session_id), principal.subject, context.generation,
                                             expires_at=context.expires_at.timestamp())
        try:
            from cyberpod_core.models import AccessGrant
        except ImportError:
            return {
                'session_id': str(context.session_id),
                'generation': context.generation,
                'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                'browser_url': url,
                'terminal_url': None,
            }
        return AccessGrant(
            session_id=context.session_id,
            generation=context.generation,
            expires_at=datetime.fromtimestamp(expires, timezone.utc),
            browser_url=url,
        )
