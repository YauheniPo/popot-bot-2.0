#!/usr/bin/env python3
"""Apply small, version-checked local patches to the installed Hermes code.

Each patch is a marker plus an exact old/new source pair. The script is safe
to run on every deploy: when the marker is already present it is a no-op. A
missing target or changed upstream source fails the deployment so a required
command cannot silently disappear. Nothing is written unless the old code
matches exactly.

Covered customizations (not yet upstream):
 * gateway commands: /gw-restart (canonical, with /restart and /gw_restart
   aliases so the Telegram menu entry resolves) and /model_global
 * /status shows reasoning effort, visibility, global + topic model
 * busy-session dispatch handles /gw-restart like /restart
The existing Edge TTS retry lives in ops/apply-edge-tts-retry.py and is not
touched here.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys


HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_INSTALL_DIR", str(Path.home() / ".hermes" / "hermes-agent"))
)

_PREFIX = "# Local Hermes:"
# Construct the retired spelling without advertising it as a supported slash
# command. It is needed only to migrate files patched by earlier deployments.
_RETIRED_MODEL_GLOBAL = "model" + chr(45) + "global"
_GW_RESTART = "gw" + chr(45) + "restart"


class PatchMigrationError(RuntimeError):
    """Raised when an installed local patch has an unknown legacy shape."""


def _replace_required(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise PatchMigrationError(f"cannot migrate {label}: expected code is missing")
    return source.replace(old, new, 1)


def _migrate_model_global_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Migrate earlier local command patches to the underscore-only command."""
    retired = _RETIRED_MODEL_GLOBAL
    retired_marker = f"{_PREFIX} {retired}"
    if retired_marker not in source:
        return source, False

    if relative_path == "hermes_cli/commands.py":
        old = f'''    # Local Hermes: {retired} CommandDef
    CommandDef("{retired}", "Set the global default model for all topics/sessions", "Configuration",
               aliases=("model_global",),
               args_hint="[model] [--provider name]",
               busy_policy="reject", busy_handler="{retired}"),
'''
        new = '''    # Local Hermes: model_global CommandDef
    CommandDef("model_global", "Set the global default model for all topics/sessions", "Configuration",
               args_hint="[model] [--provider name]",
               busy_policy="reject", busy_handler="model"),
'''
        migrated = _replace_required(source, old, new, "model_global CommandDef")
    elif relative_path == "gateway/slash_commands.py":
        legacy_usage = f'''        if not raw_args:
            return (
                "Usage: /{retired} <model> [--provider <provider>]\\n"
                "Sets the global default model in config.yaml — applies to every "
                "topic/session, not just this one."
            )
'''
        picker_behavior = '''        # No args: open the interactive model picker with --global flag so the
        # chosen model persists to config.yaml for all topics/sessions.
        if not raw_args:
            event.text = "/model --global"
            return await self._handle_model_command(event)
'''
        if legacy_usage in source:
            migrated = source.replace(legacy_usage, picker_behavior, 1)
        elif picker_behavior in source:
            migrated = source
        else:
            raise PatchMigrationError(
                "cannot migrate model_global handler: unknown no-argument behavior"
            )
        migrated = _replace_required(
            migrated,
            f'"""Handle /{retired} —',
            '"""Handle /model_global —',
            "model_global handler docstring",
        )
        migrated = _replace_required(
            migrated,
            f"# Local Hermes: {retired} handler",
            "# Local Hermes: model_global handler",
            "model_global handler marker",
        )
    elif relative_path == "gateway/run.py":
        old = f'''        if canonical in ("{retired}", "model_global"):
            # Local Hermes: {retired} route
            return await self._handle_model_global_command(event)
'''
        new = '''        if canonical == "model_global":
            # Local Hermes: model_global route
            return await self._handle_model_global_command(event)
'''
        migrated = _replace_required(source, old, new, "model_global route")
    else:
        return source, False

    if retired_marker in migrated:
        raise PatchMigrationError(
            f"cannot migrate {relative_path}: retired local patch marker remains"
        )
    return migrated, True


def _migrate_installed_model_global() -> int:
    planned_writes: list[tuple[Path, str]] = []
    for relative_path in (
        "hermes_cli/commands.py",
        "gateway/slash_commands.py",
        "gateway/run.py",
    ):
        target = HERMES_AGENT_DIR / relative_path
        if not target.is_file():
            continue
        source = target.read_text(encoding="utf-8")
        migrated, changed = _migrate_model_global_source(relative_path, source)
        if changed:
            planned_writes.append((target, migrated))

    for target, migrated in planned_writes:
        target.write_text(migrated, encoding="utf-8")
        print(f"[hermes-patch] migrated {target.relative_to(HERMES_AGENT_DIR)}")
    return len(planned_writes)


