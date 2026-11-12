# Issue tracker: GitHub

Issues and specs for this repo live in GitHub Issues for
`YauheniPo/popot-bot-2.0`. Use the `gh` CLI from inside this clone.

Before the first write of a session, verify the target with
`gh repo view --json nameWithOwner`. If it differs from the repository above,
pass the configured repository explicitly with `--repo`.

## Conventions

- **Publish / fetch**: publishing creates a GitHub issue; fetching a ticket
  reads its body, labels, and comments.
- **Multi-line bodies**: write to a temporary file and pass `--body-file`.
- **Parent / child**: use native sub-issues
  (`gh issue edit <parent> --add-sub-issue <child>`). If unavailable, add the
  child to a task list in the parent and put `Part of #<parent>` at the top
  of the child body.
- **Blocking**: use native dependencies
  (`gh issue edit <child> --add-blocked-by <blocker>`). If unavailable, put
  `Blocked by: #<n>, #<n>` at the top of the child body. For either form,
  count only open blockers; a ticket is unblocked when all blockers are closed.
- **Triage roles**: resolve labels through [triage-labels.md](triage-labels.md).
- **Closing**: post the explanation as a separate comment, then close the
  issue if still open. `gh issue close --comment` can drop the comment when
  an issue is already closed.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Wayfinding operations

For `/wayfinder`, use these GitHub representations:

- **Map**: an issue labelled `wayfinder:map`; use the skill's body template.
- **Child ticket**: a sub-issue of the map, using the parent / child fallback
  above when needed. Apply `wayfinder:<type>` for the skill's ticket type.
- **Frontier query**: enumerate the map's children in map order; select the
  first open child with no assignee and no open blockers. Follow all pages
  when listing children.
- **Claim**: `gh issue edit <number> --add-assignee @me`.
- **Resolve**: comment with the answer, close the child, and update the map's
  issue body with a finding and child link in Decisions-so-far.

Create any missing `wayfinder:*` labels when first using that workflow.
