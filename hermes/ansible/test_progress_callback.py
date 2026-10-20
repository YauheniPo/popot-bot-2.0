"""Tests for the compact Ansible task-progress callback."""

import re
import unittest
from types import SimpleNamespace
from unittest import mock

from hermes.ansible.callback_plugins.progress import CallbackModule


class _Display:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def display(self, message: str) -> None:
        self.messages.append(message)


class _Task:
    implicit = False
    _uuid = "task-uuid"

    @staticmethod
    def get_name() -> str:
        return "Install Tailscale"


class ProgressCallbackTests(unittest.TestCase):
    def test_measured_slowest_tasks_include_handlers_without_result_data(self):
        callback = CallbackModule()
        display = _Display()
        callback._display = display
        callback._planned_tasks = 1
        callback._task_positions = {_Task._uuid: 1}
        handler = SimpleNamespace(implicit=False, _uuid='handler', get_name=lambda: 'Restart gateway')
        host = SimpleNamespace(get_name=lambda: 'fixture')
        result = lambda task: SimpleNamespace(task=task, host=host, _task=task, _host=host,
                                              result={'stdout': 'test-only-secret'})
        with mock.patch('time.monotonic') as clock:
            clock.return_value = 10
            callback.v2_playbook_on_task_start(_Task(), is_conditional=False)
            clock.return_value = 12
            callback.v2_runner_on_ok(result(_Task()))
            clock.return_value = 20
            callback.v2_playbook_on_handler_task_start(handler)
            clock.return_value = 25
            callback.v2_runner_on_ok(result(handler))
            callback.v2_playbook_on_stats(None)
        output = '\n'.join(display.messages)
        self.assertIn('5.00s Restart gateway', output)
        self.assertIn('2.00s Install Tailscale', output)
        self.assertLess(output.index('5.00s'), output.index('2.00s'))
        self.assertNotIn('test-only-secret', output)

    def test_task_start_includes_local_start_time(self) -> None:
        callback = CallbackModule()
        display = _Display()
        callback._display = display
        callback._planned_tasks = 1
        callback._task_positions = {_Task._uuid: 1}

        callback.v2_playbook_on_task_start(_Task(), is_conditional=False)

        self.assertRegex(
            display.messages[0],
            re.compile(r"^\[progress 1/1\] \[\d{2}:\d{2}:\d{2}\] Install Tailscale$"),
        )


if __name__ == "__main__":
    unittest.main()