def _migrate_gw_restart_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Migrate the unmarked /gw-restart blocks produced by an earlier patch."""
    if relative_path == "hermes_cli/commands.py":
        legacy = re.compile(
            r'^    CommandDef\("(?P<command>restart|gw-restart)", "Gracefully restart the gateway after draining active runs", "Session",\n'
            r'               gateway_only=True, busy_policy="dispatch", aliases=\((?P<aliases>[^)]*)\)\),\n',
            re.MULTILINE,
        )
        new = '''    # Local Hermes: gw-restart canonical
    CommandDef("gw-restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("restart", "gw_restart")),
'''
        match = legacy.search(source)
        if match is None:
            return source, False
        aliases = set(re.findall(r'"([A-Za-z_-]+)"', match.group("aliases")))
        command = match.group("command")
        if command == "restart":
            known_legacy_shape = (
                bool(aliases.intersection({_GW_RESTART, "gw_restart"}))
                and aliases <= {_GW_RESTART, "gw_restart"}
            )
        else:
            known_legacy_shape = "restart" in aliases and aliases <= {"restart", "gw_restart"}
        if not known_legacy_shape:
            return source, False
        return source[:match.start()] + new + source[match.end():], True
    elif relative_path == "gateway/run.py":
        legacy = re.compile(
            r'^        if canonical in \((?P<aliases>[^)]*)\):\n'
            r'(?:            #[^\n]*\n)*'
            r'            return await self\._handle_restart_command\(event\)\n',
            re.MULTILINE,
        )
        new = '''        if canonical in ("restart", "gw-restart"):
            # Local Hermes: gw-restart route
            return await self._handle_restart_command(event)
'''
    else:
        return source, False

    match = legacy.search(source)
    if match is None:
        return source, False
    aliases = set(re.findall(r'"([A-Za-z_-]+)"', match.group("aliases")))
    if not ({"restart", _GW_RESTART} <= aliases <= {"restart", _GW_RESTART, "gw_restart"}):
        return source, False
    return source[:match.start()] + new + source[match.end():], True


def _migrate_installed_gw_restart() -> int:
    planned_writes: list[tuple[Path, str]] = []
    for relative_path in ("hermes_cli/commands.py", "gateway/run.py"):
        target = HERMES_AGENT_DIR / relative_path
        if not target.is_file():
            continue
        migrated, changed = _migrate_gw_restart_source(
            relative_path, target.read_text(encoding="utf-8")
        )
        if changed:
            planned_writes.append((target, migrated))

    for target, migrated in planned_writes:
        target.write_text(migrated, encoding="utf-8")
        print(f"[hermes-patch] migrated {target.relative_to(HERMES_AGENT_DIR)}")
    return len(planned_writes)


_PATCHES: list[tuple[str, str, str, str]] = [
    # NOTE: every ``new`` block MUST include its marker as a comment line so
    # the idempotency check (marker already present -> skip) works on re-run.
    (
        "hermes_cli/commands.py",
        _PREFIX + " model_global CommandDef",
        '''    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model"),
''',
        '''    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model"),
    # Local Hermes: model_global CommandDef
    CommandDef("model_global", "Set the global default model for all topics/sessions", "Configuration",
               args_hint="[model] [--provider name]",
               busy_policy="reject", busy_handler="model"),
''',
    ),
    (
        "hermes_cli/commands.py",
        _PREFIX + " gw-restart canonical",
        '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch"),
''',
        '''    # Local Hermes: gw-restart canonical
    CommandDef("gw-restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("restart", "gw_restart")),
''',
    ),
    (
        "gateway/slash_commands.py",
        _PREFIX + " model_global handler",
        '''    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model.
''',
        '''    async def _handle_model_global_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model_global — switch model persistently for ALL topics/sessions.

        Thin wrapper: rewrites the incoming command text to
        ``/model <args> --global`` and delegates to the standard /model
        pipeline so parsing, provider resolution, and config persistence
        stay in one place (hermes_cli.model_switch.switch_model).
        """
        raw_args = event.get_command_args().strip()
        # No args: open the interactive model picker with --global flag so the
        # chosen model persists to config.yaml for all topics/sessions.
        if not raw_args:
            event.text = "/model --global"
            return await self._handle_model_command(event)
        # Keep the leading "/" on the rewritten text: get_command_args()
        # only splits arguments when is_command() sees a leading "/", so a
        # bare "model ..." text makes _handle_model_command read the whole
        # string (command word included) as the model name and fail with
        # "Model names cannot contain spaces."
        if "--global" in raw_args:
            event.text = f"/model {raw_args}"
        else:
            event.text = f"/model {raw_args} --global"
        return await self._handle_model_command(event)

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model.
        # Local Hermes: model_global handler
