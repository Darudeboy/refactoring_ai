from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

T = TypeVar("T")
E = TypeVar("E", bound=BaseException)


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    ok: bool
    value: Optional[T] = None
    error: Optional[BaseException] = None
    message: str = ""

    @staticmethod
    def success(value: T | None = None, message: str = "") -> "Result[T]":
        return Result(ok=True, value=value, error=None, message=message)

    @staticmethod
    def failure(error: BaseException, message: str = "") -> "Result[T]":
        return Result(ok=False, value=None, error=error, message=message or str(error))

