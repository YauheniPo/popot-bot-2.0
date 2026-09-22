"""Run the dependency-free Node bridge tests from the repository Python suite."""

from pathlib import Path
import shutil
import subprocess
import unittest


class WorkspaceDashboardBridgeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the Workspace bridge tests')
    def test_dashboard_session_bridge(self):
        result = subprocess.run(
            ['node', '--test', str(Path(__file__).with_name('test-workspace-dashboard-bridge.mjs')),
             str(Path(__file__).with_name('test-workspace-mcp-adapter.mjs')),
             str(Path(__file__).with_name('test-workspace-mcp-ui.mjs'))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
