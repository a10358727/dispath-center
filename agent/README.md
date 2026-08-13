# Dispatch Node Agent

This distribution contains only the outbound Node Agent. It has no runtime
dependencies and never imports the Dispatch Center Control Plane (`app.*`).

The agent must run as a non-root account. Before any deployment, configure a
localhost or HTTPS control-plane URL, a node token, and a writable work
directory, then run:

```bash
dispatch-node-agent --check
```

After the local check passes, `dispatch-node-agent --probe` performs an
authenticated, read-only `2.0` protocol handshake and prints safe capability
metadata. It does not lease work or enable assignment.

Set `DISPATCH_NODE_ISOLATION_MODE=direct` for development/rollback or
`systemd` for the detached transient-unit launcher. `DISPATCH_NODE_DEPLOYMENT_TIER`
values `canary` and `production` reject `direct`; an unavailable systemd user
manager fails closed and never silently falls back. The installable systemd
template opts workloads into the strict environment allowlist.
`agent.isolation.build_transient_attempt_argv()` provides a detached
per-attempt unit contract with a private user namespace, read-only control
evidence, and bounded resource properties.

Each attempt keeps workload-owned files under `attempt/<id>/workload/`; the
agent-owned `control/<id>/` tree stores immutable isolation and launch receipts
and validated terminal evidence. The workload directory is never used as the
control evidence path.

On a Linux worker with a user systemd manager, run
`tests/test_node_isolation_integration.py` for the real transient-unit smoke:
it checks credential removal, control-path write protection, cgroup stop,
Agent-store restart recovery, resource properties, and terminal evidence
collection. It is skipped when user systemd is unavailable.

Passing the self-check does not enable node assignment. The Control Plane's
node feature flags and the per-node execution backend remain disabled until
the separately documented canary decision and evidence gates are satisfied.
