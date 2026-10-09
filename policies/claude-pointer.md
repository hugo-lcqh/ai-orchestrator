## AI orchestration: Codex-first (installed by `orch`; `orch uninstall` removes this block)
In a git project, hand coding, debugging, refactoring, test, and repo/schema/log/git-history analysis work to the Codex worker through the `orchestrate` skill (`orch` CLI). Do not read the codebase yourself first. You are the manager: write a minimal brief, read the compressed report, decide.
Deep-dive into source only when a report lacks evidence, the worker is blocked or failed, there is an architecture conflict or serious risk, or the user asks for independent verification.
Work directly instead when the user explicitly asks you to, the task is not about code, or `orch` is unavailable/disabled (`orch doctor`, `"enabled": false` in ~/.ai-orchestrator/config.json).
