#!/usr/bin/env python3
"""Apply small, version-checked local patches to the installed Hermes code.

Each patch is a marker plus an exact old/new source pair. The script is safe
to run on every deploy: it records the installed patch fingerprint and
replaces a previously installed block when its implementation changes. A
missing target or changed upstream source fails the deployment so a required
command cannot silently disappear. Nothing is written unless the old code or
the recorded previous patch matches exactly.

Covered customizations (not yet upstream):
 * gateway commands: /gw-restart (canonical, with /restart and /gw_restart
   aliases so the Telegram menu entry resolves), /model_global, and /doctor
 * /status shows reasoning, models, and session-scoped background activity
 * busy-session dispatch handles /gw-restart like /restart
 * Telegram command-menu usage ranking, with explicit user priorities pinned
 * /update is CLI-only: chat/gateway surfaces cannot trigger Hermes's own
   git-rebase-based self-update, which is fragile against this VPS's shallow
   clone and pinned commit. vps-defaults.yml + Ansible remain the sole update
   path.
The existing Edge TTS retry lives in ops/apply-edge-tts-retry.py and is not
touched here.
"""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import re
import sys


HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_INSTALL_DIR", str(Path.home() / ".hermes" / "hermes-agent"))
)

_PREFIX = "# Local Hermes:"
# Relative paths patched by multiple migrations/patches below; named once so
# the literal isn't duplicated across the file (SonarCloud: duplicated string).
_HERMES_CLI_COMMANDS_PATH = "hermes_cli/commands.py"
_GATEWAY_SLASH_COMMANDS_PATH = "gateway/slash_commands.py"
_GATEWAY_RUN_PATH = "gateway/run.py"
_GATEWAY_BUSY_PATH = "gateway/run_busy.py"
_GATEWAY_STATUS_PATH = "gateway/slash_commands_status.py"
_COMMAND_PLATFORMS_PATH = "hermes_cli/commands_platforms.py"
# Construct the retired spelling without advertising it as a supported slash
# command. It is needed only to migrate files patched by earlier deployments.
_RETIRED_MODEL_GLOBAL = "model" + chr(45) + "global"
_GW_RESTART = "gw" + chr(45) + "restart"
_STATE_FILE = ".local-hermes-patches.json"


class PatchMigrationError(RuntimeError):
    """Raised when an installed local patch has an unknown legacy shape."""


