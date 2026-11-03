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

    def test_play_start_counts_static_tasks_and_reports_total_time(self) -> None:
        callback = CallbackModule()
        display = _Display()
        callback._display = display
        block = SimpleNamespace(get_tasks=lambda: [_Task()])
        implicit = SimpleNamespace(implicit=True, _uuid='implicit')
        plain = SimpleNamespace(implicit=False, _uuid='plain', get_name=lambda: 'Plain task')
        play = SimpleNamespace(compile=lambda: [block, implicit, plain])
        with mock.patch('time.monotonic') as clock:
            clock.return_value = 100
            callback.v2_playbook_on_play_start(play)
            clock.return_value = 150
            callback.v2_playbook_on_stats(None)
        output = '\n'.join(display.messages)
        self.assertIn('[timing] total 50.00s', output)
        self.assertNotIn('Added', output)

    def test_dynamic_tasks_are_numbered_and_summarized(self) -> None:
        callback = CallbackModule()
        display = _Display()
        callback._display = display
        dynamic = SimpleNamespace(implicit=False, _uuid='dynamic', get_name=lambda: 'Included task')
        with mock.patch('time.monotonic') as clock:
            clock.return_value = 1
            callback.v2_playbook_on_task_start(dynamic, is_conditional=False)
            clock.return_value = 2
            callback.v2_playbook_on_stats(None)
        output = '\n'.join(display.messages)
        self.assertIn('[progress 1/1]', output)
        self.assertIn('Added 1 dynamically included task(s)', output)

    def test_failed_and_unreachable_results_are_timed(self) -> None:
        callback = CallbackModule()
        display = _Display()
        callback._display = display
        host = SimpleNamespace(get_name=lambda: 'fixture')
        failed = SimpleNamespace(implicit=False, _uuid='fail-task', get_name=lambda: 'Failing task')
        unreachable = SimpleNamespace(implicit=False, _uuid='unreach-task',
                                      get_name=lambda: 'Unreachable task')
        with mock.patch('time.monotonic') as clock:
            clock.return_value = 5
            callback.v2_playbook_on_task_start(failed, is_conditional=False)
            clock.return_value = 7
            callback.v2_runner_on_failed(SimpleNamespace(_task=failed, _host=host))
            clock.return_value = 8
            callback.v2_playbook_on_task_start(unreachable, is_conditional=False)
            clock.return_value = 9
            callback.v2_runner_on_unreachable(SimpleNamespace(_task=unreachable, _host=host))
            clock.return_value = 10
            callback.v2_playbook_on_stats(None)
        output = '\n'.join(display.messages)
        self.assertIn('[timing] 2.00s Failing task (fixture)', output)
        self.assertIn('[timing] 1.00s Unreachable task (fixture)', output)

    def test_results_for_unknown_tasks_are_not_timed(self) -> None:
        callback = CallbackModule()
        display = _Display()
        callback._display = display
        task = SimpleNamespace(_uuid='never-started', get_name=lambda: 'Never started')
        host = SimpleNamespace(get_name=lambda: 'fixture')
        callback.v2_runner_on_ok(SimpleNamespace(_task=task, _host=host))
        callback.v2_playbook_on_stats(None)
        self.assertEqual(display.messages, [])

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
