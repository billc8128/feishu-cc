"""Context-local execution metadata for agent runs."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from contextvars import ContextVar
from typing import Iterator, Optional


@dataclass(frozen=True)
class TaskContext:
    source: str = "chat"
    task_id: Optional[str] = None


_current_task_context: ContextVar[TaskContext] = ContextVar(
    "current_task_context", default=TaskContext()
)


def get_current_task_context() -> TaskContext:
    return _current_task_context.get()


@contextmanager
def use_task_context(
    *, source: str = "chat", task_id: Optional[str] = None
) -> Iterator[None]:
    token = _current_task_context.set(TaskContext(source=source, task_id=task_id))
    try:
        yield
    finally:
        _current_task_context.reset(token)
