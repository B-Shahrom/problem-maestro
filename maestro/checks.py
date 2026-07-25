"""Shared result type for the pre-ingest checks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    severity: Severity
    message: str
    slug: str | None = None

    def __str__(self) -> str:
        where = f" [{self.slug}]" if self.slug else ""
        return f"{self.check} {self.severity.upper()}{where}: {self.message}"


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity is Severity.ERROR]


def ok(findings: list[Finding]) -> bool:
    return not errors(findings)
