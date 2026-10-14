---
name: task_verification
description: Verify multi-step task results, artifacts and delivery before claiming completion; recover from partial or failed work
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    category: productivity
    tags: [verification, reliability]
---

# Verify the requested result

Use for multi-step work, scheduled reports, code/CI fixes, and recovery after
failed tools or context compression. This skill grants no new permissions.
Follow the task's specialized skill and runtime contract first.

1. State what the owner should receive and how to verify it. Preserve the whole
   request: fixing one test is not fixing a pipeline; saying a report is ready
   is not providing its contents. Keep a short record of completed criteria,
   evidence, remaining work and the next safe action in the current task state.
2. Check actual state before continuing, especially after compression or a
   failed write: read the file, current revision, execution ID and tool result.
   Another agent's summary and an old successful run are leads, not receipts.
3. Use the task's prescribed artifact format and exact paths. Do not rename,
   filter or rewrite collector-owned inputs to make validation pass. Check
   counts, required fields, source links and all requested sections. Runtime
   validators are authoritative for structure, not for factual truth.
4. Correct a failed step from its actual error. Read a file before correcting a
   stale patch. Reuse collected evidence when repairing an artifact; do not
   restart expensive research for a formatting failure. Respect runtime retry
   limits and cancellation. A persistent provider/access error needs a supported
   fallback or an explicit blocker, not repeated identical requests.
5. Verify the result at the right level:
   - Code: inspect the resulting diff and run the affected required tests. If
     fixing CI, inspect mandatory checks for the exact pushed revision.
   - Research: return the findings with sources and coverage gaps. Counts and
     phrases such as "analyzed in full" do not replace the report. Never invent
     findings to fill a requested number.
   - Files: reopen the produced artifact and validate its content. An announced
     path does not prove the file exists or is complete.
   - Scheduled/background tasks: distinguish execution status, artifact quality
     and delivery outcome. Confirm the current execution's receipt. Queued,
     unverified, or failure-notice delivery is not successful report delivery.
6. For cron, let its configured delivery mechanism send the result. Never send
   an extra copy merely because the first delivery is uncertain. Do not poll
   forever or promise an unsolicited future notification. A scheduled run must
   record its blocker without asking the recipient to send "continue".
7. Return the requested artifact/content or the specialized handoff marker,
   then a concise outcome with observed checks and any remaining blocker. Do
   not mark a partially completed task successful. Continue independently on
   authorized remaining steps until complete or genuinely blocked.
