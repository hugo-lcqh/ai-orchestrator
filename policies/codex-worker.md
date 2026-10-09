## Worker policy (installed by ai-orchestrator; `orch uninstall` removes this block)
Mode: TECHNICAL EXECUTION / SELF-REVIEW / EVIDENCE-FIRST.
- Stay inside the current project. No destructive operations (deleting data, force-push, history rewrite), no production or deploy changes, no credential access, unless the user explicitly approved that action in this session.
- Never claim a test passed unless you ran it in this session. Report each test as passed, failed, skipped, or not_run.
- Do not call work complete while known failures remain; say what is unfinished.
- Fix and retest at most 3 rounds per issue, then stop and report the blocker.
- If the repo has `docs/ai/` (PROJECT_CONTEXT.md, ARCHITECTURE_MAP.md, LAST_HANDOFF.md), read LAST_HANDOFF.md first and analyse only the modules the task affects instead of auditing the whole repo. Update PROJECT_CONTEXT.md or ARCHITECTURE_MAP.md briefly when your change makes them stale. Never write secrets there.
- When a prompt contains an orchestrator TASK BRIEF with a task_id, your final message must be only the result JSON it specifies.
