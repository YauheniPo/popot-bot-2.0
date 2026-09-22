// Managed adapter for the pinned Workspace; no process cancellation or fake
// provider heartbeat. The tracker proves only a locally registered send stream.
export function workspaceBackgroundRuns() {
  return { name: 'hermes-background-run-records', enforce: 'pre', transform(source, id) {
    const file = id.split('?')[0];
    let code = source;
    const replace = (anchor, value, count = 1) => {
      if (code.split(anchor).length !== count + 1) throw new Error('Unsupported Workspace background contract: ' + file);
      code = code.split(anchor).join(value);
    };
    if (file.endsWith('/src/server/run-store.ts')) {
      replace("import { getHermesRoot } from './claude-paths'", "import { getHermesRoot } from './claude-paths'\nimport { hasActiveSendRun } from './send-run-tracker'");
      replace('  errorMessage?: string\n}', '  errorMessage?: string\n  dismissedAt?: number\n}');
      // A subsequent real event revives the record through the same write queue.
      replace('    const next = updater(current)', '    const next = updater(current)\n    delete next.dismissedAt');
      replace(".filter((run) => !['complete', 'error'].includes(run.status))",
        ".filter((run) => (!run.dismissedAt || hasActiveSendRun(run.runId)) && !['complete', 'error'].includes(run.status))", 2);
      code += `
// Serialize the freshness check with all other writes. Hiding is not failure,
// completion, cancellation, or proof that a remote agent has stopped.
export async function dismissRunRecord(sessionKey: string, runId: string, updatedAt: number) {
  return enqueueRunUpdate(sessionKey, runId, async () => {
    const run = await getPersistedRun(sessionKey, runId)
    if (!run) return { status: 404, error: 'Run not found' }
    if (hasActiveSendRun(runId) || run.updatedAt !== updatedAt ||
        !Number.isFinite(run.updatedAt) || Date.now() - run.updatedAt < STALE_RUN_THRESHOLD_MS ||
        ['complete', 'error'].includes(run.status)) {
      return { status: 409, error: 'Run is locally active, recent or changed; refresh first' }
    }
    const dismissed = { ...run, dismissedAt: Date.now() }
    await writeRun(dismissed)
    return { status: 200, run: dismissed }
  })
}
`;
    } else if (file.endsWith('/src/routes/api/runs/active.ts')) {
      replace("import { listAllActiveRuns } from '../../../server/run-store'",
        "import { listAllActiveRuns } from '../../../server/run-store'\nimport { hasActiveSendRun } from '../../../server/send-run-tracker'");
      replace('              status: run.status,', `              status: run.status,
              localStreamActive: hasActiveSendRun(run.runId),
              canDismiss: !hasActiveSendRun(run.runId) && Number.isFinite(run.updatedAt) && now - run.updatedAt >= 5 * 60 * 1000,`);
      replace('run.assistantText.slice(-160)', "(run.assistantText.length > 160 ? '…' : '') + run.assistantText.slice(-160)");
    } else if (file.endsWith('/src/routes/api/runs/$sessionKey.$runId.abandon.ts')) {
      replace('import { markRunStatus }', 'import { dismissRunRecord }');
      replace('if (!sessionKey || !runId)', "if (!sessionKey || ['.', '..'].includes(sessionKey) || !runId || !/^[A-Za-z0-9_-]+$/.test(runId))");
      replace(`          const run = await markRunStatus(
            sessionKey,
            runId,
            'error',
            'Abandoned by user',
          )
          if (!run) {
            return json({ ok: false, error: 'run not found' }, { status: 404 })
          }
          return json({ ok: true, run })`, `          if (request.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') {
            return json({ ok: false, error: 'JSON body required' }, { status: 415 })
          }
          const origin = request.headers.get('origin')
          // server-entry uses HTTP internally even behind Tailscale HTTPS.
          // Preserve the host/port boundary without trusting forwarded headers.
          const url = new URL(request.url)
          const tlsOrigin = url.protocol === 'http:' ? 'https://' + url.host : url.origin
          if (origin && origin !== url.origin && origin !== tlsOrigin) {
            return json({ ok: false, error: 'Cross-origin request rejected' }, { status: 403 })
          }
          const body = await request.json().catch(() => null)
          if (!body || !Number.isFinite(body.updatedAt)) {
            return json({ ok: false, error: 'Observed updatedAt required; refresh first' }, { status: 400 })
          }
          const result = await dismissRunRecord(sessionKey, runId, body.updatedAt)
          return json({ ok: result.status === 200, run: result.run, error: result.error }, { status: result.status })`);
    } else if (file.endsWith('/src/components/agent-view/background-runs-section.tsx')) {
      replace('  stalenessMs: number', '  stalenessMs: number\n  localStreamActive: boolean\n  canDismiss: boolean');
      replace("  if (run.stalenessMs >= STALE_THRESHOLD_MS) return 'bg-amber-400'", "  if (!run.localStreamActive) return 'bg-amber-400'");
      replace('bg-emerald-400 animate-pulse', 'bg-emerald-400');
      const start = code.indexOf('  const refresh = useCallback(');
      const end = code.indexOf('  const handleOpen = useCallback(');
      if (start < 0 || end <= start) throw new Error('Unsupported Workspace background polling: ' + file);
      code = code.slice(0, start) + `  const [pollError, setPollError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    const poll = async () => {
      try {
        const res = await fetch('/api/runs/active', {
          cache: 'no-store',
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]),
        })
        if (!res.ok) throw new Error('Cannot refresh run records (HTTP ' + res.status + ')')
        const data = await res.json()
        if (!data.ok || !Array.isArray(data.runs)) throw new Error('Invalid run records response')
        if (!controller.signal.aborted) { setRuns(data.runs); setPollError(null) }
      } catch (error) {
        if (!controller.signal.aborted) setPollError(error instanceof Error ? error.message : 'Cannot refresh run records')
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), POLL_INTERVAL_MS)
      }
    }
    void poll()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [])

  const handleAbandon = useCallback(async (run: BackgroundRun) => {
    if (!run.canDismiss || !window.confirm('Hide this old run record? History is preserved. This does not stop any agent, task or provider request.')) return
    const key = run.sessionKey + ':' + run.runId
    setBusyRunId(key)
    setActionError(null)
    try {
      const res = await fetch(
        '/api/runs/' + encodeURIComponent(run.sessionKey) + '/' + encodeURIComponent(run.runId) + '/abandon',
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ updatedAt: run.updatedAt }), signal: AbortSignal.timeout(15_000) },
      )
      const data = await res.json()
      if (!res.ok || !data.ok) throw new Error(data.error || 'Cannot dismiss record (HTTP ' + res.status + ')')
      setRuns(prev => prev.filter(r => !(r.sessionKey === run.sessionKey && r.runId === run.runId && r.updatedAt === run.updatedAt)))
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Cannot dismiss record')
    } finally { setBusyRunId(null) }
  }, [])

` + code.slice(end);
      replace('if (runs.length === 0) return null', 'if (runs.length === 0 && !pollError && !actionError) return null');
      replace('const staleCount = runs.filter((r) => r.stalenessMs >= STALE_THRESHOLD_MS)', 'const staleCount = runs.filter((r) => !r.localStreamActive)');
      replace('            Background runs', '            Chat run records');
      replace('`${staleCount} stale (>5m silent)`', '`${staleCount} without a registered local stream; remote task state unknown`');
      replace('`${runs.length} running`', '`${runs.length} registered local streams; provider progress not verified`');
      replace('` · ${staleCount} stale`', '` · ${staleCount} unconfirmed`');
      replace('        <CollapsiblePanel contentClassName="pt-1">', `        {pollError && <p role="alert" className="px-2 text-xs text-red-600">{pollError}. Displayed records may be outdated; retrying automatically.</p>}
        {actionError && <p role="alert" className="px-2 text-xs text-red-600">{actionError}</p>}
        <CollapsiblePanel contentClassName="pt-1">
          <p className="px-2 text-xs text-primary-500">Saved chat records, not a process monitor. A local stream does not prove provider progress. Old records do not prove that a task is still executing.</p>`);
      replace('busyRunId === run.runId', "busyRunId === run.sessionKey + ':' + run.runId");
      replace("'no output yet'", "'no saved output'");
      replace('statusColor(run),', "pollError ? 'bg-amber-400' : statusColor(run),");
      replace('title={run.sessionKey}', 'title={run.sessionKey + \' · \' + run.runId}');
      replace('{run.friendlyId || run.sessionKey}', "{run.friendlyId || run.sessionKey} · {run.runId.slice(0, 8)}");
      replace('{formatAge(run.stalenessMs)}', 'Last update: {formatAge(run.stalenessMs)}');
      replace('{run.status} · {snippet}', "{pollError ? 'Status unavailable' : run.localStreamActive ? 'Local stream registered' : 'No local stream; task state unknown'} · saved status: {run.status} · {snippet}");
      replace('                      Open\n', '                      Open chat\n');
      replace('disabled={isBusy}', 'disabled={busyRunId !== null || !run.canDismiss || !!pollError}');
      replace('title="Mark this run as failed and remove it from the active list"', 'title="Hide an old record without cancelling work or deleting history"');
      replace("{isBusy ? 'Killing…' : 'Mark dead'}", "{isBusy ? 'Dismissing…' : 'Dismiss record'}");
    } else return null;
    return { code, map: null };
  } };
}
