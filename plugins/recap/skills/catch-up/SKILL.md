---
name: catch-up
description: Produce a one-page catch-up summary of a project from the local chat sessions of every coding agent used on it (Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Factory Droid, Pi), with recent work weighted most heavily. Use when the user asks to catch up, resume prior work, see what agents already did, or recover a project's direction, current state, and next steps without loading full transcripts.
---

# Recap

Write a one-page summary of what happened in this project across all coding agents. Newer sessions matter most. Use the extracted messages as evidence; never return them as a transcript dump.

## Workflow

1. Resolve the current Git repository root. If the directory is not a Git repository, use the current working directory.
2. Run the bundled extractor by absolute path:

   ```
   python <skill-directory>/scripts/catch_up.py --project <project-root>
   ```

   It scans the local session stores of Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Factory Droid, and Pi, keeps sessions whose working directory is the project or a subdirectory, and prints:
   - a **session index**: up to 25 sessions, newest first, one line each with date, age, agent, and opening prompt;
   - **full sections** for the 6 newest sessions: the opening request plus the latest user prompts and each turn's final agent answer. Newer sessions get larger shares of the 150,000-character budget.

   The session running this skill is skipped, and so are subagent threads. Reasoning, tool calls, commands, and tool output are never extracted.
3. Check the repository with `git status --short` and `git log -8 --oneline`. Use this to verify the present state; treat session messages as historical claims.
4. Write the summary in the shape below.

## Output: one page

Keep it to roughly 300–500 words. Write about the project, not the sessions; group by topic rather than replaying each chat.

```
# <project> catch-up · <today's date>

**In short:** 2–3 sentences: what this project is and where it stands right now.

## Latest work
Most of the detail goes here: the newest sessions, what was asked, what was done, and what the result was. Name files, commands, and commits when they help someone continue. Mark the agent and age, e.g. "(Claude Code, 2 days ago)".

## Earlier context
A few short bullets on older work that still matters: decisions, architecture, constraints, things that were tried and rejected. Skip anything later work superseded.

## Open threads
Unfinished requests, known bugs, blockers, and the most likely next steps.

<sub>Based on N sessions (agent counts) from <oldest date> to <newest date>, plus current git state.</sub>
```

Weigh by recency:
- The newest sessions get the most words. Older sessions shrink to a line or disappear unless they set something that still holds.
- On conflicts, the newer statement wins. Mention the older one only if the reversal matters.
- When the repository contradicts a session claim (e.g. "pushed" but the working tree shows uncommitted changes), trust the repository and say so.

Leave out routine noise: greetings, test prompts, app restarts, Git chores, and failed setup attempts that later succeeded. If no sessions matched, say so and summarize from git state alone. Do not invent continuity between sessions.

## Options

- `--traces N`: sessions shown in full (default 6). Use 10–12 only if the user asks for deeper history or the latest sessions are clearly mid-story.
- `--index N`: sessions listed in the index (default 25).
- `--agents claude,codex,...`: limit the sources.
- `--include-commentary`: add intermediate agent messages when a session ended mid-task without a final answer.
- `--include-active`: include the session that is currently running.

Never open whole session files after the extractor succeeds, and do not save extracted conversation text into the project. The extractor redacts common credential forms, but still avoid repeating anything that looks like a secret.
