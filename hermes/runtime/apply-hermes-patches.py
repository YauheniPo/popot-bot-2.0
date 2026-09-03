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
 * /status shows reasoning effort, visibility, global + topic model
 * busy-session dispatch handles /gw-restart like /restart
 * Telegram command-menu usage ranking, with explicit user priorities pinned
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
    except (FileNotFoundError, OSError, json.JSONDecodeError):
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


def _migrate_doctor_handler_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Migrate the first /doctor patch to pass the resolved command as argv."""
    if relative_path != "gateway/slash_commands.py":
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
        "gateway/slash_commands.py", target.read_text(encoding="utf-8")
    )
    if not changed:
        return 0
    target.write_text(migrated, encoding="utf-8")
    print(f"[hermes-patch] migrated {target.relative_to(HERMES_AGENT_DIR)}")
    return 1


def _migrate_telegram_usage_ranking_source(relative_path: str, source: str) -> tuple[str, bool]:
    """Mark the first usage-ranking patch, which predated its idempotency marker."""
    if relative_path != "hermes_cli/commands.py":
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
        "hermes_cli/commands.py", target.read_text(encoding="utf-8")
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
    (
        "gateway/slash_commands.py",
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
        "hermes_cli/commands.py",
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
        "gateway/slash_commands.py",
        _PREFIX + " doctor handler",
        '''    async def _handle_version_command(self, event: MessageEvent) -> str:
        """Handle /version — show the running Hermes Agent version."""
        from hermes_cli.slash_exec import CommandContext, execute_command

        return execute_command("version", CommandContext(surface="gateway")).text

''',
        '''    async def _handle_version_command(self, event: MessageEvent) -> str:
        """Handle /version — show the running Hermes Agent version."""
        from hermes_cli.slash_exec import CommandContext, execute_command

        return execute_command("version", CommandContext(surface="gateway")).text

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
        "gateway/run.py",
        _PREFIX + " doctor route",
        '''        if canonical == "version":
            return await self._handle_version_command(event)

        if canonical == "debug":
''',
        '''        if canonical == "version":
            return await self._handle_version_command(event)

        if canonical == "doctor":
            # Local Hermes: doctor route
            return await self._handle_doctor_command(event)

        if canonical == "debug":
''',
    ),
    (
        "hermes_cli/commands.py",
        _PREFIX + " telegram usage ranking",
        '''def _prioritize_telegram_menu_commands(
    commands: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    priority = {
        name: index
        for index, name in enumerate(_telegram_effective_priority())
    }
    return [
        command
        for _index, command in sorted(
            enumerate(commands),
            key=lambda item: (
                0,
                priority[item[1][0]],
                item[0],
            )
            if item[1][0] in priority
            else (
                1,
                item[0],
            ),
        )
    ]

''',
        '''def _prioritize_telegram_menu_commands(
    commands: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    # Local Hermes: telegram usage ranking
    menu_cfg = _telegram_command_menu_config()
    configured_priority = _dedupe_sanitized_names(menu_cfg["priority"])
    pinned_indexes = {
        name: index for index, name in enumerate(configured_priority)
    }
    default_indexes = {
        name: index
        for index, name in enumerate(_telegram_effective_priority())
    }
    usage_ranking_enabled, _refresh_every = _telegram_usage_ranking_config()
    usage_counts = _telegram_command_usage_counts() if usage_ranking_enabled else {}

    def sort_key(item: tuple[int, tuple[str, str]]) -> tuple[int, int, int]:
        original_index, command = item
        name = command[0]
        # Explicit user priority is an absolute first tier. Usage counts can
        # never move a pinned command below an unpinned command.
        if name in pinned_indexes:
            return (0, pinned_indexes[name], original_index)
        if usage_ranking_enabled:
            return (1, -usage_counts.get(name, 0), original_index)
        if name in default_indexes:
            return (1, default_indexes[name], original_index)
        return (2, original_index, original_index)

    return [command for _index, command in sorted(enumerate(commands), key=sort_key)]

''',
    ),
    (
        "hermes_cli/commands.py",
        _PREFIX + " telegram usage state",
        '''def _clamp_command_names(
    entries: list[tuple[str, ...]],
    reserved: set[str],
) -> list[tuple[str, ...]]:
''',
        '''def _telegram_usage_ranking_config() -> tuple[bool, int]:
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


def _telegram_command_usage_counts() -> dict[str, int]:
    enabled, _refresh_every = _telegram_usage_ranking_config()
    return _telegram_usage_state()[0] if enabled else {}


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
    entries: list[tuple[str, ...]],
    reserved: set[str],
) -> list[tuple[str, ...]]:
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
            from hermes_cli.commands import record_telegram_command_usage

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
            from hermes_cli.commands import telegram_menu_commands, telegram_menu_max_commands

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
        '''        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
''',
        '''        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        self._record_telegram_command_usage(event.text)
        await self._cache_replied_media(msg, event)
        # Local Hermes: telegram usage record
''',
    ),
]


def _migrate_installed_portal_info() -> int:
    """Repair the first portal-info rollout, which used invalid multiline literals."""
    target = HERMES_AGENT_DIR / "gateway/slash_commands.py"
    if not target.is_file():
        return 0
    source = target.read_text(encoding="utf-8")
    marker = _PREFIX + " portal info"
    if marker not in source:
        return 0
    portal_patch = next(new for path, patch_marker, _old, new in _PATCHES
                        if path == "gateway/slash_commands.py" and patch_marker == marker)
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
        target = HERMES_AGENT_DIR / relative_path
        if not target.is_file():
            print(f"[hermes-patch] ERROR: {relative_path}: file missing", file=sys.stderr)
            failures.append(relative_path)
            continue
        source = target.read_text(encoding="utf-8")
        digest = _patch_digest(new)
        if marker in source:
            previous = patch_state.get(marker)
            if previous and previous.get("digest") != digest:
                previous_source = previous.get("source", "")
                if new in source:
                    # The new implementation is already present (for example
                    # after a one-off repair); only refresh the local metadata.
                    patch_state[marker] = {"digest": digest, "source": new}
                    state_changed = True
                    print(f"[hermes-patch] {relative_path}: patch metadata refreshed")
                    continue
                if not previous_source or previous_source not in source:
                    print(
                        f"[hermes-patch] ERROR: {relative_path}: existing patch "
                        f"{marker!r} changed locally and cannot be upgraded safely",
                        file=sys.stderr,
                    )
                    failures.append(relative_path)
                    continue
                target.write_text(source.replace(previous_source, new, 1), encoding="utf-8")
                patch_state[marker] = {"digest": digest, "source": new}
                print(f"[hermes-patch] updated {relative_path}")
                applied += 1
                state_changed = True
            else:
                if not previous:
                    patch_state[marker] = {"digest": digest, "source": new}
                    state_changed = True
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
        patch_state[marker] = {"digest": digest, "source": new}
        state_changed = True
        print(f"[hermes-patch] applied {relative_path}")
        applied += 1
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
