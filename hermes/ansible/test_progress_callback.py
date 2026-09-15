"""Tests for the compact Ansible task-progress callback."""

import re
import unittest

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
