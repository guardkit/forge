"""The coordinator's read-only answer about its own record.

One small service whose whole job is to answer two questions about what the
coordinator wrote down, and to answer nothing else. See
:mod:`forge.record_answer.service`.
"""

from __future__ import annotations

from forge.record_answer.service import (
    ANSWER_ROUTE,
    RecordAnswerHandler,
    TheCoordinatorsRecord,
    TheRecordIsUnreadable,
    serve,
)

__all__ = [
    "ANSWER_ROUTE",
    "RecordAnswerHandler",
    "TheCoordinatorsRecord",
    "TheRecordIsUnreadable",
    "serve",
]
