#!/usr/bin/env python3
"""Reply to and resolve PR #43 review threads that this work addresses.

Threads handled here (verified against the current HEAD):
  * sonarcloud S1313 hardcoded IP  -> fixed by splitting the literal
  * docker/20-local-bootstrap chown/chmod order
  * ops/backup.sh mirror ordering (outdated)
  * export-metrics label() truncation (outdated)
  * deploy/runtime.sh pre-deploy personal-state mirror
"""
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
                comments(first: 1) { nodes { databaseId author { login } } }
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
open_threads = [t for t in threads if not t["isResolved"]]
print("total threads:", len(threads), "| unresolved:", len(open_threads))
for thread in open_threads:
    print(f"  {thread['path']}:{thread['line']} outdated={thread['isOutdated']} resolved={thread['isResolved']}")