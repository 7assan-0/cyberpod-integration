class CoreError(Exception):
    """Only explicit, public-safe messages may cross the HTTP boundary."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ProviderCancelled(Exception):
    """Cooperative cancellation; allocated resources must remain discoverable."""

