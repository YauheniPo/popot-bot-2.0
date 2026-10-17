// Managed compatibility with the pinned Workspace. Activity stays in Hermes.
export function workspaceCronUi() {
  return { name: 'hermes-cron-activity', enforce: 'pre', transform(source, id) {
    const file = id.split('?')[0];
    let code = source;
    const replace = (anchor, value) => {
      if (code.split(anchor).length !== 2) throw new Error('Unsupported Workspace cron contract: ' + file);
      code = code.replace(anchor, value);
    };
    if (file.endsWith('/src/routes/api/claude-jobs.ts')) {
      // The upstream Workspace login check still runs before the native API.
      replace('        if (aggregateProfiles) {', `        if (aggregateProfiles) {
          const response = await dashboardFetch('/api/cron/jobs?profile=all', {
            cache: 'no-store', signal: AbortSignal.timeout(15_000),
          })
          if (!response.ok) return response
          const activity = await response.json()
          if (!Array.isArray(activity)) return Response.json({ error: 'Invalid Hermes cron activity' }, { status: 502 })
          const byKey = new Map(activity.map(job => [
            (job.profile || 'default') + ':' + job.id, job.is_running,
          ]))`);
      replace('JSON.stringify({ jobs: listProfileCronJobs() })', `JSON.stringify({ jobs: listProfileCronJobs().map(job => ({ ...job,
            is_running: byKey.get((job.profile || 'default') + ':' + (job.jobId || job.id)),
          })) })`);
      replace("headers: { 'Content-Type': 'application/json' },\n          })\n        }\n        const capabilities", "headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },\n          })\n        }\n        const capabilities");
    } else if (file.endsWith('/src/lib/jobs-api.ts')) {
      replace('export type ClaudeJob = {', 'export type ClaudeJob = {\n  is_running?: boolean');
      replace('export async function fetchJobs(): Promise<Array<ClaudeJob>> {',
        'export async function fetchJobs({ signal }: { signal?: AbortSignal } = {}): Promise<Array<ClaudeJob>> {');
      replace('fetch(`${CLAUDE_API}?include_disabled=true&profiles=all`)', `fetch(
    CLAUDE_API + '?include_disabled=true&profiles=all', {
      cache: 'no-store',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15_000)]) : AbortSignal.timeout(15_000),
    })`);
    } else if (file.endsWith('/src/screens/jobs/jobs-screen.tsx')) {
      replace('  onEdit,\n}: {', '  onEdit,\n  isTriggering,\n  activityUnavailable,\n}: {');
      replace('  job: ClaudeJob\n', '  job: ClaudeJob\n  isTriggering: boolean\n  activityUnavailable: boolean\n');
      replace('  const lastRunStatus = getLastRunStatus(job)', `  const lastRunStatus = job.is_running || isTriggering
    ? { label: 'Running', color: 'var(--theme-accent)' } : getLastRunStatus(job)`);
      replace('            title="Run now"', `            title={job.is_running || isTriggering ? 'Running' : activityUnavailable || job.is_running === undefined ? 'Status unavailable' : 'Run now'}
            aria-label="Run now"
            aria-busy={job.is_running || isTriggering}
            disabled={job.is_running !== false || isTriggering || activityUnavailable}`);
      replace('              icon={PlayIcon}\n              size={14}\n              className="text-[var(--theme-accent)]"', `              icon={job.is_running || isTriggering ? RefreshIcon : PlayIcon}
              size={14}
              className={cn("text-[var(--theme-accent)]", (job.is_running || isTriggering) && "animate-spin")}`);
      replace('    refetchInterval: 30_000,', '    refetchInterval: 5_000,\n    retry: false,');
      replace('                  onTrigger={(id) => triggerMutation.mutate(id)}', `                  isTriggering={triggerMutation.isPending && triggerMutation.variables === job.id}
                  activityUnavailable={jobsQuery.isError}
                  onTrigger={(id) => triggerMutation.mutate(id)}`);
    } else return null;
    return { code, map: null };
  } };
}
