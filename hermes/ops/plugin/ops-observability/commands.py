"""No-LLM slash-command reports for local observability data."""

from __future__ import annotations

import shlex

from .metrics import _metrics_snapshot, _period, _query
from .storage import _home

def _ops_command(raw_args: str) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError as error:
        return f"Invalid arguments: {error}"
    topic = parts[0].lower() if parts else "summary"
    if topic == "help":
        return "Usage: /ops [summary|system|models|tools|costs|commands|health] [1h|24h|7d|30d]"
    if topic == "health":
        state = _home() / "ops" / "health-state"
        issues = state.read_text(encoding="utf-8").strip() if state.exists() else ""
        return "VPS health: OK" if not issues else "VPS health alerts:\n" + issues
    try:
        cutoff, label = _period(parts[1] if len(parts) > 1 else "24h")
    except ValueError as error:
        return str(error)

    if topic == "system":
        snapshot = _metrics_snapshot("overview", label)
        resources = snapshot.get("resources", {})
        activity = snapshot.get("activity", {})
        return (
            f"Hermes system ({label})\n"
            f"Gateway: {resources.get('gateway_up')}\n"
            f"Disk used: {resources.get('disk_used_ratio')}\n"
            f"Memory available: {resources.get('memory_available_ratio')}\n"
            f"Load 1m/5m/15m: {resources.get('load_1m')} / {resources.get('load_5m')} / {resources.get('load_15m')}\n"
            f"API calls: {activity.get('api_calls')} ({activity.get('api_errors')} errors)\n"
            f"Tokens: {activity.get('tokens')}; tools: {activity.get('tool_calls')} ({activity.get('tool_errors')} errors)\n"
            f"Cost USD: {activity.get('cost_usd')}"
        )

    if topic == "summary":
        api = _query(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0), "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM api_calls WHERE ts>=?", (cutoff,),
        )
        tools = _query(
            "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM tool_calls WHERE ts>=?", (cutoff,),
        )
        commands = _query("SELECT COUNT(*) FROM commands WHERE ts>=?", (cutoff,))
        a = api[0] if api else (0, 0, 0, 0)
        t = tools[0] if tools else (0, 0)
        c = commands[0][0] if commands else 0
        return (
            f"Hermes activity ({label})\n"
            f"API calls: {a[0]} ({a[3] or 0} errors)\nTokens: {a[1] or 0:,}\n"
            f"Estimated/provider cost: ${float(a[2] or 0):.4f}\n"
            f"Tool calls: {t[0]} ({t[1] or 0} errors)\nSlash commands: {c}"
        )
    if topic in {"models", "costs"}:
        rows = _query(
            "SELECT COALESCE(provider,'?'), COALESCE(model,'?'), COUNT(*), "
            "COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0) "
            "FROM api_calls WHERE ts>=? GROUP BY provider,model ORDER BY SUM(cost_usd) DESC, SUM(total_tokens) DESC LIMIT 15",
            (cutoff,),
        )
        lines = [f"Models ({label})", "provider/model — calls | tokens | cost"]
        lines.extend(f"{p}/{m} — {calls} | {tokens:,} | ${float(cost):.4f}" for p, m, calls, tokens, cost in rows)
        return "\n".join(lines) if rows else f"No model activity for {label}"
    if topic == "tools":
        rows = _query(
            "SELECT tool_name, COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), "
            "COALESCE(AVG(duration_ms),0) FROM tool_calls WHERE ts>=? GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 20",
            (cutoff,),
        )
        lines = [f"Tools ({label})", "tool — calls | errors | avg ms"]
        lines.extend(f"{name} — {calls} | {errors or 0} | {float(avg):.0f}" for name, calls, errors, avg in rows)
        return "\n".join(lines) if rows else f"No tool activity for {label}"
    if topic == "commands":
        rows = _query(
            "SELECT command, COUNT(*) FROM commands WHERE ts>=? GROUP BY command ORDER BY COUNT(*) DESC LIMIT 20", (cutoff,),
        )
        lines = [f"Commands ({label})"]
        lines.extend(f"/{name} — {count}" for name, count in rows)
        return "\n".join(lines) if rows else f"No slash commands for {label}"
    return "Usage: /ops [summary|system|models|tools|costs|commands|health] [1h|24h|7d|30d]"