''',
    ),
    (
        "gateway/run.py",
        _PREFIX + " model_global route",
        '''        if canonical == "model":
            return await self._handle_model_command(event)
''',
        '''        if canonical == "model":
            return await self._handle_model_command(event)

        if canonical == "model_global":
            # Local Hermes: model_global route
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
            # Local Hermes: gw-restart route
            return await self._handle_restart_command(event)
''',
    ),
    (
        "gateway/run.py",
        _PREFIX + " gw-restart busy map",
        '''                "restart": self._handle_restart_command,
''',
        '''                "restart": self._handle_restart_command,
                "gw-restart": self._handle_restart_command,  # Local Hermes: gw-restart busy map
''',
    ),
    (
        "gateway/slash_commands.py",
        _PREFIX + " status reasoning",
        '''            t("gateway.status.agent_running", state=t("gateway.status.state_yes") if is_running else t("gateway.status.state_no")),
        ])
''',
        '''            t("gateway.status.agent_running", state=t("gateway.status.state_yes") if is_running else t("gateway.status.state_no")),
        ])
        # Local Hermes: status reasoning
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
        # Local Hermes: model info (global + topic)
        try:
            # Global default = config.yaml model.default (single source of truth).
            global_model = _resolve_gateway_model(user_config) if user_config else _resolve_gateway_model()
            # Session override = /model <name> stored for this topic (if any).
            session_model = (getattr(self, "_session_model_overrides", None) or {}).get(
                str(session_key or "")
            )
            # Topic model = what this topic actually runs with right now:
            # the /model override if set, otherwise the live/cached agent's
            # runtime model, otherwise the global default.
            live_agent_model = ""
            if status_agent is not None and status_agent is not _AGENT_PENDING_SENTINEL:
                live_agent_model = _clean_str(getattr(status_agent, "model", ""))
            topic_model = session_model or live_agent_model or global_model
            lines.append(f"**Global model:** {global_model}")
            lines.append(f"**Topic model:** {topic_model}" + (" *(override)*" if session_model else ""))
        except Exception:
            pass
''',
    ),
]


def main() -> int:
    if not HERMES_AGENT_DIR.is_dir():
        print(
            f"[hermes-patch] ERROR: Hermes install directory is missing: {HERMES_AGENT_DIR}",
            file=sys.stderr,
        )
        return 1
    try:
        migrated = _migrate_installed_model_global() + _migrate_installed_gw_restart()
    except PatchMigrationError as exc:
        print(f"[hermes-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    applied = 0
    failures: list[str] = []
    for relative_path, marker, old, new in _PATCHES:
        target = HERMES_AGENT_DIR / relative_path
        if not target.is_file():
            print(f"[hermes-patch] ERROR: {relative_path}: file missing", file=sys.stderr)
            failures.append(relative_path)
            continue
        source = target.read_text(encoding="utf-8")
        if marker in source:
            print(f"[hermes-patch] {relative_path}: already applied")
            continue
        if old not in source:
            print(
                f"[hermes-patch] ERROR: {relative_path} does not match the expected "
                "code. Hermes may have changed — re-verify the patch "
                "before relying on /model_global, /gw-restart, or /status reasoning.",
                file=sys.stderr,
            )
            failures.append(relative_path)
            continue
        target.write_text(source.replace(old, new, 1), encoding="utf-8")
        print(f"[hermes-patch] applied {relative_path}")
        applied += 1
    if applied or migrated:
        print("[hermes-patch] changed")
    if failures:
        print(
            "[hermes-patch] ERROR: required patches were not applied to: "
            + ", ".join(sorted(set(failures))),
            file=sys.stderr,
        )
        return 1
    print(
        f"[hermes-patch] done: {applied} patch(es) applied, "
        f"{migrated} legacy patch file(s) migrated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
