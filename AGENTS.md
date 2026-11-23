# PlayLaunch Skill — Agent Entrypoint (`AGENTS.md`)

**Single Source of Truth:** Read and follow **[SKILL.md](SKILL.md)** and its Progressive Disclosure guides under `references/` before running any command.

## Hard Guardrails (Summary)
- **Never** pass `--approve`, `--promote-production`, `--acknowledge-compliance-risks`, `--sync-to-audio-workspace`, or invoke `stages/07_isolated_publish.py` without explicit user permission in the same message.
- Stop cleanly after Stage 6 + Stage 2B (`awaiting_human_approval`) and present the generated preview files directly in chat (`listing_copy.html`, `play_console_declarations_checklist.html`, and rendered screenshots under `workspace_runs/`).
- Always run scripts via `<SKILL>\.venv\Scripts\python.exe` (Python 3.12). Never modify `vendors/` or `Youssef_Audio_Workspace`.
