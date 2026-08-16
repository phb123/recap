import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "catch_up.py"


class CatchUpTests(unittest.TestCase):
    def write_trace(self, root: Path, name: str, cwd: Path, events: list[dict]) -> None:
        path = root / "2026" / "08" / "09" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"timestamp": "2026-08-09T10:00:00Z", "type": "session_meta", "payload": {"id": name, "cwd": str(cwd)}}]
        rows.extend(events)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def run_script(self, project: Path, sessions: Path, *extra: str) -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(project), "--sessions-root", str(sessions), "--traces", "5", *extra],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout

    def test_extracts_recent_messages_without_internal_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            sessions = base / "sessions"
            project.mkdir()

            self.write_trace(
                sessions,
                "older.jsonl",
                project,
                [
                    {"timestamp": "2026-08-09T10:01:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-08-09T10:02:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Boilerplate\n--- USER NOTES ---\nBuild the first version\n\nCreation requirements:\nMore boilerplate"}},
                    {"timestamp": "2026-08-09T10:03:00Z", "type": "response_item", "payload": {"type": "reasoning", "summary": "private reasoning"}},
                    {"timestamp": "2026-08-09T10:04:00Z", "type": "event_msg", "payload": {"type": "agent_message", "phase": "commentary", "message": "running a command"}},
                    {"timestamp": "2026-08-09T10:05:00Z", "type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer", "message": "Done with sk-abcdefghijklmnop"}},
                    {"timestamp": "2026-08-09T10:06:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            self.write_trace(
                sessions,
                "newer.jsonl",
                project / "src",
                [
                    {"timestamp": "2026-08-09T11:01:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-08-09T11:02:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Fix the newest issue"}},
                    {"timestamp": "2026-08-09T11:03:00Z", "type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer", "message": "Newest issue fixed → verified"}},
                    {"timestamp": "2026-08-09T11:04:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            self.write_trace(
                sessions,
                "active.jsonl",
                project,
                [
                    {"timestamp": "2026-08-09T12:01:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-08-09T12:02:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Catch me up"}},
                ],
            )
            self.write_trace(
                sessions,
                "another-active.jsonl",
                project / "src",
                [
                    {"timestamp": "2026-08-09T11:30:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-08-09T11:31:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Unfinished background task"}},
                ],
            )

            output = self.run_script(project, sessions)
            self.assertIn("Fix the newest issue", output)
            self.assertIn("→ verified", output)
            self.assertIn("Build the first version", output)
            self.assertLess(output.index("Fix the newest issue"), output.index("Build the first version"))
            self.assertNotIn("Catch me up", output)
            self.assertNotIn("Unfinished background task", output)
            self.assertNotIn("Boilerplate", output)
            self.assertNotIn("private reasoning", output)
            self.assertNotIn("running a command", output)
            self.assertNotIn("sk-abcdefghijklmnop", output)
            self.assertIn("[REDACTED]", output)
            self.assertLessEqual(len(output), 400_000)

            commentary = self.run_script(project, sessions, "--include-commentary")
            self.assertIn("running a command", commentary)

    def test_unused_recent_trace_budget_rolls_forward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            sessions = base / "sessions"
            project.mkdir()

            older_events = [
                {"timestamp": "2026-08-09T10:01:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
            ]
            older_events.extend(
                {
                    "timestamp": f"2026-08-09T10:{index + 2:02d}:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": f"old detail {index} " + "x" * 90},
                }
                for index in range(10)
            )
            older_events.append(
                {"timestamp": "2026-08-09T10:12:00Z", "type": "event_msg", "payload": {"type": "task_complete"}}
            )
            self.write_trace(sessions, "older.jsonl", project, older_events)
            self.write_trace(
                sessions,
                "newer.jsonl",
                project,
                [
                    {"timestamp": "2026-08-09T11:01:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-08-09T11:02:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "short recent task"}},
                    {"timestamp": "2026-08-09T11:03:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )

            output = self.run_script(project, sessions, "--max-chars", "2000", "--max-message-chars", "200")
            self.assertIn("short recent task", output)
            self.assertIn("old detail 0", output)
            self.assertGreater(len(output), 1500)
            self.assertLessEqual(len(output), 2000)


if __name__ == "__main__":
    unittest.main()
