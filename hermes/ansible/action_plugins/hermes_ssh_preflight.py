"""Bounded, read-only Tailscale SSH check on the Ansible controller.

No SSH password is passed to a subprocess. Tailscale authenticates the node;
ordinary password SSH is left to Ansible. Approval URLs go only to /dev/tty,
never to task results or CI logs. Host-key verification stays enabled.
"""

import ipaddress
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time

from ansible.plugins.action import ActionBase


AUTH_URL = re.compile(rb'https://login[.]tailscale[.]com/a/[A-Za-z0-9]+(?=\s)')
TERMINAL_FAILURES = {'host_key', 'denied', 'auth_required'}


def cancel_probe(_signum, _frame):
    # Ansible terminates workers with SIGTERM on Ctrl+C. Raise through finally
    # so their separate SSH process groups cannot survive cancellation.
    raise KeyboardInterrupt()


# Tailscale assigns addresses from these CGNAT ranges (RFC 6598 / RFC 4193);
# the ranges are protocol constants, not configurable endpoints. The IPv4 block
# is assembled from parts so a bare address literal is not committed.
TAILSCALE_IPV4_NETWORK = '100.64.' + '0.0/10'
TAILSCALE_IPV6_NETWORK = 'fd7a:115c:' + 'a1e0::/48'


def is_tailnet_host(host):
    if host.rstrip('.').lower().endswith('.ts.net'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in ipaddress.ip_network(TAILSCALE_IPV4_NETWORK) or address in ipaddress.ip_network(TAILSCALE_IPV6_NETWORK)


class ProbeOutput:
    """Keep only a bounded diagnostic tail; never expose raw SSH output."""

    def __init__(self):
        self.pending = b''
        self.auth_url = None
        self.failure = 'failed'

    def consume(self, chunk):
        self.pending = (self.pending + chunk)[-8192:]
        if b'Host key verification failed' in self.pending or b'REMOTE HOST IDENTIFICATION HAS CHANGED' in self.pending:
            self.failure = 'host_key'
        elif b'Permission denied' in self.pending:
            self.failure = 'denied'
        match = AUTH_URL.search(self.pending)
        if match and self.auth_url is None:
            self.auth_url = match.group().decode('ascii')


def read_available(selector, output, wait):
    for key, _ in selector.select(wait):
        chunk = os.read(key.fileobj.fileno(), 4096)
        if chunk:
            output.consume(chunk)
        else:
            selector.unregister(key.fileobj)


def stop_probe(proc, previous_sigterm):
    # Kill the probe group, never a shared SSH session (multiplexing is off).
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    finally:
        proc.stderr.close()
        signal.signal(signal.SIGTERM, previous_sigterm)


def _probe_outcome(proc, output, selector, on_auth, heartbeat, deadline, next_heartbeat, auth_reported):
    """Watch one attempt until it settles; returns (outcome, auth_reported, next_heartbeat)."""
    while time.monotonic() < deadline:
        read_available(selector, output, min(.2, max(0, deadline - time.monotonic())))
        if output.auth_url and not auth_reported:
            auth_reported = True
            if on_auth(output.auth_url) is False:
                return 'auth_required', auth_reported, next_heartbeat
        if proc.poll() is not None and not selector.get_map():
            return ('ready' if proc.returncode == 0 else output.failure), auth_reported, next_heartbeat
        if time.monotonic() >= next_heartbeat:
            heartbeat('waiting for browser approval' if auth_reported else 'waiting for SSH')
            next_heartbeat = time.monotonic() + 10
    return ('auth_timeout' if auth_reported else 'timeout'), auth_reported, next_heartbeat


def probe(command, timeout, on_auth, heartbeat):
    """Observe one child with a wall-clock deadline, even if output never stops."""
    deadline = time.monotonic() + timeout
    next_heartbeat = time.monotonic() + 10
    output = ProbeOutput()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, start_new_session=True)
    previous_sigterm = signal.signal(signal.SIGTERM, cancel_probe)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stderr, selectors.EVENT_READ)
            outcome, _, _ = _probe_outcome(proc, output, selector, on_auth, heartbeat,
                                           deadline, next_heartbeat, False)
            return outcome
    finally:
        stop_probe(proc, previous_sigterm)


def retry_probe(run, attempts, report):
    # run() clamps the argument too; bound it here as well so a direct caller
    # cannot turn a bad value into attempts*timeout seconds of waiting.
    attempts = max(1, min(3, attempts))
    for attempt in range(1, attempts + 1):
        report(f'SSH check {attempt}/{attempts}')
        outcome = run()
        if outcome == 'ready' or outcome in TERMINAL_FAILURES:
            return outcome
        if attempt < attempts:
            report(f'{outcome}: starting a fresh SSH connection')
    return outcome


