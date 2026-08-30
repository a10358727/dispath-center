# DG-OPS-SLO — What "production-ready" is allowed to mean

> Date: 2026-07-28
>
> Contract revision: `DG-OPS-SLO-v1`
>
> Status: **draft for review — not approved.** Until this is ruled on, no
> capability may be marked `production-ready=yes`.
>
> Authoritative decision record after ruling: `docs/DECISIONS.md`
>
> Reviewed-draft SHA-256: `<computed at ruling time>`
>
> Required approval phrase:
> `DG-OPS-SLO v1：核准本文件的 recommended contract`

Required before any `production-ready=yes` claim in
`docs/CAPABILITY_LEDGER.md` (plan §11). This gate is unusual: it does not
unlock code. It decides **what evidence entitles the project to stop calling
itself an internal MVP** — which is precisely the claim that is easiest to make
carelessly and most expensive to be wrong about.

---

## 1. Recommended decision

Fix these numbers and this evidence bar:

| Target | Recommended | Why this number |
|---|---|---|
| RPO (max data loss) | **24 hours** | A daily backup is operable by one person without automation debt. Tighter targets need WAL shipping, which is a larger commitment than this system currently justifies. |
| RTO (max time to serve again) | **4 hours** | Measured, not guessed: `scripts/restore_drill.py` reports actual restore duration, and 4h leaves room for diagnosis rather than just file copying. |
| Backup age alert | **> 26 hours** | Slightly above RPO so a slow backup does not page, but a *missed* one does. |
| Restore drill cadence | **quarterly, recorded** | A backup nobody has restored is a hypothesis. The drill output goes in `docs/archive/IMPLEMENTATION_PROGRESS.md` with its date. |
| Audit/results retention | **audit 1 year, results 90 days, both alert at 80% disk** | Retention that silently fills the system disk turns an observability feature into an outage. |

And these rules about the claim itself:

1. **`production-ready=yes` requires every one of**: implemented, deployed with
   evidence in this repository, canary-proven for capabilities that have a
   canary gate, a recorded restore drill, and no open release blocker touching
   that capability.
2. **Local tests never substitute for deployment or canary evidence.** This is
   already the ledger's rule; this gate makes it a release condition rather
   than a convention.
3. **An unmeasured target is not a target.** RPO/RTO may only be claimed once
   a drill has produced a number. Until then the ledger records `unknown`.

---

## 2. What this gate does *not* decide

- It does not authorize enabling any flag.
- It does not decide authorization enforcement — that is `DG-AUTHZ-ENFORCE`,
  deliberately separate because turning on 403s changes behavior for every
  caller, while agreeing on an RPO does not.
- It does not retire any existing blocker. `RB-LAUNCH-001` and `RB-NODE-001`
  close on their own evidence, not on an SLO decision.

---

## 3. Sub-decisions requiring an explicit ruling

| # | Question | Recommended | Consequence if rejected |
|---|---|---|---|
| O-1 | RPO 24h / RTO 4h | **Adopt as stated**, revisable once a drill produces real numbers. | Without a stated target, "is the backup good enough" has no answer and the question keeps being deferred. |
| O-2 | May a capability be `production-ready=yes` with a green local suite but no deployment evidence? | **No.** | This is the single most likely way for the ledger to start lying, and the ledger is the thing everything else defers to. |
| O-3 | Who may change a ledger row to `yes`? | **Only a work package that records its evidence in `docs/archive/IMPLEMENTATION_PROGRESS.md`.** | An unevidenced edit is indistinguishable from an aspiration. |
| O-4 | Retention deletion | **Requires a separate approved retention operation; never automatic.** | Automatic deletion of audit or results destroys the evidence an incident review needs. |
| O-5 | Single-process `role=all` in production | **Permitted for the first release, with the topology documented.** | Requiring a multi-process split first delays every other operational improvement behind an architecture change nothing currently needs. |

---

## 4. Acceptance after approval

- The ledger gains an explicit statement of these targets, and any row claiming
  `production-ready=yes` cites its deployment, canary and drill evidence.
- A backup older than the alert threshold surfaces in readiness or metrics.
- A recorded restore drill exists with a real duration.
- Retention policy is documented and no automatic deletion path exists.
- A test asserts that no ledger row claims `production-ready=yes` without the
  evidence fields this gate requires.

---

## 5. Decision

- [ ] **Approve recommended contract** — adopt §1's targets and rules and
  §3's recommendations (O-1…O-5).
- [ ] Approve with changes: ______________________________________________
- [ ] Reject — the project continues to describe itself as an internal MVP and
  no capability may claim production readiness.

Approving does not enable anything. It defines what would have to be true
before this system could honestly be called production-ready.
