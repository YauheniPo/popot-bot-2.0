"""Bounded publication metadata from observed execution, never model prose."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from collections.abc import Mapping


PUBLIC_ENDPOINTS = frozenset({
    "https://ollama.com", "https://ollama.com/v1/chat/completions",
    "https://openrouter.ai/api", "https://openrouter.ai/api/v1/chat/completions",
    "https://integrate.api.nvidia.com", "https://integrate.api.nvidia.com/v1/chat/completions",
    "https://inference-api.nousresearch.com", "https://inference-api.nousresearch.com/v1/chat/completions",
})
MAX_HISTORY_ROWS = 40
EXECUTION_METADATA = re.compile(r"<!-- review-execution -->.*?<!-- /review-execution -->", re.DOTALL)


def strip_execution_metadata(text: str) -> str:
    """Keep execution diagnostics out of subsequent prompts and finding matching."""
    return EXECUTION_METADATA.sub("", text)


def safe_label(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return re.sub(r"[^a-zA-Z0-9_./: -]", "?", value)[:120] or "unknown"


@dataclass
class Attempt:
    number: int
    unit: str
    model: str
    route: str
    format: str
    reasoning: str
    outcome: str = "pending"
    seconds: float | None = None
    response_id: str = ""
    reported_model: str = ""


@dataclass
class ExecutionReport:
    provider: str
    endpoint: str
    primary_model: str
    kind: str = "api"
    unit: str = "review"
    attempts: list[Attempt] = field(default_factory=list)

    def begin(self, body: dict[str, object], route: str = "") -> Attempt:
        model = safe_label(body.get("model"))
        response_format = body.get("response_format")
        reasoning = body.get("reasoning")
        attempt = Attempt(
            len(self.attempts) + 1, self.unit, model,
            route or ("primary" if model == safe_label(self.primary_model) else "fallback"),
            safe_label(response_format.get("type")) if isinstance(response_format, dict) else "ordinary",
            safe_label(reasoning.get("effort")) if isinstance(reasoning, dict) else safe_label(body.get("reasoning_effort", "default")),
        )
        self.attempts.append(attempt)
        return attempt

    def finish(self, attempt: Attempt, outcome: str, seconds: float | None, response: object = None) -> None:
        attempt.outcome = outcome
        attempt.seconds = seconds
        if not isinstance(response, dict):
            return
        response_id = response.get("id")
        if isinstance(response_id, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", response_id):
            attempt.response_id = response_id
        reported_model = response.get("model")
        if isinstance(reported_model, str):
            attempt.reported_model = safe_label(reported_model)

    def validate_last(self, outcome: str) -> None:
        if self.attempts and self.attempts[-1].outcome == "received":
            self.attempts[-1].outcome = outcome

    def connection(self) -> str:
        endpoint = self.endpoint if self.endpoint in PUBLIC_ENDPOINTS else "custom endpoint (address hidden)"
        return f"> Connection: `{safe_label(self.provider)}` · API: `{endpoint}`"

    def summary(self) -> str:
        successes = [a for a in self.attempts if a.outcome == "valid_json"]
        models = sorted({a.model for a in successes})
        # Each unit (chunk or triage) has one initial request. Attempt.number is
        # global report order, so counting number == 1 would mislabel new chunks.
        retries = len(self.attempts) - len({a.unit for a in self.attempts})
        lines = [self.connection()]
        lines.append("> Successful models: " + (", ".join(f"`{m}`" for m in models[:8]) or "none"))
        lines.append(
            f"> Attempts: {len(self.attempts)} · Validated: {len(successes)} · Retries: {retries} · "
            f"Fallback successes: {sum(a.route.startswith('fallback') for a in successes)}"
        )
        lines.append(
            f"> Provider time: {sum(a.seconds or 0 for a in self.attempts):.1f}s "
            "(retry waits, preflight, and GitHub API requests excluded)."
        )
        return "\n".join(lines)

    def footer(self, unit: str) -> str:
        attempts = [a for a in self.attempts if a.unit == unit]
        successes = [a for a in attempts if a.outcome == "valid_json"]
        if not successes:
            return ""
        success = successes[-1]
        label = "request" if self.kind == "api" else "CI attempt"
        history = " → ".join(f"{a.route}:{a.outcome}" for a in attempts[-8:])
        return (
            f"\n\n<!-- review-execution -->\n{self.connection()}\n"
            f"> Result: `{success.model}` · {success.route} · {safe_label(unit)} · "
            f"{label} #{success.number} · format: {success.format}\n"
            f"> Attempt history ({len(attempts)}): {history}\n<!-- /review-execution -->"
        )

    def details(self) -> str:
        if not self.attempts:
            return ""
        rows = self.attempts
        if len(rows) > MAX_HISTORY_ROWS:
            rows = rows[:MAX_HISTORY_ROWS // 2] + rows[-MAX_HISTORY_ROWS // 2:]
        lines = [
            "", "<details>", "<summary>Execution history</summary>", "",
            "| # | Unit | Route | Requested / reported model | Format / reasoning | Result | API seconds | Response ID |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for a in rows:
            model = a.model
            if a.reported_model and a.reported_model != a.model:
                model += " / " + a.reported_model
            seconds = f"{a.seconds:.1f}" if a.seconds is not None else "—"
            lines.append(
                f"| {a.number} | {safe_label(a.unit)} | {a.route} | {model} | "
                f"{a.format} / {a.reasoning} | {a.outcome} | {seconds} | {a.response_id or '—'} |"
            )
        if len(rows) < len(self.attempts):
            lines.extend(["", f"Showing first and last {MAX_HISTORY_ROWS // 2} attempts; totals include all {len(self.attempts)}."])
        lines.extend(["", "</details>"])
        return "\n".join(lines)


def technical_metadata(report: ExecutionReport) -> str:
    """Render the same metadata block for every reviewer publication."""
    return "### Technical metadata\n" + report.summary()


def observable_execution_report(raw_attempts: list[dict], total_chunks: int) -> ExecutionReport:
    """Adapt Observable's JSON attempt records to the shared metadata format."""
    endpoints = {
        "openrouter": "https://openrouter.ai/api",
        "ollama-cloud": "https://ollama.com",
        "nvidia": "https://integrate.api.nvidia.com",
        "nous": "https://inference-api.nousresearch.com",
    }
    first = raw_attempts[0] if raw_attempts else {}
    provider = safe_label(first.get("provider", "unknown"))
    primary = safe_label(first.get("model", "unknown"))
    report = ExecutionReport(provider, endpoints.get(provider, ""), primary)
    for raw in raw_attempts:
        chunk = raw.get("chunk", 1)
        report.unit = f"chunk {chunk}/{total_chunks}"
        attempt = report.begin({"model": raw.get("model", "unknown")}, route=raw.get("role", "primary"))
        report.finish(attempt, raw.get("outcome", "unknown"), raw.get("seconds"))
    return report


def claude_execution_report(environment: Mapping[str, str]) -> ExecutionReport:
    primary = environment.get("CLAUDE_REVIEW_PRIMARY_MODEL", environment.get("CLAUDE_REVIEW_MODEL", "unknown"))
    report = ExecutionReport(
        environment.get("CLAUDE_REVIEW_PROVIDER", "unknown"),
        environment.get("CLAUDE_REVIEW_ENDPOINT", ""), primary, kind="claude",
    )
    for stage in ("PRIMARY", "RETRY", "FALLBACK"):
        outcome = environment.get(f"CLAUDE_REVIEW_{stage}_OUTCOME", "skipped")
        if outcome not in {"success", "failure", "cancelled"}:
            continue
        validation = environment.get(f"CLAUDE_REVIEW_{stage}_VALIDATION", "skipped")
        model = environment.get("CLAUDE_REVIEW_FALLBACK_MODEL", "unknown") if stage == "FALLBACK" else primary
        attempt = report.begin({"model": model}, route="fallback" if stage == "FALLBACK" else "primary")
        result = "execution_failed"
        if outcome == "success":
            result = "valid_json" if validation == "success" else "validation_failed"
        report.finish(attempt, result, None)
    return report
