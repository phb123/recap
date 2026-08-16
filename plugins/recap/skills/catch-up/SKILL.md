---
name: catch-up
description: Produce a thorough project handover from recent local Codex session traces. Use when the user asks to catch up, resume prior work, review what agents already implemented, recover project direction and decisions, or understand the current technical state and likely next steps without loading reasoning, tool calls, commands, or full transcripts.
---

# Recap

Build a complete, decision-oriented handover from recent local Codex conversations. Use the extracted messages as evidence; never return them as a transcript dump.

## Workflow

1. Resolve the current Git repository root. If the directory is not a Git repository, use the current working directory.
2. Run the bundled extractor by absolute path:

   ```powershell
   python <skill-directory>\scripts\catch_up.py --project <project-root>
   ```

   Defaults: five most recent matching traces, newest first, a 400,000-character ceiling (roughly 100,000 tokens), user messages plus final agent messages. The currently active trace is skipped. Reasoning, developer/system messages, tool calls, commands, and tool output are never read into the result.
3. Check the current repository with `git status --short` and `git log -5 --oneline`. Use this only to verify the present state; treat trace messages as historical claims.
4. Synthesize one thorough handover for the next collaborator:
   - **Overview and direction:** the project's purpose, what it is becoming, and the important product or technical principles;
   - **Implemented:** capability-level outcomes, how the pieces work together, and relevant architecture, interfaces, data flow, files, commands, configuration, or deployment details;
   - **Decisions and constraints:** important rationale, rejected directions, compatibility requirements, and anything that must be preserved;
   - **Current state:** what works now, verified repository state, meaningful uncommitted or unfinished work, and current runtime or deployment state when known;
   - **Heading next:** unresolved requests, known issues, blockers, uncertainties, and the most likely next moves;
   - **Operating notes:** how to run, verify, release, or continue the work when the traces provide reliable instructions.
5. Prefer completeness over brevity while grouping related history instead of replaying each trace. Include technical details, exact paths, commands, APIs, versions, commits, and validation evidence when they help the next collaborator act or locate state. Omit routine noise and Git directives; do not turn the handover into a changelog or diagnostic dump.
6. Distinguish verified repository state from historical claims. State when no matching traces exist or when history may be incomplete. Do not invent continuity.

## Context budget

- The 400,000-character ceiling limits extracted trace evidence, not the final handover. Make the handover as detailed as needed for continuity.
- Each message is capped at 50,000 characters. Newer traces receive larger weighted shares, and unused space goes to the newest traces that still have omitted material.
- Keep the default limits unless the user requests deeper history.
- If important context is missing, retry once with `--traces 10` while keeping the same ceiling.
- Add `--include-commentary` only when final answers are insufficient or a trace ended mid-task.
- Never open whole JSONL traces after the extractor succeeds.
- Do not save extracted conversation text into the project.

The extractor redacts common credential forms, but still avoid repeating suspected secrets in the brief.
