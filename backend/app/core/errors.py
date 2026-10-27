from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class FluxoraError(HTTPException):
    """Base for every API error so responses share one envelope."""

    error_type = "internal_error"

    def __init__(self, messages: str | list[str], status_code: int = 500):
        self.messages = [messages] if isinstance(messages, str) else messages
        super().__init__(status_code=status_code, detail=self.messages)


class InvalidParameters(FluxoraError):
    error_type = "invalid_parameters"

    def __init__(self, messages: str | list[str]):
        super().__init__(messages, status.HTTP_400_BAD_REQUEST)


class Unauthorized(FluxoraError):
    error_type = "unauthorized"

    def __init__(self, messages: str | list[str] = "Invalid or missing credentials"):
        super().__init__(messages, status.HTTP_401_UNAUTHORIZED)


class Forbidden(FluxoraError):
    error_type = "forbidden"

    def __init__(self, messages: str | list[str] = "Not permitted"):
        super().__init__(messages, status.HTTP_403_FORBIDDEN)


class NotFound(FluxoraError):
    error_type = "not_found"

    def __init__(self, messages: str | list[str] = "Resource not found"):
        super().__init__(messages, status.HTTP_404_NOT_FOUND)


class Conflict(FluxoraError):
    error_type = "conflict"

    def __init__(self, messages: str | list[str]):
        super().__init__(messages, status.HTTP_409_CONFLICT)


class RateLimited(FluxoraError):
    error_type = "rate_limited"

    def __init__(self, messages: str | list[str] = "Too many requests"):
        super().__init__(messages, status.HTTP_429_TOO_MANY_REQUESTS)


def _envelope(error_type: str, messages: list[str]) -> dict:
    return {"error": {"type": error_type, "messages": messages}}


async def fluxora_error_handler(_: Request, exc: FluxoraError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Basic"} if exc.status_code == 401 else None
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.error_type, exc.messages),
        headers=headers,
    )


async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    by_status = {
        400: "invalid_parameters",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        429: "rate_limited",
    }
    detail = exc.detail
    messages = detail if isinstance(detail, list) else [str(detail)]
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(by_status.get(exc.status_code, "internal_error"), messages),
    )


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    messages = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p not in ("body", "query"))
        messages.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return JSONResponse(
        status_code=422, content=_envelope("invalid_parameters", messages)
    )
