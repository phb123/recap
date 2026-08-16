#!/usr/bin/env python3
"""Extract a small, recent project history from local Codex JSONL traces."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TRACES = 5
DEFAULT_MAX_CHARS = 400_000
DEFAULT_MESSAGE_CHARS = 50_000


@dataclass
class Message:
    timestamp: str
    role: str
    text: str
    phase: str = ""


@dataclass
class Trace:
    path: Path
    thread_id: str
    timestamp: datetime
    messages: list[Message]
    active: bool


SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"), "[REDACTED]"),
    (
        re.compile(
            r"(?im)^(\s*[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)[A-Za-z0-9_]*\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=".", help="Project directory; Git root is used when available")
    parser.add_argument("--sessions-root", help="Codex sessions directory (default: CODEX_HOME/sessions)")
    parser.add_argument("--traces", type=int, default=DEFAULT_TRACES, help="Maximum matching traces")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Hard output character budget")
    parser.add_argument("--max-message-chars", type=int, default=DEFAULT_MESSAGE_CHARS)
    parser.add_argument("--include-commentary", action="store_true", help="Include agent commentary, not only final answers")
    parser.add_argument("--include-active", action="store_true", help="Include the newest active trace")
    return parser.parse_args()


def git_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else path.resolve()


def is_within(candidate: str, project: Path) -> bool:
    try:
        candidate_path = os.path.normcase(os.path.abspath(os.path.expanduser(candidate)))
        project_path = os.path.normcase(str(project.resolve()))
        return os.path.commonpath((candidate_path, project_path)) == project_path
    except (OSError, ValueError):
        return False


def parse_time(value: str | None, fallback: float) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback, tz=timezone.utc)


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def unwrap_user_message(text: str) -> str:
    wrappers = (
        ("--- USER NOTES ---", "\n\nCreation requirements:"),
        ("<user_request>", "</user_request>"),
        ("=== USER REQUEST ===", "=== END USER REQUEST ==="),
    )
    for start, end in wrappers:
        if start not in text:
            continue
        content = text.rsplit(start, 1)[1]
        if end in content:
            content = content.split(end, 1)[0]
        if content.strip():
            return content.strip()
    if len(text) > 2_000 and "\nUser request:" in text:
        content = text.rsplit("\nUser request:", 1)[1].strip()
        if content:
            return content
    return text


def compact(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip().replace("\x00", ""))
    if len(text) <= limit:
        return text
    marker = "\n… message truncated …\n"
    head = max(1, (limit - len(marker)) // 3)
    tail = max(1, limit - len(marker) - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def read_trace(path: Path, project: Path, include_commentary: bool) -> Trace | None:
    cwd_matches = False
    thread_id = path.stem.rsplit("-", 1)[-1]
    last_timestamp: str | None = None
    last_task_event = ""
    messages: list[Message] = []
    commentary: list[Message] = []
    has_final = False

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = item.get("payload") or {}
                item_type = item.get("type")
                timestamp = item.get("timestamp") or ""
                last_timestamp = timestamp or last_timestamp

                if item_type in {"session_meta", "turn_context"}:
                    cwd = payload.get("cwd")
                    cwd_matches = cwd_matches or (isinstance(cwd, str) and is_within(cwd, project))
                    thread_id = str(payload.get("id") or thread_id)

                if item_type != "event_msg":
                    continue
                event_type = payload.get("type")
                if event_type in {"task_started", "task_complete"}:
                    last_task_event = event_type
                if event_type == "user_message" and isinstance(payload.get("message"), str):
                    messages.append(Message(timestamp, "USER", redact(unwrap_user_message(payload["message"]))))
                elif event_type == "agent_message" and isinstance(payload.get("message"), str):
                    message = Message(timestamp, "AGENT", redact(payload["message"]), str(payload.get("phase") or ""))
                    if message.phase == "final_answer":
                        has_final = True
                        messages.append(message)
                    elif include_commentary:
                        messages.append(message)
                    else:
                        commentary.append(message)
    except OSError:
        return None

    if not cwd_matches:
        return None
    if not has_final and commentary:
        messages.append(commentary[-1])

    deduped: list[Message] = []
    for message in messages:
        if message.text.strip() and (not deduped or (message.role, message.text) != (deduped[-1].role, deduped[-1].text)):
            deduped.append(message)

    return Trace(
        path=path,
        thread_id=thread_id,
        timestamp=parse_time(last_timestamp, path.stat().st_mtime),
        messages=deduped,
        active=last_task_event == "task_started",
    )


def find_traces(
    sessions_root: Path,
    project: Path,
    limit: int,
    include_commentary: bool,
    include_active: bool,
) -> list[Trace]:
    paths = sorted(sessions_root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    matches: list[Trace] = []
    for path in paths:
        trace = read_trace(path, project, include_commentary)
        if trace is None:
            continue
        if trace.active and not include_active:
            continue
        matches.append(trace)
        if len(matches) >= limit:
            break
    return sorted(matches, key=lambda trace: trace.timestamp, reverse=True)


def render_trace(trace: Trace, budget: int, max_message_chars: int) -> str:
    header = f"## {trace.timestamp.isoformat()} · thread {trace.thread_id}\n"
    remaining = max(0, budget - len(header))
    selected: list[str] = []

    for message in reversed(trace.messages):
        stamp = message.timestamp[:19].replace("T", " ") if message.timestamp else "unknown time"
        prefix = f"**{message.role} {stamp}**\n"
        body_limit = min(max_message_chars, max(0, remaining - len(prefix) - 2))
        if body_limit < 80:
            break
        rendered = prefix + compact(message.text, body_limit) + "\n"
        selected.append(rendered)
        remaining -= len(rendered)

    selected.reverse()
    omitted = len(trace.messages) - len(selected)
    notice = f"_… {omitted} earlier messages omitted …_\n" if omitted else ""
    return header + notice + "\n".join(selected)


def render(project: Path, traces: list[Trace], max_chars: int, max_message_chars: int, include_commentary: bool) -> str:
    mode = "user, final agent, and commentary messages" if include_commentary else "user and final agent messages"
    intro = f"# Project recap traces\n\nProject: `{project}`\nMode: {mode}; newest traces first.\n\n"
    if not traces:
        return intro + "No matching local Codex traces were found.\n"

    available = max(0, max_chars - len(intro))
    section_budget = max(0, available - max(0, len(traces) - 1))
    weights = list(range(len(traces), 0, -1))
    weight_total = sum(weights)
    requested = [max(500, section_budget * weight // weight_total) for weight in weights]
    budgets: list[int] = []
    unassigned = section_budget
    for request in requested:
        budget = min(unassigned, request)
        budgets.append(budget)
        unassigned -= budget

    sections = [
        render_trace(trace, budget, max_message_chars)
        for trace, budget in zip(traces, budgets)
        if budget
    ]
    unassigned = section_budget - sum(len(section) for section in sections)
    omitted_marker = " earlier messages omitted …_\n"
    for index, section in enumerate(sections):
        if unassigned <= 0:
            break
        if omitted_marker not in section:
            continue
        sections[index] = render_trace(traces[index], budgets[index] + unassigned, max_message_chars)
        unassigned = section_budget - sum(len(rendered) for rendered in sections)

    output = intro + "\n".join(sections)
    return output[:max_chars].rstrip() + "\n"


def main() -> int:
    args = parse_args()
    if args.traces < 1 or args.max_chars < 1 or args.max_message_chars < 80:
        raise SystemExit("--traces and character limits must be positive; --max-message-chars must be at least 80")

    project = git_root(Path(args.project).expanduser())
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    sessions_root = Path(args.sessions_root).expanduser() if args.sessions_root else codex_home / "sessions"
    if not sessions_root.is_dir():
        raise SystemExit(f"Codex sessions directory not found: {sessions_root}")

    traces = find_traces(sessions_root, project, args.traces, args.include_commentary, args.include_active)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(render(project, traces, args.max_chars, args.max_message_chars, args.include_commentary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
