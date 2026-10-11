#!/usr/bin/env python3
"""Extract recent project history from the local session stores of several coding agents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator


DEFAULT_TRACES = 6
DEFAULT_INDEX = 25
DEFAULT_MAX_CHARS = 150_000
DEFAULT_MESSAGE_CHARS = 12_000
OPENING_CHARS = 1_500

AGENTS = ("claude", "codex", "cursor", "factory", "gemini", "opencode", "pi")
AGENT_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "factory": "Factory Droid",
    "gemini": "Gemini CLI",
    "opencode": "OpenCode",
    "pi": "Pi",
}


@dataclass
class Message:
    timestamp: str
    role: str
    text: str
    phase: str = ""


@dataclass
class Trace:
    agent: str
    thread_id: str
    timestamp: datetime
    messages: list[Message]
    title: str = ""
    active: bool = False
    elsewhere: str = ""


@dataclass
class Candidate:
    agent: str
    cwd: str
    mtime: float
    load: Callable[[], Trace | None] = field(repr=False)


SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}"
            r"|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|4/0A[A-Za-z0-9_-]{20,})"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"(?im)^(\s*[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)[A-Za-z0-9_]*\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# Codex appends memory citations and app directives such as ::git-create-branch{...} to answers.
CITATION = re.compile(r"\s*<oai-mem-citation>.*?</oai-mem-citation>\s*|^::[a-z-]+\{.*\}[ \t]*$", re.S | re.M)
# Agents prepend context blocks such as <environment_context> or <recommended_plugins> to user turns.
LEADING_CONTEXT_TAG = re.compile(r"\s*<(?!user_query>|user_request>)([a-z][a-z_]*)>.*?</\1>\s*", re.S)
NOISE_PREFIXES = ("<local-command", "<task-notification", "# AGENTS.md instructions", "Caveat: The messages below")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=".", help="Project directory; Git root is used when available")
    parser.add_argument("--home", help="Home directory holding agent data (default: the user's home)")
    parser.add_argument("--agents", default=",".join(AGENTS), help=f"Comma-separated sources (default: {','.join(AGENTS)})")
    parser.add_argument("--traces", type=int, default=DEFAULT_TRACES, help="Sessions rendered with message content")
    parser.add_argument("--index", type=int, default=DEFAULT_INDEX, help="Sessions listed in the one-line index")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Hard output character budget")
    parser.add_argument("--max-message-chars", type=int, default=DEFAULT_MESSAGE_CHARS)
    parser.add_argument("--include-commentary", action="store_true", help="Include intermediate agent messages, not only each turn's final answer")
    parser.add_argument("--include-active", action="store_true", help="Include the session that is currently running")
    parser.add_argument("--no-clones", action="store_true", help="Only match sessions inside the project directory, not other checkouts of the same origin")
    return parser.parse_args()


# ---------------------------------------------------------------- helpers


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


def git_dir(path: Path) -> Path | None:
    """The shared .git directory for a checkout or linked worktree, found without running git."""
    for directory in (path, *path.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return marker
        if marker.is_file():
            content = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not content.startswith("gitdir:"):
                return None
            linked = (directory / content.split(":", 1)[1].strip()).resolve()
            common = linked / "commondir"
            return (linked / common.read_text(encoding="utf-8").strip()).resolve() if common.is_file() else linked
    return None


def origin_url(path: Path) -> str:
    """Normalized origin remote, e.g. github.com/owner/repo, or "" when there is none."""
    try:
        directory = git_dir(path)
        config = (directory / "config").read_text(encoding="utf-8", errors="replace") if directory else ""
    except OSError:
        return ""
    section = re.search(r'^\[remote "origin"\](.*?)(?=^\[|\Z)', config, re.M | re.S)
    url = re.search(r"^\s*url\s*=\s*(\S+)", section.group(1), re.M) if section else None
    if not url:
        return ""
    value = re.sub(r"^[a-z+]+://", "", url.group(1).strip().lower())  # scheme
    value = re.sub(r"^[^@/]+@", "", value)  # user@
    value = re.sub(r"^([^/:]+):(?!\d)", r"\1/", value)  # scp-style host:owner/repo
    return re.sub(r"(\.git)?/*$", "", value)


class ProjectMatcher:
    """Matches session directories inside the project, or inside another checkout with the same origin remote."""

    def __init__(self, project: Path, follow_clones: bool) -> None:
        self.project = project
        self.remote = origin_url(project) if follow_clones else ""
        self.cache: dict[str, bool] = {}

    def __call__(self, cwd: str) -> bool:
        if not cwd:
            return False
        if is_within(cwd, self.project):
            return True
        if not self.remote:
            return False
        key = os.path.normcase(os.path.abspath(os.path.expanduser(cwd)))
        if key not in self.cache:
            self.cache[key] = origin_url(Path(key)) == self.remote
        return self.cache[key]


def slug(path: Path | str, collapse: bool = False) -> str:
    """Directory names agents derive from a project path, e.g. C:\\a_b -> C--a-b."""
    return re.sub(r"[^A-Za-z0-9]+" if collapse else r"[^A-Za-z0-9]", "-", str(path)).strip("-" if collapse else "")


def parse_time(value: object, fallback: float = 0.0) -> datetime:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback, tz=timezone.utc)


def iso(value: object) -> str:
    return parse_time(value).isoformat() if isinstance(value, (int, float)) else str(value or "")


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") in {"text", "input_text", "output_text"}:
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n\n".join(part for part in parts if part.strip())
    return ""


def has_block(content: object, *types: str) -> bool:
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") in types for block in content)


def unwrap_user_message(text: str) -> str:
    wrappers = (
        ("--- USER NOTES ---", "\n\nCreation requirements:"),
        ("<user_request>", "</user_request>"),
        ("<user_query>", "</user_query>"),
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


def clean_user(text: str) -> str:
    text = SYSTEM_REMINDER.sub("", text).strip()
    while (injected := LEADING_CONTEXT_TAG.match(text)) is not None:
        text = text[injected.end():]
    command = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if command:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return f"{command.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    if text.startswith(NOISE_PREFIXES):
        return ""
    return unwrap_user_message(text).strip()


def compact(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip().replace("\x00", ""))
    if len(text) <= limit:
        return text
    marker = "\n… message truncated …\n"
    head = max(1, (limit - len(marker)) // 3)
    tail = max(1, limit - len(marker) - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def age(then: datetime, now: datetime) -> str:
    seconds = max(0, (now - then).total_seconds())
    for unit, size in (("month", 30 * 86400), ("week", 7 * 86400), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size * (2 if unit in {"month", "week"} else 1):
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def finish(agent: str, thread_id: str, raw: list[Message], fallback: float, include_commentary: bool, title: str = "", active: bool = False) -> Trace | None:
    """Keep user messages plus each turn's final agent answer, then dedupe and redact."""
    turns: list[Message] = []
    pending: list[Message] = []

    def flush() -> None:
        if not pending:
            return
        finals = [message for message in pending if message.phase == "final_answer"]
        turns.extend(pending if include_commentary else finals or pending[-1:])
        pending.clear()

    for message in raw:
        message.text = (CITATION.sub("", message.text) if message.role == "AGENT" else message.text).strip()
        if not message.text:
            continue
        if message.role == "USER":
            flush()
            turns.append(message)
        else:
            pending.append(message)
    flush()

    deduped: list[Message] = []
    for message in turns:
        if not deduped or (message.role, message.text) != (deduped[-1].role, deduped[-1].text):
            message.text = redact(message.text)
            deduped.append(message)
    if not any(message.role == "USER" for message in deduped):
        return None
    last = next((message.timestamp for message in reversed(deduped) if message.timestamp), "")
    if not title:
        title = next((message.text for message in deduped if message.role == "USER"), "")
    return Trace(agent, thread_id, parse_time(last, fallback), deduped, redact(title), active)


