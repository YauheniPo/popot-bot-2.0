// Compatibility for the pinned Swarm; source checkout stays pristine.
function transformSwarmActivity(source) {
  let code = source;
  const replace = (anchor, value) => {
    if (code.split(anchor).length !== 2) throw new Error('Unsupported Workspace Swarm activity contract');
    code = code.replace(anchor, value);
  };
  replace('  currentTask?: string | null', '  currentTask?: string | null\n  lastResult?: string | null\n  lastSummary?: string | null');
  replace("kind: 'tail' | 'session' | 'task'", "kind: 'tail' | 'session' | 'task' | 'runtime'");
  replace('function relativeTime(', `function activityTimestamp(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null
  const milliseconds = value < 100_000_000_000 ? value * 1000 : value
  return milliseconds > Date.now() + 60_000 ? null : milliseconds
}

function relativeTime(`);
  replace("  if (!ts) return 'just now'\n  const diff = Date.now() - ts", "  const timestamp = activityTimestamp(ts)\n  if (timestamp === null) return 'time unknown'\n  const diff = Math.max(0, Date.now() - timestamp)");
  const start = code.indexOf('function buildRows(');
  const end = code.indexOf('export function Swarm2ActivityFeed(');
  if (start < 0 || end <= start || code.split('function buildRows(').length !== 2) {
    throw new Error('Unsupported Workspace Swarm activity contract');
  }
  code = code.slice(0, start) + `function buildRows(
  members: Array<CrewMember>,
  runtime: Map<string, RuntimeEntry>,
): Array<ActivityRow> {
  const rows: Array<ActivityRow> = []
  for (const member of members) {
    const entry = runtime.get(member.id)
    const candidates: Array<ActivityRow> = []
    const add = (text: string | null | undefined, ts: number | null | undefined, kind: ActivityRow['kind']) => {
      if (text?.trim()) candidates.push({ id: member.id + '-' + kind, workerId: member.id,
        workerName: member.displayName || member.id, text: text.trim(), ts: activityTimestamp(ts), kind })
    }
    const result = entry?.lastResult || entry?.lastSummary
    // lastOutputAt dates the runtime snapshot; it is not a result timestamp.
    add(entry?.currentTask || result, entry?.lastOutputAt, 'runtime')
    add(member.lastSessionTitle, member.lastSessionAt, 'session')
    // Do not attach a task's timestamp (or file mtime) to an unrelated log line.
    for (const raw of (entry?.recentLogTail || '').split('\\n').reverse()) {
      const text = stripLogPrefix(raw)
      if (!text || /^INFO\\s+hermes_cli\\.mem_trim:/.test(text)) continue
      // Only a timestamp with an explicit timezone is safe in a browser.
      const stamp = raw.match(/^\\[?(\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}:\\d{2}(?:[.,]\\d+)?(?:Z|[+-]\\d{2}:?\\d{2}))/)
      add(text, stamp ? Date.parse(stamp[1].replace(',', '.').replace(' ', 'T')) : null, 'tail')
      break
    }
    candidates.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))
    if (candidates.length) rows.push(candidates[0])
  }
  return rows.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))
}

` + code.slice(end);
  replace('Latest signals across all wired workers', 'Latest recorded events, not live readiness; routine memory-trim logs hidden');
  replace('No worker output captured yet. Once swarm TUIs emit logs they will show\n          up here, ordered by latest event.',
    'No meaningful worker events recorded yet. Routine memory-trim logs are hidden.');
  replace('{row.text}', "{row.kind === 'tail' ? 'Log: ' : row.kind === 'session' ? 'Session: ' : 'Runtime snapshot: '}{row.text}");
  return code;
}

