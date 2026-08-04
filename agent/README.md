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

The installable systemd template opts workloads into the PR-10 strict
environment allowlist. `agent.isolation.build_transient_attempt_argv()` also
provides a detached per-attempt unit contract with a private user namespace,
read-only control evidence, and bounded resource properties. The direct
supervisor path remains available as an explicit rollback mode until a real
worker canary proves the host's systemd policy.

Passing the self-check does not enable node assignment. The Control Plane's
node feature flags and the per-node execution backend remain disabled until
the separately documented canary decision and evidence gates are satisfied.
