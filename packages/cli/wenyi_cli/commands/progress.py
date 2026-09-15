"""Render workflow stages without resetting the overall invocation clock."""

from rich.cells import cell_len, set_cell_size
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column
from rich.text import Text
from wenyi_core.timing import format_duration

_PROGRESS_DESCRIPTION_WIDTH = 28


def _short_progress_description(label: str) -> str:
    """Truncate long titles by terminal cell width and keep an ellipsis when clipped."""
    if cell_len(label) <= _PROGRESS_DESCRIPTION_WIDTH:
        return label
    prefix = set_cell_size(label, _PROGRESS_DESCRIPTION_WIDTH - 1).rstrip()
    return f"{prefix}…"


class WorkflowElapsedColumn(TimeElapsedColumn):
    """Keep elapsed time advancing across stages, including completed-stage waits."""

    def render(self, task: Task) -> Text:
        started = task.fields.get("workflow_started", task.start_time)
        elapsed = 0.0 if started is None else task.get_time() - started
        return Text(format_duration(elapsed), style="progress.elapsed")


def progress_columns() -> tuple[ProgressColumn, ...]:
    """Build Rich columns that keep long stage names from hiding the bar."""
    description_column = Column(
        max_width=_PROGRESS_DESCRIPTION_WIDTH,
        no_wrap=True,
        overflow="ellipsis",
    )
    return (
        SpinnerColumn(),
        TextColumn(
            "[progress.description]{task.description}",
            table_column=description_column,
        ),
        BarColumn(),
        MofNCompleteColumn(),
        WorkflowElapsedColumn(),
    )


class RichProgressBridge:
    """Map pipeline stage progress onto one Rich task."""

    def __init__(self, progress: Progress, initial_description: str) -> None:
        self.progress = progress
        self._started = progress.get_time()
        self.task = progress.add_task(
            _short_progress_description(initial_description),
            total=None,
            workflow_started=self._started,
        )
        self._stage: tuple[str, int | None] = (initial_description, None)

    def __call__(self, done: int, total: int, label: str) -> None:
        """Refresh the current stage and counts without accumulating progress bars."""
        stage = (label, total if total > 0 else None)
        short = _short_progress_description(label)
        if total > 0:
            if stage != self._stage:
                # A completed Rich task retains its finished time until reset.
                self.progress.reset(self.task, total=total, completed=done, description=short)
            self._stage = stage
            self.progress.update(
                self.task,
                completed=done,
                total=total,
                description=short,
            )
            return
        if stage == self._stage:
            return
        # Rich update(total=None) leaves the total unchanged. Recreate the task
        # to restore indeterminate progress and clear the previous stage’s counts.
        self.progress.remove_task(self.task)
        self.task = self.progress.add_task(short, total=None, workflow_started=self._started)
        self._stage = stage
