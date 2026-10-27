from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ApiModel, RequestModel


class RegisterRequest(RequestModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    name: str | None = Field(default=None, max_length=255)

    @field_validator("password")
    @classmethod
    def not_trivially_weak(cls, v: str) -> str:
        if v.lower() in {"password12", "password123", "1234567890"}:
            raise ValueError("password is too common")
        return v


class LoginRequest(RequestModel):
    email: EmailStr
    password: str


class RefreshRequest(RequestModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(ApiModel):
    id: str
    email: str
    name: str | None
    created_at: datetime


class ProjectCreate(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    environment: Literal["development", "production"] = "development"


class ProjectUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class ProjectOut(ApiModel):
    id: str
    name: str
    environment: str
    created_at: datetime


class ApiKeyCreate(RequestModel):
    name: str = Field(default="Default", max_length=255)


class ApiKeyOut(ApiModel):
    id: str
    project_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    """The only response that ever carries the plaintext secret."""

    secret: str