def read_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def first_cwd(path: Path, lines: int = 200) -> str:
    for number, item in enumerate(read_jsonl(path)):
        if number >= lines:
            break
        for holder in (item, item.get("payload") or {}):
            if isinstance(holder, dict) and isinstance(holder.get("cwd"), str):
                return holder["cwd"]
    return ""


# ---------------------------------------------------------------- sources


def codex_candidates(home: Path, match: "ProjectMatcher", opts: argparse.Namespace) -> Iterable[Candidate]:
    codex_home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") and not opts.home else home / ".codex"
    current = os.environ.get("CODEX_THREAD_ID", "")
    for root in (codex_home / "sessions", codex_home / "archived_sessions"):
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if match(cwd := first_cwd(path)):
                yield Candidate("codex", cwd, mtime(path), lambda path=path: read_codex(path, opts.include_commentary, current))


def read_codex(path: Path, include_commentary: bool, current: str) -> Trace | None:
    thread_id = path.stem.rsplit("-", 1)[-1]
    last_task_event = ""
    raw: list[Message] = []
    # Some Codex builds only record response_item messages, without user_message/agent_message events.
    response_raw: list[Message] = []
    for item in read_jsonl(path):
        payload = item.get("payload") or {}
        timestamp = item.get("timestamp") or ""
        if item.get("type") == "session_meta":
            thread_id = str(payload.get("id") or thread_id)
            if isinstance(payload.get("source"), dict) and "subagent" in payload["source"]:
                return None
        if item.get("type") == "response_item" and payload.get("type") == "message":
            text = content_text(payload.get("content"))
            if payload.get("role") == "user":
                response_raw.append(Message(timestamp, "USER", clean_user(text)))
            elif payload.get("role") == "assistant":
                response_raw.append(Message(timestamp, "AGENT", text, str(payload.get("phase") or "")))
        if item.get("type") != "event_msg":
            continue
        event_type = payload.get("type")
        if event_type in {"task_started", "task_complete"}:
            last_task_event = event_type
        if event_type == "user_message" and isinstance(payload.get("message"), str):
            raw.append(Message(timestamp, "USER", clean_user(payload["message"])))
        elif event_type == "agent_message" and isinstance(payload.get("message"), str):
            raw.append(Message(timestamp, "AGENT", payload["message"], str(payload.get("phase") or "")))
    active = thread_id == current if current else last_task_event == "task_started"
    if not any(message.role == "USER" for message in raw):
        raw = response_raw
    return finish("codex", thread_id, raw, mtime(path), include_commentary, active=active)


