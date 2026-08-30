# Type-check baseline

Status: active debt ledger  
Established: 2026-08-03

The first repository-wide mypy gate found 969 pre-existing errors in 31 of 74
production and script modules. Failing CI on that historical debt would make
the gate unusable, while globally disabling error classes would also hide
problems in clean and newly created modules.

`pyproject.toml` therefore names the exact legacy modules with
`ignore_errors = true`. All other modules are checked with `check_untyped_defs`,
and a new module is checked automatically. The baseline list must not grow to
accommodate new code.

## Retirement policy

Each architecture work package should remove entries as it changes the
affected boundary:

- settings decomposition removes `app.config`;
- API router extraction removes `app.main` and newly clean router modules are
  never added to the baseline;
- repository and Unit of Work extraction removes `app.db` and migrated
  persistence modules remain checked;
- protocol and worker separation removes the Node-related entries;
- focused cleanup removes the remaining service and script entries.

CI runs mypy over the complete `app`, `agent`, and `scripts` trees. The explicit
module list is the only legacy exemption; error-code-wide suppression and broad
path exclusions are not accepted as substitutes.
