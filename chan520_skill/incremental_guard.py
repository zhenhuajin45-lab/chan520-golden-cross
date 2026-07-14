from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable


class FutureDataAccessViolation(RuntimeError):
    """Raised when an incremental session attempts to read data after its clock."""


@dataclass
class DataAccessGuard:
    session_date: date
    violations: list[dict[str, str]] = field(default_factory=list)

    def assert_not_future(self, value: date | str | None, *, source: str, code: str = "") -> None:
        if value is None:
            return
        checked = date.fromisoformat(value) if isinstance(value, str) else value
        if checked > self.session_date:
            violation = {
                "source": source,
                "code": code,
                "date": checked.isoformat(),
                "session_date": self.session_date.isoformat(),
                "error_code": "FUTURE_DATA_ACCESS_VIOLATION",
            }
            self.violations.append(violation)
            raise FutureDataAccessViolation(
                f"{source} accessed {checked.isoformat()} after session {self.session_date.isoformat()}"
            )

    def assert_rows_not_future(
        self,
        rows: Iterable[Any],
        *,
        source: str,
        code_attr: str = "code",
        date_attr: str = "date",
    ) -> None:
        for row in rows:
            row_date = _value(row, date_attr)
            code = str(_value(row, code_attr) or "")
            self.assert_not_future(row_date, source=source, code=code)


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)
