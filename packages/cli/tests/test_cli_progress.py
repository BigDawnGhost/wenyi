"""Terminal progress clocks, counts and stage presentation contracts."""

import unittest

from rich.cells import cell_len
from rich.progress import Progress
from wenyi_cli.commands.progress import (
    RichProgressBridge,
    WorkflowElapsedColumn,
    progress_columns,
)


class TestCliProgress(unittest.TestCase):
    def test_progress_clock_advances_after_completed_stage(self):
        now = 10.0
        progress = Progress(disable=True, get_time=lambda: now)
        bridge = RichProgressBridge(progress, "Preparing translation…")
        bridge(1674, 1674, "Translation complete")
        self.assertTrue(progress.tasks[0].finished)

        now = 20.0
        bridge(0, 1674, "Whole-book review R1")
        bridge(777, 1674, "Whole-book review R1")
        task = progress.tasks[0]
        self.assertFalse(task.finished)
        now = 25.0
        self.assertEqual(WorkflowElapsedColumn().render(task).plain, "0:00:15")
        now = 35.0
        self.assertEqual(WorkflowElapsedColumn().render(task).plain, "0:00:25")
        bridge(778, 1674, "Whole-book review R1")
        self.assertEqual(WorkflowElapsedColumn().render(task).plain, "0:00:25")

    def test_indeterminate_progress_updates_preserve_elapsed_time(self):
        now = 10.0
        progress = Progress(disable=True, get_time=lambda: now)
        bridge = RichProgressBridge(progress, "Preparing review…")
        bridge(1, 1, "Loading review chapters")
        now = 12.0
        bridge(0, 0, "Restoring review checkpoint…")
        now = 15.0
        bridge(0, 0, "Restoring review checkpoint…")
        task = progress.tasks[0]
        self.assertIsNone(task.total)
        self.assertFalse(task.finished)
        self.assertEqual(WorkflowElapsedColumn().render(task).plain, "0:00:05")

    def test_progress_clock_keeps_running_while_completed_stage_waits(self):
        now = 10.0
        progress = Progress(disable=True, get_time=lambda: now)
        bridge = RichProgressBridge(progress, "Preparing…")
        now = 15.0
        bridge(2, 2, "Translating chapter 1")
        now = 25.0
        self.assertEqual(WorkflowElapsedColumn().render(progress.tasks[0]).plain, "0:00:15")
        bridge(1, 3, "Translating chapter 2")
        now = 30.0
        self.assertEqual(WorkflowElapsedColumn().render(progress.tasks[0]).plain, "0:00:20")

    def test_long_progress_description_is_ellipsized_without_hiding_bar(self):
        progress = Progress(*progress_columns(), disable=True)
        bridge = RichProgressBridge(progress, "Preparing…")

        bridge(1, 2, "这是一个特别特别长而且不应该挤掉右侧进度条的章节标题")

        description = progress.tasks[0].description
        self.assertTrue(description.endswith("…"))
        self.assertLessEqual(cell_len(description), 28)

    def test_progress_bridge_reuses_one_task_across_review_stages(self):
        progress = Progress(disable=True)
        bridge = RichProgressBridge(progress, "Preparing whole-book review…")

        bridge(0, 6386, "Whole-book review R1")
        bridge(6386, 6386, "Whole-book review R1")
        bridge(0, 58, "Shadow revision R1")
        bridge(58, 58, "Shadow revision R1")
        bridge(0, 6386, "Blind whole-book review R2")

        self.assertEqual(len(progress.tasks), 1)
        task = progress.tasks[0]
        self.assertEqual(task.description, "Blind whole-book review R2")
        self.assertEqual(task.completed, 0)
        self.assertEqual(task.total, 6386)
        self.assertFalse(task.finished)