def claude_candidates(home: Path, match: "ProjectMatcher", opts: argparse.Namespace) -> Iterable[Candidate]:
    config = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") and not opts.home else home / ".claude"
    root = config / "projects"
    if not root.is_dir():
        return
    current = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for path in root.glob("*/*.jsonl"):
        if match(cwd := first_cwd(path)):
            yield Candidate("claude", cwd, mtime(path), lambda path=path: read_claude(path, opts.include_commentary, current))


def read_claude(path: Path, include_commentary: bool, current: str) -> Trace | None:
    raw: list[Message] = []
    title = ""
    for item in read_jsonl(path):
        if item.get("type") == "summary" and isinstance(item.get("summary"), str):
            title = item["summary"]
        if item.get("type") == "custom-title" and isinstance(item.get("customTitle"), str):
            title = item["customTitle"]
        if item.get("type") not in {"user", "assistant"} or item.get("isSidechain") or item.get("isMeta") or item.get("isCompactSummary"):
            continue
        content = (item.get("message") or {}).get("content")
        timestamp = item.get("timestamp") or ""
        if item["type"] == "user":
            if not has_block(content, "tool_result"):
                raw.append(Message(timestamp, "USER", clean_user(content_text(content))))
        else:
            raw.append(Message(timestamp, "AGENT", content_text(content)))
    return finish("claude", path.stem, raw, mtime(path), include_commentary, title, path.stem == current)


def cursor_candidates(home: Path, match: "ProjectMatcher", opts: argparse.Namespace) -> Iterable[Candidate]:
    root = home / ".cursor" / "projects"
    if not root.is_dir():
        return
    target = slug(match.project, collapse=True).lower()
    for directory in root.iterdir():
        if directory.name.lower() != target:
            continue
        for path in (directory / "agent-transcripts").rglob("*.jsonl"):
            yield Candidate("cursor", "", mtime(path), lambda path=path: read_cursor(path, opts.include_commentary))