function transformSwarmReports(source) {
  let code = source;
  const replace = (anchor, replacement, count = 1) => {
    if (code.split(anchor).length !== count + 1) throw new Error('Unsupported Workspace Swarm report contract');
    code = code.split(anchor).join(replacement);
  };
  replace('function splitChangedFiles(', `type ReportScope = 'tasks' | 'installation' | 'runtime'

function isReportedFile(value: string | null | undefined): boolean {
  return Boolean(value?.trim()) && !['none', 'n/a', '[]', '—'].includes(value!.trim().toLowerCase())
}

function selectSwarmReportRows(rows: Array<Swarm2ReportRow>, scope: ReportScope): Array<Swarm2ReportRow> {
  return rows.filter(row => {
    if (scope === 'runtime') return row.kind !== 'checkpoint'
    if (row.kind !== 'checkpoint') return false
    const installation = row.missionTitle === 'Managed Swarm installation smoke check'
      || row.title.startsWith('Installation smoke check only.')
    return scope === 'installation' ? installation : !installation
  })
}

function reportFileCount(rows: Array<Swarm2ReportRow>): number {
  return new Set(rows.flatMap(row => row.artifacts)
    .map(artifact => artifact.path?.trim()).filter(isReportedFile)).size
}

function splitChangedFiles(`);
  replace('    .filter(Boolean)\n    .slice(0, 12)',
    '    .filter(isReportedFile)\n    .filter((path, index, paths) => paths.indexOf(path) === index)\n    .slice(0, 12)');
  replace('artifacts: inferredArtifacts.length ? inferredArtifacts : runtime?.artifacts ?? [],', 'artifacts: inferredArtifacts,');
  replace('previews: runtime?.previews ?? [],', 'previews: [],');
  replace('updatedAt: assignment.completedAt ?? mission.updatedAt ?? runtime?.lastOutputAt ?? null,',
    'updatedAt: assignment.completedAt ?? assignment.dispatchedAt ?? mission.updatedAt ?? null,');
  replace(`      const stateRank = statePriority(a.state) - statePriority(b.state)
      if (stateRank !== 0) return stateRank
      return (b.updatedAt ?? 0) - (a.updatedAt ?? 0)`,
    `      return (b.updatedAt ?? 0) - (a.updatedAt ?? 0) || statePriority(a.state) - statePriority(b.state)`);
  replace('artifactCount: workerRows.reduce((sum, row) => sum + row.artifacts.length + row.previews.length, 0),',
    'artifactCount: reportFileCount(workerRows),');
  replace("  const [stateFilter, setStateFilter] = useState<ReportState>('all')",
    "  const [reportScope, setReportScope] = useState<ReportScope>('tasks')\n  const [stateFilter, setStateFilter] = useState<ReportState>('all')");
  replace('  const rows = useMemo(() => buildSwarm2ReportRows({ missions, runtimes }), [missions, runtimes])',
    `  const allRows = useMemo(() => buildSwarm2ReportRows({ missions, runtimes }), [missions, runtimes])
  const rows = useMemo(() => selectSwarmReportRows(allRows, reportScope), [allRows, reportScope])`);
  replace('  const inboxLanes = useMemo(() => buildSwarm2InboxLanes({ missions, runtimes }), [missions, runtimes])\n', '');
  replace('() => missions.map((mission) => ({ id: mission.id, label: mission.title || mission.id })),\n    [missions],',
    '() => missions.filter(mission => rows.some(row => row.missionId === mission.id)).map((mission) => ({ id: mission.id, label: mission.title || mission.id })),\n    [missions, rows],');
  replace('  const workerCards = useMemo(() => buildWorkerReportCards(filteredRows), [filteredRows])',
    `  const inboxLanes: Swarm2InboxLanes = { needs_review: [], blocked: [], ready: [] }
  for (const row of filteredRows) {
    if (row.kind === 'checkpoint' && (row.state === 'needs_review' || row.state === 'blocked' || row.state === 'ready')) {
      inboxLanes[row.state].push({ ...row, lane: row.state })
    }
  }
  const workerCards = useMemo(() => buildWorkerReportCards(filteredRows), [filteredRows])`);
  replace('  const counts = rows.reduce', '  const counts = filteredRows.reduce');
  replace('Board for queues, Cards for worker-level scanning, List for dense detail.',
    'Counts describe report records, not live worker readiness. Cards show the latest report; older failures remain in history. Files counts unique reported paths, not verified filesystem changes.');
  replace('        {STATE_FILTERS.map((filter) => (', `        <select aria-label="Report source" value={reportScope} onChange={(event) => {
          setReportScope(event.target.value as ReportScope)
          setStateFilter('all'); setWorkerFilter('all'); setMissionFilter('all'); setExpandedId(null)
          if (event.target.value === 'runtime') setLayout('cards')
        }} className="rounded-full border px-3 py-1.5 text-xs bg-[var(--theme-bg)] text-[var(--theme-text)]">
          <option value="tasks">Task reports</option>
          <option value="installation">Installation checks</option>
          <option value="runtime">Runtime snapshots (not live readiness)</option>
        </select>
        {STATE_FILTERS.map((filter) => (`);
  replace('onClick={() => setLayout(id)}', "disabled={reportScope === 'runtime' && id === 'board'}\n              onClick={() => setLayout(id)}");
  replace('>{card.stateLabel}</span>', '>Latest report: {card.stateLabel}</span>');
  replace('Review {counts.needs_review}', 'Review reports {counts.needs_review}');
  replace('Ready {counts.ready}', 'Ready reports {counts.ready}');
  replace('Blocked {counts.blocked}', 'Blocked reports {counts.blocked}');
  replace('Review {card.reviewCount}', 'History: review {card.reviewCount}');
  replace('Ready {card.readyCount}', 'History: ready {card.readyCount}');
  replace('Blocked {card.blockedCount}', 'History: blocked {card.blockedCount}');
  replace('Files {card.artifactCount}', 'Unique files {card.artifactCount}');
  // Every record remains accessible, including installation failures.
  replace('card.rows.slice(0, 4).map', 'card.rows.map');
  return code;
}