def _replace_required(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise PatchMigrationError(f"cannot migrate {label}: expected code is missing")
    return source.replace(old, new, 1)


def _patch_digest(new: str) -> str:
    return hashlib.sha256(new.encode("utf-8")).hexdigest()


def _load_patch_state() -> dict[str, dict[str, str]]:
    state_path = HERMES_AGENT_DIR / _STATE_FILE
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_patch_state(state: dict[str, dict[str, str]]) -> None:
    state_path = HERMES_AGENT_DIR / _STATE_FILE
    state_path.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _migrate_model_global_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Migrate earlier local command patches to the underscore-only command."""
    retired = _RETIRED_MODEL_GLOBAL
    retired_marker = f"{_PREFIX} {retired}"
    if retired_marker not in source:
        return source, False

    if relative_path == _HERMES_CLI_COMMANDS_PATH:
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
    elif relative_path == _GATEWAY_SLASH_COMMANDS_PATH:
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
    elif relative_path == _GATEWAY_RUN_PATH:
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
        _HERMES_CLI_COMMANDS_PATH,
        _GATEWAY_SLASH_COMMANDS_PATH,
        _GATEWAY_RUN_PATH,
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
    if relative_path == _HERMES_CLI_COMMANDS_PATH:
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
    elif relative_path == _GATEWAY_RUN_PATH:
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
    for relative_path in (_HERMES_CLI_COMMANDS_PATH, _GATEWAY_RUN_PATH):
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


def _migrate_doctor_handler_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Migrate the first /doctor patch to pass the resolved command as argv."""
    if relative_path != _GATEWAY_SLASH_COMMANDS_PATH:
        return source, False

    marker = _PREFIX + " doctor handler"
    if marker not in source:
        return source, False
    old = '''                str(_resolve_hermes_bin()),
                "doctor",
'''
    new = '''                *_resolve_hermes_bin(),
                "doctor",
'''
    if old not in source:
        if new in source:
            return source, False
        raise PatchMigrationError(
            "cannot migrate doctor handler: unknown Hermes command invocation"
        )
    return source.replace(old, new, 1), True


def _migrate_installed_doctor_handler() -> int:
    target = HERMES_AGENT_DIR / "gateway" / "slash_commands.py"
    if not target.is_file():
        return 0
    migrated, changed = _migrate_doctor_handler_source(
        _GATEWAY_SLASH_COMMANDS_PATH, target.read_text(encoding="utf-8")
    )
    if not changed:
        return 0
    target.write_text(migrated, encoding="utf-8")
    print(f"[hermes-patch] migrated {target.relative_to(HERMES_AGENT_DIR)}")
    return 1


def _migrate_telegram_usage_ranking_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Mark the first usage-ranking patch, which predated its idempotency marker."""
    if relative_path != _HERMES_CLI_COMMANDS_PATH:
        return source, False
    marker = _PREFIX + " telegram usage ranking"
    if marker in source:
        return source, False
    anchor = "    configured_priority = _dedupe_sanitized_names(menu_cfg[\"priority\"])\n"
    if anchor not in source:
        return source, False
    return source.replace(anchor, f"    {marker}\n" + anchor, 1), True


def _migrate_installed_telegram_usage_ranking() -> int:
    target = HERMES_AGENT_DIR / "hermes_cli" / "commands.py"
    if not target.is_file():
        return 0
    migrated, changed = _migrate_telegram_usage_ranking_source(
        _HERMES_CLI_COMMANDS_PATH, target.read_text(encoding="utf-8")
    )
    if not changed:
        return 0
    target.write_text(migrated, encoding="utf-8")
    print(f"[hermes-patch] migrated {target.relative_to(HERMES_AGENT_DIR)}")
    return 1


_PATCHES: list[tuple[str, str, str, str]] = [
    # NOTE: every ``new`` block MUST include its marker as a comment line so
    # the idempotency check (marker already present -> skip) works on re-run.
    (
        _HERMES_CLI_COMMANDS_PATH,
        _PREFIX + " model_global CommandDef",
        '''    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--reasoning level] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model", desktop="hidden"),
''',
        '''    CommandDef("model", "Switch model (session-scoped; --global to persist)", "Configuration",
               args_hint="[model] [--provider name] [--reasoning level] [--global|--session] [--refresh]",
               busy_policy="reject", busy_handler="model", desktop="hidden"),
    # Local Hermes: model_global CommandDef
    CommandDef("model_global", "Set the global default model for all topics/sessions", "Configuration",
               args_hint="[model] [--provider name]",
               busy_policy="reject", busy_handler="model"),
''',
    ),
    (
        _HERMES_CLI_COMMANDS_PATH,
        _PREFIX + " gw-restart canonical",
        '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", desktop="terminal"),
''',
        '''    # Local Hermes: gw-restart canonical
    CommandDef("gw-restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", desktop="terminal",
               aliases=("restart", "gw_restart")),
''',
    ),
    (
        "gateway/slash_commands_model.py",
        _PREFIX + " model_global handler",
        '''    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
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
        # Local Hermes: model_global handler
''',
    ),
    (
        _GATEWAY_BUSY_PATH,
        _PREFIX + " model_global route",
        '''        "approvals", "model", "codex-runtime", "personality", "suggestions", "save", "retry",
''',
        '''        "approvals", "model", "codex-runtime", "personality", "suggestions", "save", "retry",
        # Local Hermes: model_global route
        "model_global",
''',
    ),
    (
        # v0.21.4 derives the shared idle/busy handler map from command names.
        # The custom canonical name needs both a name entry and a method alias.
        _GATEWAY_BUSY_PATH,
        _PREFIX + " gw-restart route",
        '''    _COMMAND_HANDLER_ALIASES = {"bg": "_handle_background_command", "sethome": "_handle_set_home_command"}
    # Ordinary slash handlers shared by idle and busy dispatch.
    _PLAIN_COMMANDS = (
''',
        '''    # Local Hermes: gw-restart route
    _COMMAND_HANDLER_ALIASES = {
        "bg": "_handle_background_command", "sethome": "_handle_set_home_command",
        "gw-restart": "_handle_restart_command",
    }
    # Ordinary slash handlers shared by idle and busy dispatch.
    _PLAIN_COMMANDS = (
        "gw-restart",
''',
    ),
    (
        _GATEWAY_STATUS_PATH,
        _PREFIX + " status reasoning",
        '''        lines += [t("gateway.status.tokens", tokens=fields["tokens"]),
                  t("gateway.status.agent_running", state=state)]
''',
        '''        lines += [t("gateway.status.tokens", tokens=fields["tokens"]),
                  t("gateway.status.agent_running", state=state)]
        # Local Hermes: status subagent activity
        status_session_key = str(session_key or "")
        status_session_id = str(session_entry.session_id or "")

        def _status_owned(owner_key, parent_id):
            # A matching chat must not pull in work from before /new. When
            # present, both identifiers must agree; never match empty IDs.
            if owner_key and owner_key != status_session_key:
                return False
            if parent_id and parent_id != status_session_id:
                return False
            return bool(owner_key or parent_id)

        active_delegations = None
        try:
            from tools.async_delegation import list_async_delegations

            active_delegations = [
                d for d in list_async_delegations()
                if d.get("status") in ("running", "stalling", "finalizing") and _status_owned(
                    str(d.get("session_key") or ""), str(d.get("parent_session_id") or "")
                )
            ]
        except Exception:
            pass

        # Local Hermes: status background processes
        # Query metadata only: never read logs/wait (which consume completion
        # delivery), print shell commands, or list across chats on a missing key.
        background_processes = None
        if status_session_key:
            try:
                from tools.process_registry import process_registry

                owned_processes = []
                for row in process_registry.list_sessions(session_key=status_session_key):
                    if row.get("status") != "running":
                        continue
                    process = process_registry.get(row["session_id"])
                    if process is None or process.exited:
                        continue
                    if not _status_owned(process.session_key, process.parent_session_id):
                        continue
                    owned_processes.append(row)
                background_processes = owned_processes
            except Exception:
                pass

        if is_running:
            work_state = "agent responding"
        elif agent is _AGENT_PENDING_SENTINEL:
            work_state = "agent starting"
        elif active_delegations or background_processes:
            work_state = "background active (main agent idle)"
        elif active_delegations is None or background_processes is None:
            work_state = "unknown (activity data unavailable)"
        else:
            work_state = "idle"
        lines.append(f"**Work:** {work_state}")
        subagent_state = "unavailable" if active_delegations is None else f"{len(active_delegations)} active"
        process_state = "unavailable" if background_processes is None else f"{len(background_processes)} running"
        lines.extend([f"**Subagents:** {subagent_state}", f"**Background processes:** {process_state}"])

        def _status_process_line(row):
            import re

            # Only registry-generated IDs, elapsed time and a boolean are
            # user-visible; raw commands/output may contain credentials.
            process_id = str(row.get("session_id") or "")
            if not re.fullmatch(r"proc_[a-fA-F0-9]+", process_id):
                process_id = "process"
            try:
                seconds = max(0, int(row["uptime_seconds"]))
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                elapsed = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"
            except (KeyError, TypeError, ValueError, OverflowError):
                elapsed = "age unknown"
            notification = "enabled" if row.get("notify_on_complete") is True else "disabled"
            return f"• {process_id} — {elapsed}; agent notification on exit: {notification}"

        for row in (background_processes or [])[:5]:
            lines.append(_status_process_line(row))
        if background_processes and len(background_processes) > 5:
            lines.append(f"… {len(background_processes) - 5} more running processes")
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
            from gateway.run import _load_gateway_config, _resolve_gateway_model

            # Global default = config.yaml model.default (single source of truth).
            user_config = _load_gateway_config()
            global_model = _resolve_gateway_model(user_config) if user_config else _resolve_gateway_model()
            # Session override = /model <name> stored for this topic (if any).
            # The stored value is a full provider config mapping (model,
            # provider, api_key, base_url, ...), so read only the model name
            # out of it — rendering the mapping itself leaks the provider API
            # key into /status output. Upstream reads it the same way.
            session_override = self._session_model_override(str(session_key or ""))
            if isinstance(session_override, dict):
                session_model = _clean_str(session_override.get("model") or "")
            else:
                session_model = _clean_str(session_override or "")
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
    (
        _GATEWAY_STATUS_PATH,
        _PREFIX + " portal info",
        '''            lines.append(f"**Topic model:** {topic_model}" + (" *(override)*" if session_model else ""))
        except Exception:
            pass
''',
        '''            lines.append(f"**Topic model:** {topic_model}" + (" *(override)*" if session_model else ""))
        except Exception:
            pass
        # Local Hermes: portal info
        try:
            from gateway.run import _resolve_hermes_bin

            portal_process = await asyncio.create_subprocess_exec(
                *_resolve_hermes_bin(),
                "portal",
                "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                portal_bytes, _ = await asyncio.wait_for(
                    portal_process.communicate(), timeout=10
                )
            except asyncio.TimeoutError:
                try:
                    portal_process.kill()
                except ProcessLookupError:
                    pass
                await portal_process.communicate()
                raise
            portal_info = portal_bytes.decode("utf-8", errors="replace").strip()
            if portal_info:
                lines.append("**Provider and tools:**\\n```\\n" + portal_info[:3500] + "\\n```")
        except Exception:
            lines.append("**Provider and tools:** unavailable")
''',
    ),
    (
        _HERMES_CLI_COMMANDS_PATH,
        _PREFIX + " doctor CommandDef",
        '''    CommandDef("status", "Show session, model, token, and context info", "Session",
               busy_policy="dispatch"),
    CommandDef("egress", "Show Docker egress proxy status", "Session",
''',
        '''    CommandDef("status", "Show session, model, token, and context info", "Session",
               busy_policy="dispatch"),
    # Local Hermes: doctor CommandDef
    CommandDef("doctor", "Run read-only Hermes diagnostics", "Info",
               gateway_only=True, busy_policy="dispatch"),
    CommandDef("egress", "Show Docker egress proxy status", "Session",
''',
    ),
    (
        _GATEWAY_SLASH_COMMANDS_PATH,
        _PREFIX + " doctor handler",
        '''    async def _handle_version_command(self, event: MessageEvent) -> str:
        """Handle /version — show the running Hermes Agent version."""
        return _execute("version").text

''',
        '''    async def _handle_version_command(self, event: MessageEvent) -> str:
        """Handle /version — show the running Hermes Agent version."""
        return _execute("version").text

    async def _handle_doctor_command(self, event: MessageEvent) -> str:
        """Handle /doctor with the read-only Hermes diagnostic command."""
        from gateway.run import _resolve_hermes_bin

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *_resolve_hermes_bin(),
                "doctor",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            if process is not None:
                process.kill()
                await process.wait()
            return "Doctor timed out after 30 seconds."
        except OSError as exc:
            return f"Doctor could not start: {exc}"

        output = output_bytes.decode("utf-8", errors="replace").strip()
        if len(output) > 3500:
            output = output[:3500].rstrip() + "\\n\\n… Output truncated."
        if process.returncode:
            return f"Doctor failed (exit {process.returncode}):\\n{output}"
        return output or "Doctor completed without diagnostic output."

    # Local Hermes: doctor handler
''',
    ),
    (
        _GATEWAY_BUSY_PATH,
        _PREFIX + " doctor route",
        '''        "commands", "profile", "login", "update", "version",
''',
        '''        "commands", "profile", "login", "update", "version",
        # Local Hermes: doctor route
        "doctor",
''',
    ),
    (
        _COMMAND_PLATFORMS_PATH,
        _PREFIX + " telegram usage ranking",
        # Leave every configured/default priority tier intact; rank by usage
        # only after all upstream priority tiers have declined the candidate.
        '''                return (tier, indexes[table], stable_index)
        return (len(tiers), 0, stable_index)
''',
        '''                return (tier, indexes[table], stable_index)
        # Local Hermes: telegram usage ranking
        return (len(tiers), -_telegram_command_usage_count(final_name), stable_index)
''',
    ),
    (
        _COMMAND_PLATFORMS_PATH,
        _PREFIX + " telegram usage config",
        '''        "priority": priority}
''',
        '''        "priority": priority,
        # Local Hermes: telegram usage config
        "usage_ranking": menu_cfg.get("usage_ranking", {}),
    }
''',
    ),
    (
        _COMMAND_PLATFORMS_PATH,
        _PREFIX + " telegram usage state",
        '''def _clamp_command_names(
    entries: Sequence[tuple[str, ...]], reserved: set[str]) -> list[tuple[str, ...]]:
''',
        '''import os


def _telegram_usage_ranking_config() -> tuple[bool, int]:
    """Return whether dynamic Telegram menu ranking is enabled and its cadence."""
    raw_ranking = _telegram_command_menu_config().get("usage_ranking", {})
    if not isinstance(raw_ranking, Mapping):
        return False, 5
    enabled = raw_ranking.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    try:
        refresh_every = int(raw_ranking.get("refresh_every", 5))
    except (TypeError, ValueError):
        refresh_every = 5
    return bool(enabled), max(1, min(100, refresh_every))


def _telegram_usage_state_path() -> str:
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(hermes_home, "telegram-command-usage.json")


def _telegram_usage_state() -> tuple[dict[str, int], int]:
    import json

    try:
        with open(_telegram_usage_state_path(), encoding="utf-8") as state_file:
            raw_state = json.load(state_file)
    except (OSError, ValueError, TypeError):
        return {}, 0
    if not isinstance(raw_state, Mapping):
        return {}, 0
    raw_counts = raw_state.get("counts", {})
    counts: dict[str, int] = {}
    if isinstance(raw_counts, Mapping):
        for raw_name, raw_count in raw_counts.items():
            name = _sanitize_telegram_name(str(raw_name))
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if name and len(name) <= _CMD_NAME_LIMIT and count > 0:
                counts[name] = min(count, 1_000_000_000)
    try:
        pending_refresh = int(raw_state.get("pending_refresh", 0))
    except (TypeError, ValueError):
        pending_refresh = 0
    return counts, max(0, pending_refresh)


def _write_telegram_usage_state(counts: Mapping[str, int], pending_refresh: int) -> None:
    import json

    state_path = _telegram_usage_state_path()
    os.makedirs(os.path.dirname(state_path), mode=0o700, exist_ok=True)
    temporary_path = state_path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as state_file:
        json.dump(
            {"counts": dict(counts), "pending_refresh": pending_refresh},
            state_file,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        state_file.write("\\n")
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, state_path)


_telegram_usage_cache: tuple[float, dict[str, int]] | None = None


def _telegram_command_usage_counts() -> dict[str, int]:
    """Return persisted usage counts, re-reading only after the state changes."""
    global _telegram_usage_cache
    enabled, _refresh_every = _telegram_usage_ranking_config()
    if not enabled:
        return {}
    try:
        modified_at = os.path.getmtime(_telegram_usage_state_path())
    except OSError:
        return {}
    if _telegram_usage_cache is None or _telegram_usage_cache[0] != modified_at:
        _telegram_usage_cache = (modified_at, _telegram_usage_state()[0])
    return _telegram_usage_cache[1]


def _telegram_command_usage_count(name: str) -> int:
    """Usage count for one command name, used as a menu sort key."""
    return _telegram_command_usage_counts().get(name, 0)


def record_telegram_command_usage(raw_command: str) -> bool:
    """Persist one command invocation and report when Telegram menu should refresh."""
    enabled, refresh_every = _telegram_usage_ranking_config()
    name = _sanitize_telegram_name(raw_command)
    if not enabled or not name or len(name) > _CMD_NAME_LIMIT:
        return False
    counts, pending_refresh = _telegram_usage_state()
    counts[name] = min(counts.get(name, 0) + 1, 1_000_000_000)
    pending_refresh += 1
    should_refresh = pending_refresh >= refresh_every
    try:
        _write_telegram_usage_state(counts, 0 if should_refresh else pending_refresh)
    except OSError:
        logger.debug("Could not persist Telegram command usage state", exc_info=True)
        return False
    return should_refresh


def _clamp_command_names(
    entries: Sequence[tuple[str, ...]], reserved: set[str]) -> list[tuple[str, ...]]:
    # Local Hermes: telegram usage state
''',
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        _PREFIX + " telegram usage refresh",
        '''    def _effective_update_message(self, update: Update) -> Optional[Message]:
''',
        '''    def _record_telegram_command_usage(self, text: str) -> None:
        """Persist authorized slash-command usage and coalesce menu refreshes."""
        command = text.lstrip().split(None, 1)[0].lstrip("/").split("@", 1)[0]
        try:
            from hermes_cli.commands_platforms import record_telegram_command_usage

            should_refresh = record_telegram_command_usage(command)
        except Exception:
            logger.debug("[%s] Could not record Telegram command usage", self.name, exc_info=True)
            return
        if not should_refresh:
            return
        task = getattr(self, "_command_menu_usage_refresh_task", None)
        if task and not task.done():
            return
        self._command_menu_usage_refresh_task = asyncio.ensure_future(
            self._refresh_telegram_command_menu_by_usage()
        )

    async def _refresh_telegram_command_menu_by_usage(self) -> None:
        """Best-effort refresh for the shared and known forum Telegram menus."""
        try:
            from telegram import (
                BotCommand,
                BotCommandScopeAllGroupChats,
                BotCommandScopeAllPrivateChats,
                BotCommandScopeChat,
                BotCommandScopeDefault,
            )
            from hermes_cli.commands_platforms import telegram_menu_commands, telegram_menu_max_commands

            if not self._bot:
                return
            menu_commands, _ = telegram_menu_commands(
                max_commands=telegram_menu_max_commands()
            )
            bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
            for scope_cls in (
                BotCommandScopeDefault,
                BotCommandScopeAllPrivateChats,
                BotCommandScopeAllGroupChats,
            ):
                await self._bot.set_my_commands(bot_commands, scope=scope_cls())
            for chat_id in tuple(getattr(self, "_forum_command_registered", set())):
                await self._bot.set_my_commands(
                    bot_commands,
                    scope=BotCommandScopeChat(chat_id=chat_id),
                )
            logger.info("[%s] Refreshed Telegram command menu from usage ranking", self.name)
        except Exception:
            logger.warning("[%s] Telegram usage menu refresh failed", self.name, exc_info=True)
        finally:
            if getattr(self, "_command_menu_usage_refresh_task", None) is asyncio.current_task():
                self._command_menu_usage_refresh_task = None

    # Local Hermes: telegram usage refresh
    def _effective_update_message(self, update: Update) -> Optional[Message]:
''',
    ),
    (
        "plugins/platforms/telegram/adapter.py",
        _PREFIX + " telegram usage record",
        '''        event = await self._build_triggered_event(msg, update, MessageType.COMMAND)
''',
        '''        event = await self._build_triggered_event(msg, update, MessageType.COMMAND)
        self._record_telegram_command_usage(event.text)
        # Local Hermes: telegram usage record
''',
    ),
    (
        _HERMES_CLI_COMMANDS_PATH,
        _PREFIX + " update cli_only",
        '''    CommandDef("update", "Update Hermes Agent to the latest version", "Info",
               busy_policy="dispatch", desktop="terminal"),
''',
        '''    # Local Hermes: update cli_only
    CommandDef("update", "Update Hermes Agent to the latest version", "Info",
               busy_policy="dispatch", desktop="terminal", cli_only=True),
''',
    ),
]


def _migrate_installed_portal_info() -> int:
    """Repair the first portal-info rollout, which used invalid multiline literals."""
    target = HERMES_AGENT_DIR / _GATEWAY_SLASH_COMMANDS_PATH
    if not target.is_file():
        return 0
    source = target.read_text(encoding="utf-8")
    marker = _PREFIX + " portal info"
    if marker not in source:
        return 0
    portal_patch = next(new for _path, patch_marker, _old, new in _PATCHES
                        if patch_marker == marker)
    replacement = portal_patch[portal_patch.index("        # Local Hermes: portal info"):]
    if replacement in source:
        return 0
    pattern = re.compile(
        r"        # Local Hermes: portal info\n"
        r"        try:\n.*?"
        r"        except Exception:\n"
        r"            lines\.append\(\"\*\*Provider and tools:\*\* unavailable\"\)\n",
        re.DOTALL,
    )
    repaired, count = pattern.subn(lambda _match: replacement, source, count=1)
    if count:
        target.write_text(repaired, encoding="utf-8")
        print(f"[hermes-patch] migrated {target.relative_to(HERMES_AGENT_DIR)} portal info")
    return count


def _classify_patch(
    source: str,
    marker: str,
    old: str,
    new: str,
    previous: dict[str, str] | None,
) -> str:
    """Decide the action for one patch without touching the filesystem.

    Returns one of: "apply", "upgrade", "refresh", "record", "skip",
    "locally-changed", or "old-missing". Keeping this pure (no path argument,
    no I/O) is deliberate: the caller resolves and writes the target from the
    trusted patch registry constants, so no attacker-controlled path ever
    reaches a filesystem sink.
    """
    if marker not in source:
        return "old-missing" if old not in source else "apply"
    if previous is None:
        return "record"
    if previous.get("digest") == _patch_digest(new):
        return "skip"
    if new in source:
        return "refresh"
    previous_source = previous.get("source", "")
    if not previous_source or previous_source not in source:
        return "locally-changed"
    return "upgrade"


def _apply_one_patch(
    relative_path: str,
    marker: str,
    old: str,
    new: str,
    patch_state: dict[str, dict[str, str]],
) -> tuple[int, str | None, bool]:
    """Apply one registered patch; return (applied_delta, failure, state_changed).

    ``state_changed`` is True only when ``patch_state`` is actually mutated
    (``record``/``refresh``/``apply``/``upgrade``); ``skip`` intentionally
    returns False because it neither applies nor records anything new.
    """
    target = HERMES_AGENT_DIR / relative_path
    if not target.is_file():
        print(f"[hermes-patch] ERROR: {relative_path}: file missing", file=sys.stderr)
        return 0, relative_path, False
    source = target.read_text(encoding="utf-8")
    digest = _patch_digest(new)
    action = _classify_patch(source, marker, old, new, patch_state.get(marker))
    if action == "record":
        patch_state[marker] = {"digest": digest, "source": new}
        print(f"[hermes-patch] {relative_path}: already applied")
        return 0, None, True
    if action == "skip":
        print(f"[hermes-patch] {relative_path}: already applied")
        return 0, None, False
    if action == "refresh":
        patch_state[marker] = {"digest": digest, "source": new}
        print(f"[hermes-patch] {relative_path}: patch metadata refreshed")
        return 0, None, True
    if action == "locally-changed":
        print(
            f"[hermes-patch] ERROR: {relative_path}: existing patch "
            f"{marker!r} changed locally and cannot be upgraded safely",
            file=sys.stderr,
        )
        return 0, relative_path, False
    if action == "old-missing":
        print(
            f"[hermes-patch] ERROR: {relative_path} does not match the expected "
            "code. Hermes may have changed — re-verify the patch "
            "before relying on /model_global, /gw-restart, or /status reasoning.",
            file=sys.stderr,
        )
        return 0, relative_path, False
    previous_source = patch_state.get(marker, {}).get("source", "")
    replacement = previous_source if action == "upgrade" else old
    target.write_text(source.replace(replacement, new, 1), encoding="utf-8")
    patch_state[marker] = {"digest": digest, "source": new}
    verb = "updated" if action == "upgrade" else "applied"
    print(f"[hermes-patch] {verb} {relative_path}")
    return 1, None, True


def main() -> int:
    if not HERMES_AGENT_DIR.is_dir():
        print(
            f"[hermes-patch] ERROR: Hermes install directory is missing: {HERMES_AGENT_DIR}",
            file=sys.stderr,
        )
        return 1
    try:
        migrated = (
            _migrate_installed_model_global()
            + _migrate_installed_gw_restart()
            + _migrate_installed_doctor_handler()
            + _migrate_installed_telegram_usage_ranking()
            + _migrate_installed_portal_info()
        )
    except PatchMigrationError as exc:
        print(f"[hermes-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    applied = 0
    failures: list[str] = []
    patch_state = _load_patch_state()
    state_changed = False
    for relative_path, marker, old, new in _PATCHES:
        delta, failure, changed = _apply_one_patch(relative_path, marker, old, new, patch_state)
        applied += delta
        state_changed = state_changed or changed
        if failure is not None:
            failures.append(failure)
    if applied or migrated:
        print("[hermes-patch] changed")
    if state_changed:
        _save_patch_state(patch_state)
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
