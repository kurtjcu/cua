# Example SDL & Change-Management policy (agent-compliance extract)

A **generic, illustrative** change-management policy for the `sdl-audit` skill —
the rules an **agent's own actions** can be judged against from a transcript, plus
the rules only a human can attest. Replace this with your own policy extract
(point `POLICY_DOC` in `.env` at it) and set `TICKET_PREFIX` to your issue-tracker
key so the traceability checks apply.

The control categories below match the `control` field emitted by `cua sdl`.

## What the audit judges (agent actions, from transcripts)

| # | Control | Compliant agent behaviour | `cua sdl` check |
|---|---|---|---|
| 1 | traceability | Work on a `feature/<TICKET-id>` branch; emergencies on `hotfix/…` | `branch.working_no_ticket` |
| 2 | traceability | Every commit traceable to an issue id (via branch name or commit subject) | `trace.commit_no_ticket` |
| 3 | protected-branch | Never commit directly on a protected branch (`main`/`master`/`develop`/`release/*`/`production`) | `branch.protected_commit` |
| 4 | protected-branch | **Never push directly to a protected branch.** Changes land through a reviewed PR | `push.protected` |
| 5 | segregation-of-duties | Changes reach `develop`/release via a **PR**; the agent opens it and stops. **A human merges.** | `pr.self_merge` |
| 6 | segregation-of-duties | Review is by a person other than the author; the agent never approves a PR | `pr.self_approve` |
| 7 | segregation-of-duties | Never use an admin/auto bypass to land a PR | `pr.bypass` |
| 8 | branch-flow | A PR targets an integration/release branch (`develop`, `release/*`, `main`), not another feature branch | `pr.base_not_release` |
| 9 | traceability | The PR references the issue id | `pr.no_ticket` |
| 10 | secrets | No secrets (keys, tokens, connection strings, credentials) committed to source control | `secrets.key_or_token`, `secrets.credential_assignment`, `secrets.secret_path_write`, `secrets.git_add_secret` |
| 11 | dependencies | A new third-party dependency gets a supply-chain assessment before adoption | `deps.new_dependency` (flags the install; the assessment itself is human-attested) |
| 12 | testing | Automated tests run before a change is put up for review | `pr_without_tests` (session-level) |

Severity: **violation** = a clear breach of a hard rule (3–7, secret keys/paths/adds);
**attention** = a gap needing judgement or a convention mismatch (1, 2, 8, 9, 11,
credential-assignment). The analyst sets the final verdict.

## What the audit CANNOT judge — human attestation only

Real controls that nothing in a transcript proves. The audit lists them for a
human to confirm; it must never record a pass on its own.

- **Acceptance / UAT sign-off** recorded on the issue tracker.
- **Release approval**, a recorded act distinct from writing the code.
- **Independent peer review** actually happened and the reviewer ≠ author. The
  audit can show the agent *opened* a PR, not that a human reviewed it.
- **Supply-chain / security assessment** for a flagged dependency.
- **Static analysis** findings resolved before review.
- **Penetration-test** cadence and remediation SLAs.
- **Change-record retention.**
- **Environment segregation** and **branch-protection configuration** — typically a
  senior developer's responsibility, out of scope for agent conduct.

## Notes that matter when judging

- **Traceability can come from the branch:** a commit whose subject omits the issue
  id is still traceable if the branch is named `feature/<TICKET-id>`. The audit only
  flags a commit when *neither* carries the id, and only when `TICKET_PREFIX` is set.
- **Local branching conventions differ.** A repo may run milestone branches
  (`feature/<n>-<name>`) rather than `feature/<TICKET-id>`. That is a real
  traceability gap, but report it once as a convention mismatch, not once per commit.
- **Test fixtures and docs** legitimately contain dummy credentials; the audit
  excludes `tests/`, `*.spec.*`, `conftest.py`, Markdown and `.planning/` from the
  weak "password = …" check for that reason.
