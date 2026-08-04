# Dispatch Node Agent

This distribution contains only the outbound Node Agent. It has no runtime
dependencies and never imports the Dispatch Center Control Plane (`app.*`).

The agent must run as a non-root account. Before any deployment, configure a
localhost or HTTPS control-plane URL, a node token, and a writable work
directory, then run:

```bash
dispatch-node-agent --check
```

Passing the self-check does not enable node assignment. The Control Plane's
node feature flags and the per-node execution backend remain disabled until
the separately documented canary decision and evidence gates are satisfied.
