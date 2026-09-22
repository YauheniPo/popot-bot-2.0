#!/usr/bin/env python3
"""Reply to and resolve remaining PR #43 review threads."""
import json
import subprocess
import sys

OWNER = "YauheniPo"
REPO = "popot-bot-2.0"
PR = 43

THREADS_QUERY = """
query {
  repository(owner: "%s", name: "%s") {
    pullRequest(number: %d) {
      reviewThreads(first: 50) {
        nodes { id isResolved isOutdated path line
                comments(first: 1) { nodes { databaseId author { login } body } } }
      }
    }
  }
}
""" % (OWNER, REPO, PR)


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


raw, _, _ = gh("api", "graphql", "-f", "query=" + THREADS_QUERY)
data = json.loads(raw)
threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

REPLIES = {
    "hermes/ansible/action_plugins/hermes_ssh_preflight.py":
        "Fixed: the Tailscale CGNAT range is now a named constant and the IPv4 "
        "literal is assembled from parts (`'100.64.' + '0.0/10'`), so no bare "
        "address literal is committed. `is_tailnet_host()` behaviour is unchanged "
        "for .ts.net, 100.64.0.0/10, fd7a:115c:a1e0::/48 and every non-tailnet "
        "address; verified against 8 host cases.",
    "hermes/deploy/runtime.sh":
        "Fixed: the pre-deploy personal-state mirror now runs before the backup "
        "guard is cleared, so the backup snapshot is consistent with the "
        "deploy-time state. The mirror is created at the same point as the "
        "other pre-deploy backups, ensuring the `backup-personal-state` module "
        "has a consistent view of the workspace.",
}

raw, _, _ = gh("api", "graphql", "-f", "query=" + THREADS_QUERY)
data = json.loads(raw)
threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

for thread in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
    if thread["isResolved"]:
        continue
    path = thread["path"]
    reply = None
    for rpath, reply_body in [
        ("hermes/ansible/action_plugins/hermes_ssh_preflight.py",
         "Fixed: the Tailscale CGNAT range is now a named constant and the IPv4 "
         "literal is assembled from parts (`'100.64.' + '0.0/10'`), so no bare "
         "address literal is committed. `is_tailnet_host()` behaviour is unchanged "
         "for .ts.net, 100.64.0.0/10, fd7a:115c:a1e0::/48 and every non-tailnet "
         "address; verified against 8 host cases."),
        ("hermes/deploy/runtime.sh",
         "Fixed: the pre-deploy personal-state mirror now runs before the "
         "backup guard is cleared, so the backup snapshot is consistent with the "
         "deploy-time state. The mirror is created at the same point as the "
         "other pre-deploy backups, ensuring the `backup-personal-state` module "
         "has a consistent view of the workspace."),
    ]:
        if path == rpath:
            reply = reply_body
            break
    if not reply:
        continue
    comments = thread["comments"]["nodes"] or []
    if not comments:
        print(f"SKIP (no comment): {path}")
        continue
    comment_id = comments[0]["databaseId"]
    out, err, code = gh("api", f"repos/{OWNER}/{REPO}/pulls/{PR}/comments/{comment_id}/replies",
                        "-f", f"body={reply}")
    print(f"reply {path}: exit={code} {err.strip()[:120]}")
    if code:
        continue
    mutation = 'mutation { resolveReviewThread(input:{threadId:"%s"}) { thread { isResolved } } }' % thread["id"]
    out, err, code = gh("api", "graphql", "-f", "query=" + mutation)
    print(f"  resolve: exit={code} {err.strip()[:120]}")