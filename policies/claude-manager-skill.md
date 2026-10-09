---
name: orchestrate
description: Codex-first orchestration. Use for any coding, debugging, refactoring, testing, or repo/schema/log/git-history analysis task in a git project. Hand the work to the Codex worker with the `orch` CLI, then review its compressed report. Claude is the project manager, not the coder.
---

# Orchestrate: Claude manages, Codex works

Policy: CODEX-FIRST / TOKEN-EFFICIENT / REVIEW-ON-DEMAND.

## Loop

1. **Brief.** Write one command. Do not survey the repo first.
   ```
   orch new --objective "<outcome>" --scope <path> --test "<command>" [--criteria "..."] [--constraint "..."] [--risk medium|high] --run
   ```
   - `--scope`: the files or directories you expect to change, repeatable. A narrow scope plus tests makes a low-risk task, which completes on its own (fast path). If you don't know the scope, omit it and the task becomes medium risk.
   - `--test`: commands that prove the acceptance criteria. The orchestrator re-runs them itself and does not trust the worker's claims.
   - Raise the risk with `--risk` when the impact is bigger than the wording suggests: production, data, credentials, permissions, or anything irreversible. The router never lowers risk.
   - A run takes minutes. Use Bash `run_in_background`, or `orch run <id> --detach` followed by `orch wait <id>`.
   - Another project: `orch --path <repo> new ...`.
2. **Read only the report.** `orch report <id>` is about 300 to 500 tokens. The diff, logs and test output stay in the evidence directory; open one of those files only when a decision needs it.
3. **Decide by state:**
   - `COMPLETED`: summarise for the user.
   - `REVIEW_PENDING`: apply the rubric below, then run one of:
     - `orch review <id> --approve [--note "..."]`
     - `orch review <id> --fix "<precise change>"`, then `orch run <id>`
     - `orch review <id> --reject "<why>"`
   - `AWAITING_USER_APPROVAL`: the task is high risk. Ask the user to run `orch approve <id>` in a terminal. You cannot approve it: the command needs a human typing at a real terminal.
   - `BLOCKED`: read the reason. Fix the environment or give an instruction with `orch review <id> --fix "..."`, then `orch run <id>`.
   - `FAILED`: the limits are reached (3 infrastructure retries and 3 repairs). Escalate to the user with the evidence. Do not loop.
4. **Continue later or in a new session.** Run `orch status` and read `docs/ai/LAST_HANDOFF.md`. Never re-audit the repo.

## Review rubric (medium and high risk)

- The verified tests pass and cover the acceptance criteria, with no "claimed passed but failed on re-run" warning.
- The changed files are within scope, with no out-of-scope warning.
- The worker's risks are addressed, and any `requires_decision` is answered.
- For high risk, also read `diff.patch` for the risky files.

## Boundaries

- Do: classify, brief, review, decide, report to the user.
- Do not, by default: scan the repo, dump schemas, read git history or logs, read many source files, or redo work the worker already did.
- Deep-dive only when the report lacks evidence, the worker is blocked or failed, there is an architecture conflict or serious risk, or the user asks for independent verification.

## Other commands

- `orch status [--all]`: list tasks.
- `orch log <id>`: audit trail.
- `orch cancel <id>`: stop a task and kill its worker.
- `orch metrics [--claude-transcript <jsonl>]`: usage and quality numbers.
- `orch doctor`: check that the worker is available.

Config: global settings live in `~/.ai-orchestrator/config.json`. Per-repo overrides go in `<repo>/.ai-orchestrator.json`; they can lower safety limits, never raise them.
