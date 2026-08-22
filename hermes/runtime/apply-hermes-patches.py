#!/usr/bin/env python3
"""Apply small, version-checked local patches to the installed Hermes code.

Each patch is a marker plus an exact old/new source pair. The script is safe
to run on every deploy: when the marker is already present it is a no-op; when
the expected old code is missing (upstream changed) the patch is skipped with
a warning instead of breaking the deploy. Nothing is written unless the old
code matches exactly.

Covered customizations (not yet upstream):
 * gateway commands: /gw-restart alias for /restart and /model-global
 * /status shows reasoning effort and visibility
The existing Edge TTS retry lives in ops/apply-edge-tts-retry.py and is not
touched here.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_INSTALL_DIR", str(Path.home() / ".hermes" / "hermes-agent"))
)

_PREFIX = "# Local Hermes:"

_PATCHES: list[tuple[str, str, str, str]] = [
    (
        "hermes_cli/commands.py",
        _PREFIX + " model-global CommandDef",
        '''    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model"),
''',
        '''    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model"),
    # Local Hermes: model-global alias for /model <name> --global
    CommandDef("model-global", "Set the global default model for all topics/sessions", "Configuration",
               aliases=("model_global",),
               args_hint="[model] [--provider name]",
               busy_policy="reject", busy_handler="model-global"),
''',
    ),
    (
        "hermes_cli/commands.py",
        _PREFIX + " gw-restart alias",
        '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch"),
''',
        '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("gw-restart",)),
''',
    ),
    (
        "gateway/slash_commands.py",
        _PREFIX + " model-global handler",
        '''    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model.
''',
        '''    async def _handle_model_global_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model-global — switch model persistently for ALL topics/sessions.

        Thin wrapper: rewrites the incoming command text to
        ``model <args> --global`` and delegates to the standard /model
        pipeline so parsing, provider resolution, and config persistence
        stay in one place (hermes_cli.model_switch.switch_model).
        """
        raw_args = event.get_command_args().strip()
        if not raw_args:
            return (
                "Usage: /model-global <model> [--provider <provider>]\\n"
                "Sets the global default model in config.yaml — applies to every "
                "topic/session, not just this one."
            )
        if "--global" in raw_args:
            event.text = f"model {raw_args}"
        else:
            event.text = f"model {raw_args} --global"
        return await self._handle_model_command(event)

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model.
''',
    ),
    (
        "gateway/run.py",
        _PREFIX + " model-global route",
        '''        if canonical == "model":
            return await self._handle_model_command(event)
''',
        '''        if canonical == "model":
            return await self._handle_model_command(event)

        if canonical in ("model-global", "model_global"):
            # Alias for /model <name> --global: persist to config.yaml so the
            # switch applies to every topic/session, not just this one.
            return await self._handle_model_global_command(event)
''',
    ),
    (
        "gateway/run.py",
        _PREFIX + " gw-restart route",
        '''        if canonical == "restart":
            return await self._handle_restart_command(event)
''',
        '''        if canonical in ("restart", "gw-restart"):
            # /gw-restart is the user-facing alias; /restart is kept for
            # backward compatibility with scripts that already use it.
            return await self._handle_restart_command(event)
''',
    ),
    (
        "gateway/slash_commands.py",
        _PREFIX + " status reasoning",
        '''            t("gateway.status.agent_running", state=t("gateway.status.state_yes") if is_running else t("gateway.status.state_no")),
''',
        '''            t("gateway.status.agent_running", state=t("gateway.status.state_yes") if is_running else t("gateway.status.state_no")),
        # Local Hermes: report reasoning effort and whether it is shown.
        reasoning_cfg = getattr(self, "_reasoning_config", None)
        if isinstance(reasoning_cfg, dict) and reasoning_cfg.get("enabled") is False:
            reasoning_effort = "disabled"
        elif isinstance(reasoning_cfg, dict):
            reasoning_effort = str(reasoning_cfg.get("effort") or "default")
        else:
            reasoning_effort = "default (provider)"
        show_reasoning = bool(getattr(self, "_show_reasoning", False))
        lines.extend([
            f"**Reasoning:** {reasoning_effort}",
            f"**Show reasoning:** {'on' if show_reasoning else 'off'}",
        ])
''',
    ),
]


def main() -> int:
    if not HERMES_AGENT_DIR.is_dir():
        print(
            f"[hermes-patch] skipped: Hermes install directory is missing: {HERMES_AGENT_DIR}",
            file=sys.stderr,
        )
        return 0
    applied = 0
    for relative_path, marker, old, new in _PATCHES:
        target = HERMES_AGENT_DIR / relative_path
        if not target.is_file():
            print(f"[hermes-patch] skipped {relative_path}: file missing")
            continue
        source = target.read_text(encoding="utf-8")
        if marker in source:
            print(f"[hermes-patch] {relative_path}: already applied")
            continue
        if old not in source:
            print(
                f"[hermes-patch] WARNING: {relative_path} does not match the expected "
                "code; skipping. Hermes may have changed — re-verify the patch "
                "before relying on /model-global, /gw-restart, or /status reasoning.",
                file=sys.stderr,
            )
            continue
        target.write_text(source.replace(old, new, 1), encoding="utf-8")
        print(f"[hermes-patch] applied {relative_path}")
        applied += 1
    print(f"[hermes-patch] done: {applied} patch(es) applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())