export function workspaceSwarmRuntime() {
  const replacements = {
    '/src/server/swarm-roster.ts': [
      "export const SWARM_ROSTER_PATH = join(SWARM_CANONICAL_REPO, 'swarm.yaml')",
      "export const SWARM_ROSTER_PATH = process.env.HERMES_SWARM_ROSTER || join(SWARM_CANONICAL_REPO, 'swarm.yaml')",
    ],
    '/src/server/swarm-environment.ts': [
      'export const SWARM_CANONICAL_REPO = resolve(process.cwd())',
      'export const SWARM_CANONICAL_REPO = resolve(process.env.HERMES_SWARM_REPO || process.cwd())',
    ],
    '/src/routes/api/swarm-dispatch.ts': [
      "return ['chat', '-q', prompt, '-Q', '--yolo', '--ignore-rules', '--source', 'swarm-dispatch']",
      "return ['chat', '-q', prompt, '-Q', '--source', 'swarm-dispatch']",
    ],
  };
  return { name: 'hermes-managed-swarm-runtime', enforce: 'pre', transform(source, id) {
    if (id.split('?')[0].endsWith('/src/screens/swarm2/swarm2-activity-feed.tsx')) {
      return { code: transformSwarmActivity(source), map: null };
    }
    if (id.split('?')[0].endsWith('/src/screens/swarm2/swarm2-reports-view.tsx')) {
      return { code: transformSwarmReports(source), map: null };
    }
    const entry = Object.entries(replacements).find(([path]) => id.split('?')[0].endsWith(path));
    if (!entry) return null;
    const [anchor, replacement] = entry[1];
    if (source.split(anchor).length !== 2) throw new Error('Unsupported Workspace Swarm runtime contract');
    let code = source.replace(anchor, replacement);
    if (id.split('?')[0].endsWith('/src/routes/api/swarm-dispatch.ts')) {
      const promptAnchor = "  const normalizedPrompt = prompt.replace(/\\r\\n/g, '\\n')";
      const pasteAnchor = "    'paste-buffer',\n    '-d',";
      if (code.split(promptAnchor).length !== 2 || code.split(pasteAnchor).length !== 2) {
        throw new Error('Unsupported Workspace Swarm terminal contract');
      }
      code = code.replace(promptAnchor, `  // A tmux pane exists before the Hermes TUI can accept input.
  const readyDeadline = Date.now() + 40_000
  let ready = false
  while (Date.now() < readyDeadline) {
    const pane = await captureTmuxPane(tmuxBin, sessionName)
    if (pane.includes('─ ready') && pane.includes('❯')) { ready = true; break }
    if (!(await tmuxHasSession(tmuxBin, sessionName))) break
    await sleep(500)
  }
  if (!ready) return { workerId, ok: false, output: '',
    error: 'Worker TUI did not become ready within 40 seconds; inspect its terminal',
    durationMs: Date.now() - startedAt, exitCode: null, delivery: 'tmux' }
` + promptAnchor).replace(pasteAnchor, "    'paste-buffer',\n    '-p',\n    '-d',");
      // An initial progress checkpoint is not the result of the task.
      for (const name of ['checkpoint', 'runtimeCheckpoint']) {
        const anchor = `if (${name} && ${name}.raw !== previousRaw) return ${name}`;
        if (code.split(anchor).length !== 2) throw new Error('Unsupported Swarm checkpoint contract');
        code = code.replace(anchor,
          `if (${name} && ${name}.stateLabel !== 'IN_PROGRESS' && ${name}.raw !== previousRaw) return ${name}`);
      }
    }
    return { code, map: null };
  } };
}
