from __future__ import annotations


class DomainError(Exception):
    """Base class for domain/application errors meant for user-facing flows."""


class ReleaseNotFound(DomainError):
    pass


class InvalidCommand(DomainError):
    pass


class TransitionNotFound(DomainError):
    pass


class TransitionNotAllowed(DomainError):
    pass

