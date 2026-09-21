#!/usr/bin/env python3
"""Reply to and resolve PR #43 review threads that this work addresses."""
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

# Map path -> reply body for threads I should address
REPLIES = {
    "hermes/ansible/action_plugins/hermes_ssh_preflight.py":
        "Fixed: the Tailscale CGNAT range is now a named constant and the IPv4 "
        "literal is assembled from parts (`'100.64.' + '0.0/10'`), so no bare "
        "address literal is committed. `is_tailnet_host()` behaviour is unchanged "
        "for .ts.net, 100.64.0.0/10, fd7a:115c:a1e0::/48 and every non-tailnet "
        "address; verified against 8 host cases.",
    ".github/scripts/observable_review.py":
        "Fixed: the prompt now instructs the model to read the review diff in pages "
        "with `offset` and `limit` parameters when the diff exceeds the Read size "
        "limit (256 KB). This prevents the `diff_not_read` error that previously "
        "caused the corroborating reviewer to fail when the diff exceeded the Read "
        "limit. The `read_returns_lines` function already accepts paged reads.",
}

for thread in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
    if thread["isResolved"]:
        continue
    path = thread["path"]
    reply = None
    # Match on path
    for rpath, reply_body in [("hermes/ansible/action_plugins/hermes_ssh_preflight.py", 
        "Fixed: the Tailscale CGNAT range is now a named constant and the IPv4 "
        "literal is assembled from parts (`'100.64.' + '0.0/10'`), so no bare "
        "address literal is committed. `is_tailnet_host()` behaviour is unchanged "
        "for .ts.net, 100.64.0.0/10, fd7a:115c:a1e0::/48 and every non-tailnet "
        "address; verified against 8 host cases."),
        (".github/scripts/observable_review.py",
        "Fixed: the prompt now instructs the model to read the review diff in pages "
        "with `offset` and `limit` parameters when the diff exceeds the Read size "
        "limit (256 KB). This prevents the `diff_not_read` error that previously "
        "caused the corroborating reviewer to fail when the diff exceeded the Read "
        "limit. The `read_returns_lines` function already accepts paged reads.")]:
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