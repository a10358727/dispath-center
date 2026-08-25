# UI state coverage and domain-specific rules

Detail companion to `SKILL.md`. Apply these wherever the feature touches them.

## State coverage

Each state needs a distinct, readable rendering; none may be silently
collapsed into another:

| State | Requirement |
|---|---|
| loading | distinguishable from empty |
| empty | says what is absent, not "0" alone |
| error | shows what failed and what the user can do |
| success | reflects the server's response, not the request |
| **stale** | says when data was last confirmed; never present cached data as live |
| **unknown** | missing/unreadable evidence renders as unknown, never as success or failure |
| **disconnected** | streaming/polling loss is visible, non-destructive, and shows reconnect state |
| **partial / legacy** | partially-evidenced or legacy-observed records are labelled as such |
| **blocked / pending approval** | shows that an action is waiting on a human decision, and who may decide |

## Domain-specific UI rules

- **Onboarding**: scan results, candidates, and instance states are server
  observations. Render `missing`/`dirty`/`diverged`/`busy`/`unknown` honestly and
  never offer a one-click path that would force a dirty checkout clean. Never
  display file contents that the read-only scan is not allowed to read.
- **Approvals**: the UI proposes; it never decides on the server's behalf.
  Show the actual operation in plain language, keep the requester and the
  decider visible, and disable a decision control that the current actor is not
  permitted to use rather than failing after the click.
- **Diff review**: render diffs as text nodes, never as HTML. Show the exact
  base revision and whether the worktree is clean. Never present an unpromoted
  or dirty result as promotable.
- **Test results**: show the command, its exit evidence, and truncation
  boundaries. Absent output is unknown, not pass.
- **Workspace / agent session UI**: session identity, transcript,
  and workspace status must be reloadable from the server after a refresh or a
  reconnect. Streamed tokens are a progressive view of a server-recorded turn,
  not the record itself; an interrupted stream shows disconnected and resumes
  from server state. No client-side transcript is authoritative.
- **Development Agent provider selector**: the option list comes from the
  server's enabled + approved provider registry, never a hardcoded client
  list (a single available provider may hide the selector); if an Auto mode
  exists, the UI shows the provider the **server** selected and why, and
  never guesses or displays a provider the server has not confirmed;
  selection is a request, not a privilege — the UI must not imply a provider
  grants different permissions; unavailable providers render as unavailable,
  not hidden errors.
- **Paths and identifiers**: internal filesystem paths, credentials, and secret
  values are never rendered. Render server-supplied text through text nodes.
