"""Typed service errors carrying an ErrorDetail and HTTP status."""

from research_engine_client.models import ErrorCode, ErrorDetail


class ServiceError(Exception):
    def __init__(self, detail: ErrorDetail, http_status: int = 502) -> None:
        super().__init__(detail.message)
        self.detail = detail
        self.http_status = http_status

    @classmethod
    def of(
        cls,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool,
        source: str | None = None,
        http_status: int = 502,
    ) -> "ServiceError":
        return cls(
            ErrorDetail(code=code, message=message, retryable=retryable, source=source),
            http_status,
        )