def read_cursor(path: Path, include_commentary: bool) -> Trace | None:
    # Cursor transcripts carry no timestamps; the file time stands in for the whole session.
    stamp = datetime.fromtimestamp(mtime(path), tz=timezone.utc).isoformat()
    raw = []
    for item in read_jsonl(path):
        role = item.get("role") or (item.get("message") or {}).get("role")
        text = content_text((item.get("message") or {}).get("content"))
        if role == "user":
            raw.append(Message(stamp, "USER", clean_user(text)))
        elif role == "assistant":
            raw.append(Message(stamp, "AGENT", text))
    return finish("cursor", path.stem, raw, mtime(path), include_commentary)


def role_jsonl_candidates(agent: str, root: Path, match: "ProjectMatcher", opts: argparse.Namespace) -> Iterable[Candidate]:
    """Factory and Pi: one directory per project, JSONL with a cwd header and role/content messages."""
    if not root.is_dir():
        return
    for path in root.glob("*/*.jsonl"):
        if match(cwd := first_cwd(path, 5)):
            yield Candidate(agent, cwd, mtime(path), lambda path=path: read_role_jsonl(agent, path, opts.include_commentary))


def read_role_jsonl(agent: str, path: Path, include_commentary: bool) -> Trace | None:
    raw: list[Message] = []
    title = ""
    thread_id = path.stem
    for item in read_jsonl(path):
        if item.get("type") in {"session_start", "session"}:
            thread_id = str(item.get("id") or thread_id)
            title = item.get("title") if isinstance(item.get("title"), str) and item.get("title") != "New Session" else title
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        timestamp = iso(item.get("timestamp") or message.get("timestamp"))
        if message.get("role") == "user" and not has_block(content, "tool_result"):
            raw.append(Message(timestamp, "USER", clean_user(content_text(content))))
        elif message.get("role") == "assistant":
            raw.append(Message(timestamp, "AGENT", content_text(content)))
    return finish(agent, thread_id, raw, mtime(path), include_commentary, title)


def gemini_candidates(home: Path, match: "ProjectMatcher", opts: argparse.Namespace) -> Iterable[Candidate]:
    root = home / ".gemini" / "tmp"
    if not root.is_dir():
        return
    raw_path = str(match.project)
    hashes = {hashlib.sha256(variant.encode()).hexdigest() for variant in {raw_path, raw_path[:1].lower() + raw_path[1:], raw_path[:1].upper() + raw_path[1:]}}
    for directory in root.iterdir():
        marker = directory / ".project_root"
        try:
            matches = directory.name in hashes or (marker.is_file() and match(marker.read_text(encoding="utf-8").strip()))
        except OSError:
            matches = False
        if not matches:
            continue
        for path in (directory / "chats").glob("*.json"):
            yield Candidate("gemini", "", mtime(path), lambda path=path: read_gemini(path, opts.include_commentary))


def read_gemini(path: Path, include_commentary: bool) -> Trace | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = []
    for message in data.get("messages") or []:
        text = content_text(message.get("content"))
        if message.get("type") == "user":
            raw.append(Message(iso(message.get("timestamp")), "USER", clean_user(text)))
        elif message.get("type") in {"gemini", "model", "assistant"}:
            raw.append(Message(iso(message.get("timestamp")), "AGENT", text))
    return finish("gemini", str(data.get("sessionId") or path.stem), raw, mtime(path), include_commentary)


def opencode_candidates(home: Path, match: "ProjectMatcher", opts: argparse.Namespace) -> Iterable[Candidate]:
    data_home = Path(os.environ["XDG_DATA_HOME"]) if os.environ.get("XDG_DATA_HOME") and not opts.home else home / ".local" / "share"
    database = data_home / "opencode" / "opencode.db"
    if not database.is_file():
        return
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        rows = connection.execute("SELECT id, directory, title, time_updated FROM session WHERE parent_id IS NULL").fetchall()
    except sqlite3.Error:
        return
    for session_id, directory, title, updated in rows:
        if match(directory):
            yield Candidate(
                "opencode",
                directory,
                parse_time(updated).timestamp(),
                lambda session_id=session_id, title=title: read_opencode(connection, session_id, title, opts.include_commentary),
            )


