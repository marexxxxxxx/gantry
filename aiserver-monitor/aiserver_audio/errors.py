"""Einheitliches Fehlerformat des Audio-Sidecars."""

_SERVER_CODES = frozenset((
    "internal_error", "model_not_installed", "upstream_unavailable",
    "timeout"))


class AudioError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def audio_error_body(message: str, code: str) -> dict:
    typ = "server_error" if code in _SERVER_CODES \
        else "invalid_request_error"
    return {"error": {"message": message, "type": typ, "code": code}}
