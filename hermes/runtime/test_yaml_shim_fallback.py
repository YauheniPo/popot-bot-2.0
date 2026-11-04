"""Standalone scripts fall back to the bundled ``hermes_yaml`` shim without PyYAML."""

from __future__ import annotations

import importlib.abc
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = {
    "hermes_config_io": ROOT / "ops" / "hermes_config_io.py",
    "configure_plugin": ROOT / "ops" / "configure-plugin.py",
    "configure_superpowers": ROOT / "ops" / "configure-superpowers.py",
    "fallback_policy": ROOT / "runtime" / "fallback-policy.py",
    "apply_config": ROOT / "runtime" / "apply-config.py",
    "workspace_swarm": ROOT / "runtime" / "workspace-swarm.py",
    "competitor_digest": ROOT / "runtime" / "competitor_digest.py",
}


class _MissingPyYAML(importlib.abc.MetaPathFinder):
    """Fail ``import yaml`` exactly like a host without PyYAML installed."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102 - protocol
        if fullname == "yaml" or fullname.startswith("yaml."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def _shim() -> types.ModuleType:
    shim = types.ModuleType("hermes_yaml")
    shim.YAMLError = type("YAMLError", (Exception,), {})
    shim.safe_load = mock.Mock(return_value={"shim": True})
    shim.safe_dump = mock.Mock(side_effect=lambda data, stream, **kwargs: stream.write("shim: true\n"))
    return shim


class StandaloneYamlFallbackTests(unittest.TestCase):
    def load_script(self, name: str):
        """Import a standalone script while PyYAML is unavailable."""
        path = SCRIPTS[name]
        shim = _shim()
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        finder = _MissingPyYAML()
        saved = {key: sys.modules.pop(key, None) for key in (name, "yaml", "hermes_yaml", "hermes_config_io")}
        sys.modules["hermes_yaml"] = shim
        sys.meta_path.insert(0, finder)
        with mock.patch.object(sys, "path", [str(path.parent), str(ROOT / "ops"), str(ROOT / "runtime"), *sys.path]):
            try:
                spec.loader.exec_module(module)
            finally:
                sys.meta_path.remove(finder)
                for key in (name, "hermes_yaml", "hermes_config_io"):
                    sys.modules.pop(key, None)
                for key, value in saved.items():
                    if value is not None:
                        sys.modules[key] = value
        return module, shim

    def test_standalone_scripts_bind_the_bundled_yaml_shim(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                module, shim = self.load_script(name)
                self.assertIs(module.yaml, shim)

    def test_config_io_reads_and_writes_through_the_shim_when_pyyaml_is_missing(self):
        module, shim = self.load_script("hermes_config_io")
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.yaml"
            config.write_text("stub: true\n", encoding="utf-8")
            self.assertEqual(module.load_config(config), {"shim": True})
            module.write_config(config, {"shim": True})
            self.assertEqual(config.read_text(encoding="utf-8"), "shim: true\n")
        shim.safe_load.assert_called_once()
        shim.safe_dump.assert_called_once()

if __name__ == "__main__":
    unittest.main()
