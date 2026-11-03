"""Show the current and planned task number during a playbook run."""

from __future__ import annotations

from datetime import datetime
import time

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    """Supplement Ansible's default output with lightweight task progress."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "progress"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        super().__init__()
        self._planned_tasks = 0
        self._started_tasks = 0
        self._task_positions = {}
        self._dynamic_tasks = 0
        self._task_starts = {}
        self._timings = {}
        self._play_started = None

    def v2_playbook_on_play_start(self, play) -> None:
        """Count statically imported tasks before the play starts."""
        if self._play_started is None:
            self._play_started = time.monotonic()
        for entry in play.compile():
            for task in self._tasks_in(entry):
                if task.implicit:
                    continue
                self._planned_tasks += 1
                self._task_positions[task._uuid] = self._planned_tasks

    @staticmethod
    def _tasks_in(entry):
        """Support both Task and Block entries returned by Ansible internals."""
        return entry.get_tasks() if hasattr(entry, "get_tasks") else [entry]

    def v2_playbook_on_task_start(self, task, is_conditional: bool) -> None:
        """Display progress before every user-defined task."""
        if task.implicit:
            return

        self._task_starts[task._uuid] = time.monotonic()
        self._started_tasks += 1
        number = self._task_positions.get(task._uuid)
        if number is None:
            self._dynamic_tasks += 1
            number = self._planned_tasks + self._dynamic_tasks
        total = self._planned_tasks + self._dynamic_tasks
        started_at = datetime.now().strftime("%H:%M:%S")
        self._display.display(
            f"[progress {number}/{total}] [{started_at}] {task.get_name().strip()}"
        )

    def v2_playbook_on_handler_task_start(self, task) -> None:
        self._task_starts[task._uuid] = time.monotonic()

    def _finish(self, result) -> None:
        started = self._task_starts.get(result._task._uuid)
        if started is not None:
            host = result._host.get_name()
            self._timings[result._task._uuid, host] = (
                time.monotonic() - started, result._task.get_name().strip(), host)

    def v2_runner_on_ok(self, result) -> None:
        self._finish(result)

    def v2_runner_on_failed(self, result, ignore_errors=False) -> None:
        self._finish(result)

    def v2_runner_on_unreachable(self, result) -> None:
        self._finish(result)

    def v2_playbook_on_stats(self, stats) -> None:
        """Make it clear when dynamic tasks made the initial count approximate."""
        if self._dynamic_tasks:
            self._display.display(
                "[progress] Added "
                f"{self._dynamic_tasks} dynamically included task(s) to the plan."
            )
        for elapsed, name, host in sorted(self._timings.values(), reverse=True)[:10]:
            self._display.display(f"[timing] {elapsed:.2f}s {name} ({host})")
        if self._play_started is not None:
            self._display.display(f"[timing] total {time.monotonic() - self._play_started:.2f}s")
