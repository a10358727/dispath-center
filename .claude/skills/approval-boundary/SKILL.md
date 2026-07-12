---
name: approval-boundary
description: Protect the dispatch-center approval and LLM/MCP security boundary. Use when changing approvals, auto-approval, mutating APIs, auth exemptions, LLM/agent tools, chat, or MCP behavior.
---

# Approval Boundary

Read `../dispatcher-domain/references/invariants.md` sections `INV-APPROVAL-*`, `INV-LLM-*`, `INV-AUDIT-*`, and `INV-TEST-2` as relevant.

Required checks:

- State-changing actions go through a pending approval; never add LLM approve/reject/shell/exec tools or direct LLM-to-SSH execution.
- Reject dangerous input when creating the request and revalidate state at approval time.
- Never expand auto-approval beyond `enqueue` and `stop` without explicit user approval.
- Keep auth default-on, audit lifecycle events, and existing boundary tests intact.

Run targeted tests, normally:

`pytest tests/test_approvals.py tests/test_autoapprove.py tests/test_agent_tools.py tests/test_mcp_bridge.py -q`

Do not contact real services, SSH hosts, or modify runtime data/config. Any failed invariant blocks completion.
