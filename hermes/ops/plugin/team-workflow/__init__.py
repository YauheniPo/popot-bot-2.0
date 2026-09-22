"""Native Hermes team orchestration; completion routing remains host-owned."""
import json
import copy
from pathlib import Path
import re
import subprocess

from .engine import Engine


class NativeBackend:
    def __init__(self, parent, owner, root, workspace):
        self.parent, self.owner = parent, owner
        self.root, self.workspace = root, workspace

    def spawn(self, brief, schema):
        from tools.delegate_tool import delegate_task
        return self._normalize(json.loads(delegate_task(goal=brief, output_schema=schema,
                                        role='leaf', background=True, parent_agent=self.parent)))

    @staticmethod
    def _normalize(result):
        from tools.delegation_output_schema import extract_json_candidate
        result = copy.deepcopy(result)
        for entry in result.get('results', []):
            if isinstance(entry, dict) and isinstance(entry.get('summary'), str):
                entry['summary'] = extract_json_candidate(entry['summary'])
        return result

    def result(self, ident):
        from tools.async_delegation import get_durable_delegation
        record = get_durable_delegation(ident)
        if record and isinstance(record.get('result'), dict):
            record = {**record, 'result': self._normalize(record['result'])}
        return record

    def stop(self, ids):
        from tools.delegate_tool import delegate_task
        for ident in ids:
            delegate_task(action='stop', subagent_id=ident, parent_agent=self.parent)

    def prepare_worktree(self, task_id, repository):
        if not isinstance(repository, str) or not Path(repository).is_absolute():
            raise ValueError('Code tasks require an absolute repository path')
        source = Path(repository).resolve(strict=True)
        if not source.is_relative_to(self.workspace.resolve(strict=True)):
            raise ValueError('Repository must be inside the managed workspace')
        if not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('Invalid task identity')
        target = self.root / 'worktrees' / task_id
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if target.parent.is_symlink() or target.exists():
            raise ValueError('Worktree target already exists or is unsafe')
        # Never fetch, checkout, clean or reset the source; use committed HEAD.
        subprocess.run(['git', '-C', str(source), 'worktree', 'add', '--detach', str(target), 'HEAD'],
                       check=True, capture_output=True, timeout=60)
        return str(target)


def handle(args, **kwargs):
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config
        from tools.approval import get_current_session_key
        from gateway.session_context import async_delivery_supported

        parent = kwargs.get('parent_agent') or get_active_subagent_parent()
        if parent is None or getattr(parent, '_delegate_depth', 0) != 0:
            raise ValueError('team_workflow is available only to the main coordinator')
        owner = get_current_session_key(default='')
        if not owner or not async_delivery_supported():
            raise ValueError('Team tasks require a routable interactive session; use ordinary synchronous work here')
        config = load_config()
        policy = config.get('team_workflow', {})
        if not policy.get('enabled', False):
            raise ValueError('Team workflow is disabled')
        home = Path(get_hermes_home())
        root = home / 'team-workflow'
        workspace = Path(config.get('terminal', {}).get('cwd') or '')
        if not workspace.is_absolute():
            raise ValueError('Configure an absolute terminal.cwd for team tasks')
        engine = Engine(root, NativeBackend(parent, owner, root, workspace), policy)
        arguments = dict(args)
        action = arguments.pop('action', 'status')
        return json.dumps(engine.call(owner, action, **arguments), ensure_ascii=False)
    except ValueError as error:
        return json.dumps({'error': str(error)}, ensure_ascii=False)
    except Exception as error:
        # Provider stderr, paths and config values may contain credentials.
        return json.dumps({'error': 'Team controller failed; no automatic replay', 'type': type(error).__name__})


def register(ctx):
    ctx.register_tool(
        # Join the existing opt-in delegation toolset; do not bypass a platform
        # where the operator disabled delegation, or require a new UI toggle.
        name='team_workflow', toolset='delegation', handler=handle,
        description='Bounded research / execution / independent review loop with native completion delivery.',
        schema={
            'name': 'team_workflow',
            'description': (
                'Coordinate nontrivial user tasks. Simple answers need no team. '
                'start launches Researcher, then on EVERY native TEAM completion call advance with the task_id. '
                'advance validates the saved native result and launches the next stage, or returns a final outcome. '
                'Never claim completion before status=completed; report blocked/needs_input/timeout honestly. '
                'Reuse request_id on duplicate starts; never poll. Only one team task per Hermes home. '
                'Use list/status for user progress requests and cancel before replacing or cancelling a task. '
                'Code mode needs an explicit repository inside terminal.cwd and uses a new detached worktree of HEAD. '
                'Workers cannot publish, deploy or send external messages; the main agent handles owner-authorized actions after review.'),
            'parameters': {'type': 'object', 'additionalProperties': False,
                           'required': ['action'], 'properties': {
                'action': {'type': 'string', 'enum': ['start', 'advance', 'status', 'list', 'cancel']},
                'task_id': {'type': 'string'},
                'request_id': {'type': 'string', 'description': 'Stable request label; reuse only for retries of this same request.'},
                'objective': {'type': 'string'},
                'acceptance': {'type': 'string', 'description': 'Concrete criteria and evidence needed to finish.'},
                'context': {'type': 'string', 'description': 'Compact context pack: response language, relevant preferences, verified facts with source/date, uncertainties, instruction paths and allowed actions. No secrets or full memory dumps; children do not inherit main memory/SOUL.'},
                'mode': {'type': 'string', 'enum': ['research', 'writing', 'code', 'diagnostic', 'planning', 'general']},
                'repository': {'type': 'string'},
            }},
        })