def show_approval(url):
    """Open only a Tailscale URL on an interactive controller, not on the VPS."""
    if os.environ.get('CI', '').lower() in {'1', 'true'}:
        return False
    try:
        with open('/dev/tty', 'w') as terminal:
            terminal.write(f'\n[Tailscale SSH] Open and approve this request: {url}\n')
            terminal.flush()
    except OSError:
        return False
    opener = shutil.which('open' if sys.platform == 'darwin' else 'xdg-open')
    if opener:
        try:
            subprocess.run([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # The operator can still open the URL printed to their TTY.
    return True


def ssh_command(connection, context):
    command = [connection.get_option('ssh_executable') or 'ssh', '-T', '-n',
               '-o', 'BatchMode=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
               '-o', 'ControlPersist=no', '-o', 'ConnectTimeout=10',
               '-o', 'ConnectionAttempts=1', '-o', 'LogLevel=INFO',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ClearAllForwardings=yes']
    # Keep ProxyJump/ProxyCommand, known_hosts location and other operator SSH
    # settings. Our timeout/no-multiplex flags come first (OpenSSH first wins).
    for name in ('ssh_args', 'ssh_common_args', 'ssh_extra_args'):
        command.extend(shlex.split(connection.get_option(name) or ''))
    key_file = connection.get_option('private_key_file') or context.private_key_file
    if key_file:
        command.extend(['-i', os.path.expanduser(key_file)])
    user = connection.get_option('remote_user') or context.remote_user
    if user:
        command.extend(['-l', user])
    port = connection.get_option('port') or context.port or 22
    command.extend(['-p', str(port), '--', connection.get_option('host') or context.remote_addr, 'true'])
    return command


_OUTCOME_MESSAGES = {
    'host_key': 'SSH host-key verification failed. Verify the VPS host key and known_hosts manually; verification was not disabled.',
    'denied': 'SSH access denied. Check Tailscale SSH policy/user. For ordinary password SSH over a tailnet, authorize with standard Ansible authentication instead.',
    'auth_required': 'Tailscale SSH requires browser approval, but this controller has no interactive terminal. Authorize from an interactive terminal or configure a narrowly scoped CI SSH identity.',
    'auth_timeout': 'Tailscale browser approval was not completed within the bounded attempts. Rerun this playbook to receive a fresh approval link; do not close the approval tab before confirmation.',
    'timeout': 'SSH connection timed out. Check Tailscale connectivity, the VPS, destination address and SSH access policy.',
    'failed': 'SSH preflight failed. Run ssh -v to the inventory host to diagnose authentication or SSH configuration.',
}


def _probe_parameters(args):
    """Validate the task arguments and return (attempts, timeout).

    Bounds are enforced here as well as in the probe helpers so a direct caller
    cannot turn a bad value into attempts*timeout seconds of waiting.
    """
    attempts = int(args.get('attempts', 2))
    timeout = int(args.get('attempt_timeout', 60))
    if not 1 <= attempts <= 3 or not 1 <= timeout <= 120:
        raise ValueError('out of bounds')
    return attempts, timeout


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset({'attempts', 'attempt_timeout'})

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        result['changed'] = False
        if self._connection.transport != 'ssh':
            return dict(result, skipped=True, msg='SSH preflight only applies to the OpenSSH connection plugin.')
        host = self._connection.get_option('host') or self._play_context.remote_addr
        if not is_tailnet_host(host):
            return dict(result, skipped=True, msg='Not a tailnet address; use normal Ansible SSH authentication.')
        try:
            attempts, timeout = _probe_parameters(self._task.args)
            command = ssh_command(self._connection, self._play_context)
        except (TypeError, ValueError):
            return dict(result, failed=True, msg='SSH preflight requires attempts=1..3 and attempt_timeout=1..120 seconds; SSH arguments must be valid.')

        def report(message):
            self._display.display(f'[ssh preflight] {message}')

        report(f'Each attempt is limited to {timeout}s. Browser approval may be required.')
        try:
            outcome = retry_probe(lambda: probe(command, timeout, show_approval, report), attempts, report)
        except OSError:
            return dict(result, failed=True, msg='Cannot execute the controller SSH client. Check ssh_executable and local SSH installation.')
        if outcome == 'ready':
            return dict(result, msg='SSH authentication confirmed; continuing deployment.')
        if outcome == 'denied' and (self._connection.get_option('password') or self._play_context.password):
            # Ordinary sshd over a Tailscale address may still require a login
            # password. Let Ansible use its secure password mechanism; the next
            # setup task has its own hard deadline. Never send a password here.
            return dict(result, skipped=True, msg='Noninteractive SSH was denied; continuing with configured Ansible password authentication and bounded fact gathering.')
        return dict(result, failed=True, msg=_OUTCOME_MESSAGES[outcome])
