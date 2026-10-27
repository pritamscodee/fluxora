from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for every response model. `from_attributes` lets routers return ORM rows
    directly, and unknown input keys are rejected so typos surface as 422 instead of
    silently doing nothing."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", populate_by_name=True)


class RequestModel(BaseModel):
    """Base for every request body. `extra="forbid"` turns a misspelled field into a 422
    instead of a silently ignored no-op, which is the difference between a client finding
    its bug now and wondering why a setting had no effect."""

    model_config = ConfigDict(extra="forbid")


class Envelope(BaseModel, Generic[T]):
    data: T


class ListParams(BaseModel):
    limit: int = Field(default=25, ge=1, le=100)
    page: int = Field(default=1, ge=1)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


class ErrorBody(BaseModel):
    type: str
    messages: list[str]


class ErrorResponse(BaseModel):
    error: ErrorBody


def envelope(data):
    return {"data": data}