def read_opencode(connection: sqlite3.Connection, session_id: str, title: str, include_commentary: bool) -> Trace | None:
    raw: list[Message] = []
    try:
        messages = connection.execute("SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created, id", (session_id,)).fetchall()
        for message_id, created, data in messages:
            role = json.loads(data).get("role")
            parts = connection.execute("SELECT data FROM part WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()
            texts = []
            for (part,) in parts:
                part = json.loads(part)
                if part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored"):
                    texts.append(part.get("text") or "")
            text = "\n\n".join(texts)
            if role == "user":
                raw.append(Message(iso(created), "USER", clean_user(text)))
            elif role == "assistant":
                raw.append(Message(iso(created), "AGENT", text))
    except (sqlite3.Error, json.JSONDecodeError):
        return None
    title = "" if not title or title.startswith("New session - ") else title
    return finish("opencode", session_id, raw, 0, include_commentary, title)


def find_candidates(home: Path, match: "ProjectMatcher", opts: argparse.Namespace, agents: list[str]) -> list[Candidate]:
    sources = {
        "claude": lambda: claude_candidates(home, match, opts),
        "codex": lambda: codex_candidates(home, match, opts),
        "cursor": lambda: cursor_candidates(home, match, opts),
        "factory": lambda: role_jsonl_candidates("factory", home / ".factory" / "sessions", match, opts),
        "gemini": lambda: gemini_candidates(home, match, opts),
        "opencode": lambda: opencode_candidates(home, match, opts),
        "pi": lambda: role_jsonl_candidates("pi", home / ".pi" / "agent" / "sessions", match, opts),
    }
    candidates: list[Candidate] = []
    for agent in agents:
        try:
            candidates.extend(sources[agent]())
        except OSError:
            continue
    return sorted(candidates, key=lambda candidate: candidate.mtime, reverse=True)


def load_traces(candidates: list[Candidate], project: Path, limit: int, include_active: bool) -> tuple[list[Trace], int]:
    """Load the newest usable sessions; also return how many sessions exist in total (unread ones counted as-is)."""
    traces: list[Trace] = []
    consumed = 0
    for candidate in candidates:
        if len(traces) >= limit:
            break
        consumed += 1
        trace = candidate.load()
        if trace is None or (trace.active and not include_active):
            continue
        if candidate.cwd and not is_within(candidate.cwd, project):
            trace.elsewhere = candidate.cwd
        traces.append(trace)
    traces.sort(key=lambda trace: trace.timestamp, reverse=True)
    return traces, len(traces) + len(candidates) - consumed


# ---------------------------------------------------------------- rendering


def trace_header(number: int, trace: Trace, now: datetime) -> str:
    stamp = trace.timestamp.strftime("%Y-%m-%d %H:%M UTC")
    where = f" · other checkout `{trace.elsewhere}`" if trace.elsewhere else ""
    return f"## Session {number} · {AGENT_NAMES[trace.agent]} · {stamp} ({age(trace.timestamp, now)}) · {trace.thread_id}{where}\n"


def render_trace(number: int, trace: Trace, budget: int, max_message_chars: int, now: datetime) -> str:
    header = trace_header(number, trace, now)
    remaining = max(0, budget - len(header))
    messages = trace.messages

    opening = ""
    first_kept = 0
    if len(messages) > 1 and messages[0].role == "USER" and remaining > 1_000:
        opening = "_Opening request:_ " + compact(messages[0].text, min(OPENING_CHARS, max_message_chars, remaining // 4)) + "\n\n"
        remaining -= len(opening)
        first_kept = 1

    selected: list[str] = []
    for message in reversed(messages[first_kept:]):
        stamp = message.timestamp[:16].replace("T", " ") if message.timestamp else "unknown time"
        prefix = f"**{message.role} {stamp}**\n"
        body_limit = min(max_message_chars, max(0, remaining - len(prefix) - 2))
        if body_limit < 80:
            break
        rendered = prefix + compact(message.text, body_limit) + "\n"
        selected.append(rendered)
        remaining -= len(rendered)

    selected.reverse()
    omitted = len(messages) - first_kept - len(selected)
    notice = f"_… {omitted} earlier messages omitted …_\n" if omitted else ""
    return header + opening + notice + "\n".join(selected)


def render_index(traces: list[Trace], detailed: int, total: int, now: datetime) -> str:
    lines = ["## Session index (newest first)\n"]
    for number, trace in enumerate(traces, 1):
        stamp = trace.timestamp.strftime("%Y-%m-%d")
        scope = "full below" if number <= detailed else "index only"
        prompts = sum(message.role == "USER" for message in trace.messages)
        title = one_line(trace.title, 140) or "(no prompt)"
        where = " · other checkout" if trace.elsewhere else ""
        lines.append(f"{number}. {stamp} ({age(trace.timestamp, now)}) · {AGENT_NAMES[trace.agent]}{where} · {prompts} prompt{'s' if prompts != 1 else ''} · {scope} — {title}")
    if total > len(traces):
        lines.append(f"\n_{total - len(traces)} older sessions not listed._")
    return "\n".join(lines) + "\n\n"


def render(
    project: Path,
    traces: list[Trace],
    detailed: int,
    total: int,
    agents: list[str],
    max_chars: int,
    max_message_chars: int,
    include_commentary: bool,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    mode = "user, final agent, and commentary messages" if include_commentary else "user messages and each turn's final agent answer"
    counts: dict[str, int] = {}
    for trace in traces:
        counts[trace.agent] = counts.get(trace.agent, 0) + 1
    found = ", ".join(f"{AGENT_NAMES[agent]} {count}" for agent, count in counts.items()) or "none"
    intro = (
        "# Project catch-up evidence\n\n"
        f"Project: `{project}`\n"
        f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Sources scanned: {', '.join(AGENT_NAMES[agent] for agent in agents)}\n"
        f"Sessions listed: {found}\n"
        f"Mode: {mode}; newest first. Newer sessions get more space; on conflicts the newer statement wins.\n\n"
    )
    if not traces:
        return intro + "No matching local agent sessions were found.\n"

    intro += render_index(traces, detailed, total, now)
    full = traces[:detailed]
    available = max(0, max_chars - len(intro))
    section_budget = max(0, available - max(0, len(full) - 1))
    # Harmonic weights: the newest session gets the largest share, and each older one progressively less.
    weights = [1 / (rank + 1) for rank in range(len(full))]
    weight_total = sum(weights)
    budgets: list[int] = []
    unassigned = section_budget
    for weight in weights:
        budget = min(unassigned, max(500, int(section_budget * weight / weight_total)))
        budgets.append(budget)
        unassigned -= budget

    sections = [render_trace(number, trace, budget, max_message_chars, now) for number, (trace, budget) in enumerate(zip(full, budgets), 1) if budget]
    unassigned = section_budget - sum(len(section) for section in sections)
    omitted_marker = " earlier messages omitted …_\n"
    for index, section in enumerate(sections):
        if unassigned <= 0:
            break
        if omitted_marker not in section:
            continue
        sections[index] = render_trace(index + 1, full[index], budgets[index] + unassigned, max_message_chars, now)
        unassigned = section_budget - sum(len(rendered) for rendered in sections)

    output = intro + "\n".join(sections)
    return output[:max_chars].rstrip() + "\n"


def main() -> int:
    args = parse_args()
    if args.traces < 1 or args.index < 0 or args.max_chars < 1 or args.max_message_chars < 80:
        raise SystemExit("--traces and character limits must be positive; --max-message-chars must be at least 80")
    agents = [agent.strip().lower() for agent in args.agents.split(",") if agent.strip()]
    unknown = sorted(set(agents) - set(AGENTS))
    if unknown:
        raise SystemExit(f"Unknown agents: {', '.join(unknown)}. Choose from: {', '.join(AGENTS)}")

    project = git_root(Path(args.project).expanduser())
    home = Path(args.home).expanduser() if args.home else Path.home()
    candidates = find_candidates(home, ProjectMatcher(project, not args.no_clones), args, agents)
    traces, total = load_traces(candidates, project, max(args.traces, args.index), args.include_active)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(render(project, traces, args.traces, total, agents, args.max_chars, args.max_message_chars, args.include_commentary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
