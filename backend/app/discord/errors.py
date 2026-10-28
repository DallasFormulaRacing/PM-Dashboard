class DiscordError(Exception):
    pass


class RateLimited(DiscordError):
    """429, or a bucket we know is empty. Retry after `retry_after` seconds; not counted as an attempt."""

    def __init__(self, retry_after: float, *, is_global: bool = False):
        super().__init__(f"rate limited for {retry_after:.2f}s" + (" (global)" if is_global else ""))
        self.retry_after = retry_after
        self.is_global = is_global


class TransientDiscordError(DiscordError):
    """5xx or a network failure. `ambiguous` means Discord may have accepted the request anyway."""

    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


class DiscordAPIError(DiscordError):
    """A 4xx other than 429. Retrying the same request won't help."""

    # https://discord.com/developers/docs/topics/opcodes-and-status-codes#json
    UNKNOWN_CHANNEL = 10003
    UNKNOWN_MESSAGE = 10008
    UNKNOWN_USER = 10013
    UNKNOWN_WEBHOOK = 10015
    MISSING_ACCESS = 50001
    CANNOT_DM_USER = 50007
    MISSING_PERMISSIONS = 50013

    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"{status} code={code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def is_missing(self) -> bool:
        return self.status == 404 or self.code in (self.UNKNOWN_MESSAGE, self.UNKNOWN_CHANNEL, self.UNKNOWN_WEBHOOK)
