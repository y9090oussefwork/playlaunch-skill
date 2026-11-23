# PlayLaunch Skill — Gemini & Antigravity Entrypoint (`GEMINI.md`)

**Single Source of Truth:** Read and follow **[SKILL.md](SKILL.md)** (and only the specific `references/*.md` needed for the user's task).

- **Hard Safety Lock:** Never pass `--approve`, `--promote-production`, `--acknowledge-compliance-risks`, `--sync-to-audio-workspace`, or execute Stage 7 automatically without explicit user permission in the same message.
- Pause cleanly at `awaiting_human_approval` after Stage 6 + Stage 2B and share clickable links to `play_console_declarations_checklist.html`, `listing_copy.html`, and generated store visuals directly in chat.
- Always invoke `<SKILL>\.venv\Scripts\python.exe` and keep `vendors/` & `Youssef_Audio_Workspace` untouched.
