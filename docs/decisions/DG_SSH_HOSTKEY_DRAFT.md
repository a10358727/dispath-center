# DG-SSH-HOSTKEY — Internet SSH host identity policy

> Date: 2026-09-19
>
> Contract revision: `DG-SSH-HOSTKEY-v1`
>
> Status: **approved 2026-09-23.** The authoritative ruling is recorded in
> [`docs/DECISIONS.md`](../DECISIONS.md); this file remains the decision's
> provenance and question record.
>
> Authoritative decision record after ruling: [`docs/DECISIONS.md`](../DECISIONS.md)
>
> Governing invariant: [`docs/PLATFORM_CHARTER.md`](../PLATFORM_CHARTER.md)
> §6, `INV-SSH-8`.

## 1. Current boundary and reason for the gate

The current `app/sshpool.py` policy uses `known_hosts=None`, an explicitly
recorded trade-off under the Tailscale private-network assumption. `INV-SSH-8`
requires an architecture decision before changing that policy and requires new
SSH call paths to remain consistent with it.

V0.1 also describes Internet-facing rental endpoints with custom SSH ports.
Fingerprint display, human trust, pinning, later mismatch handling, or any
related storage and migration behavior therefore remain **blocked pending this
decision**. No implementation is authorized by this draft.

## 2. Questions requiring an explicit human ruling

The reviewer must rule on each item below; this packet intentionally does not
select a policy.

| # | Question requiring a ruling |
|---|---|
| H-1 | What trust bootstrap is permitted for a first connection, and what counts as out-of-band fingerprint verification? |
| H-2 | How are endpoint, custom port, and server revision bound to a host identity, and what is the approved replacement procedure? |
| H-3 | Where are pins stored, what migration is required from the current policy, and how must all `asyncssh` and `rsync` paths use the same decision? |
| H-4 | Which authorized human may trust, replace, rotate, or revoke a pin, and what audit record is required? |
| H-5 | What happens on a mismatch? The ruling must preserve nonexecution and existing Job truth; a mismatch must not be translated into a new Job lifecycle meaning or rewrite an existing result. |

Any selected contract must also preserve the Charter boundaries: SSH remains a
governed Compute path, unreachable remains distinct from failure, and
credentials remain outside agent and UI surfaces; any fingerprint and audit
projection must retain existing access controls. See
[`docs/PLATFORM_CHARTER.md`](../PLATFORM_CHARTER.md) §4.3–§4.4 and `INV-SSH-1`
through `INV-SSH-9`.

## 3. Proposed evidence after approval

After a human ruling, the implementation packet should add offline tests for
the approved contract covering:

- first-connection trust bootstrap and verified match;
- mismatch blocks execution without changing existing Job truth;
- authorized rotation and replacement, including endpoint/port/server revision
  binding;
- custom-port handling;
- consistency across every `asyncssh` and `rsync` path;
- absence of credentials and secret material from user-visible or audit
  payloads; public fingerprint evidence follows the approved contract.

These are acceptance proposals only. They do not authorize a new validation
mechanism or implementation until the ruling is recorded in
[`docs/DECISIONS.md`](../DECISIONS.md).

## 4. Decision record

- [x] Approved as `DG-SSH-HOSTKEY-v1`: OOB-first with explicit TOFU fallback.
- [x] H-1…H-5 are answered in the authoritative decision log dated
      2026-09-23.
- [x] The ruling adds no approval kind, lifecycle state, provider, or arbitrary
      remote-shell capability.
