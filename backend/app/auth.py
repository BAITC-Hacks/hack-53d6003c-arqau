from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Literal

from pydantic import BaseModel, model_validator


AuthRole = Literal["employee", "hr", "manager"]


class DemoLoginRequest(BaseModel):
    role: AuthRole
    employee_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "DemoLoginRequest":
        if self.role in {"employee", "manager"} and not self.employee_id:
            raise ValueError(f"employee_id is required for the {self.role} role")
        if self.role == "hr" and self.employee_id is not None:
            raise ValueError("employee_id must not be supplied for the hr role")
        return self


class AuthPrincipal(BaseModel):
    role: AuthRole
    employee_id: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: AuthRole
    employee_id: str | None = None


class AuthError(ValueError):
    """Raised when a bearer token cannot be trusted."""


class AuthService:
    """Issue and verify compact HMAC-signed demo tokens without external state."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("CAREER_QUEST_SECRET must not be empty")
        self._secret = secret.encode("utf-8")

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        try:
            decoded = base64.b64decode(
                value + padding,
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError, TypeError) as exc:
            raise AuthError("Invalid authentication token") from exc
        if AuthService._encode(decoded) != value:
            raise AuthError("Invalid authentication token")
        return decoded

    def issue(self, principal: AuthPrincipal) -> str:
        payload = {
            "version": 1,
            "role": principal.role,
            "employee_id": principal.employee_id,
        }
        encoded = self._encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{self._encode(signature)}"

    def verify(self, token: str) -> AuthPrincipal:
        try:
            encoded, supplied_signature = token.split(".", maxsplit=1)
        except ValueError as exc:
            raise AuthError("Invalid authentication token") from exc
        expected_signature = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(
            expected_signature,
            self._decode(supplied_signature),
        ):
            raise AuthError("Invalid authentication token")
        try:
            payload = json.loads(self._decode(encoded))
            if payload.pop("version", None) != 1:
                raise AuthError("Unsupported authentication token")
            return AuthPrincipal.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            if isinstance(exc, AuthError):
                raise
            raise AuthError("Invalid authentication token") from exc
