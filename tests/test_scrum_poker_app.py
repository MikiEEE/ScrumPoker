import os
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


import scrum_poker_app as demo


class FakeWriterTask:
    def __init__(self):
        self.done = False
        self.signals = []

    def acceptSignal(self, sig):
        self.signals.append(sig)


class FakeKernel:
    def __init__(self):
        self.closed = []
        self.now_ms = 1000

    def socket_close(self, sock):
        self.closed.append(sock)

    def scheduler_now_ms(self):
        return self.now_ms


class FakeTaskRegistry:
    def __init__(self, tasks=None):
        self._tasks = list(tasks or [])

    def list(self):
        return list(self._tasks)


class FakeRuntime:
    def __init__(self, tasks=None):
        self.kernel = FakeKernel()
        self.cancelled = []
        self.tasks = FakeTaskRegistry(tasks)

    def cancel_task(self, task, recursive=False):
        self.cancelled.append((task, recursive))
        return 0


class FakeSpawnTask:
    def __init__(self):
        self.spawned = []
        self.priority = 3

    def spawn(self, routine, priority=None, **kwargs):
        writer = FakeWriterTask()
        writer.priority = priority
        writer.routine = routine
        writer.args = kwargs.get("args")
        self.spawned.append(writer)
        return writer


class TestScrumPokerState(unittest.TestCase):
    def test_public_state_masks_other_votes_when_hidden(self):
        state = {
            "session_open": True,
            "votes_visible": False,
            "connections": {
                1: {
                    "client_id": 1,
                    "is_admin": True,
                    "name": "Alice",
                    "vote": "5",
                },
                2: {
                    "client_id": 2,
                    "is_admin": False,
                    "name": "Bob",
                    "vote": "8",
                },
            },
        }

        snapshot = demo._build_public_state(state, viewer_id=1)

        self.assertTrue(snapshot["participants"][0]["is_admin"])
        self.assertEqual("5", snapshot["participants"][0]["vote"])
        self.assertTrue(snapshot["participants"][1]["has_voted"])
        self.assertIsNone(snapshot["participants"][1]["vote"])
        self.assertTrue(snapshot["me"]["is_admin"])

    def test_public_state_includes_session_token_for_resume(self):
        state = {
            "session_open": True,
            "votes_visible": False,
            "connections": {
                1: {
                    "client_id": 1,
                    "connected": True,
                    "is_admin": False,
                    "name": "Alice",
                    "session_token": "resume-token-123",
                    "vote": "5",
                }
            },
        }

        snapshot = demo._build_public_state(state, viewer_id=1)

        self.assertEqual("resume-token-123", snapshot["me"]["session_token"])

    def test_apply_client_message_requires_join_before_vote(self):
        state = {
            "session_open": True,
            "votes_visible": False,
            "connections": {},
        }
        connection = {"name": None, "vote": None}

        error = demo._apply_client_message(state, connection, {"type": "vote", "value": "5"})

        self.assertEqual("join the session before voting", error)
        self.assertIsNone(connection["vote"])

    def test_clear_votes_hides_board_again(self):
        state = {
            "session_open": True,
            "votes_visible": True,
            "connections": {
                1: {"client_id": 1, "name": "Alice", "vote": "5"},
                2: {"client_id": 2, "name": "Bob", "vote": "8"},
            },
        }

        error = demo._apply_client_message(
            state,
            state["connections"][1],
            {"type": "clear_votes"},
        )

        self.assertIsNone(error)
        self.assertFalse(state["votes_visible"])
        self.assertIsNone(state["connections"][1]["vote"])
        self.assertIsNone(state["connections"][2]["vote"])

    def test_admin_auth_succeeds_when_passphrase_matches_env(self):
        writer = FakeWriterTask()
        connection = {
            "client_id": 1,
            "is_admin": False,
            "name": "Alice",
            "outbox": [],
            "vote": None,
            "writer_task": writer,
        }
        state = {
            "session_open": True,
            "votes_visible": False,
            "connections": {1: connection},
        }

        with patch.dict(os.environ, {"ADMIN_PASSPHRASE": "swordfish"}, clear=False):
            error = demo._apply_client_message(
                state,
                connection,
                {"type": "become_admin", "passphrase": "swordfish"},
            )

        self.assertIsNone(error)
        self.assertTrue(connection["is_admin"])
        self.assertEqual([demo.OUTBOX_SIGNAL], writer.signals)
        self.assertIn('"type":"notice"', connection["outbox"][0])

    def test_non_admin_cannot_change_session_join_state(self):
        state = {
            "session_open": True,
            "votes_visible": False,
            "connections": {},
        }
        connection = {"client_id": 1, "is_admin": False, "name": "Alice", "vote": None}

        error = demo._apply_client_message(
            state,
            connection,
            {"type": "set_session_open", "open": False},
        )

        self.assertEqual("admin privileges required", error)
        self.assertTrue(state["session_open"])

    def test_admin_can_kick_a_connected_user(self):
        kernel = FakeKernel()
        runtime = FakeRuntime()
        runtime.kernel = kernel
        admin_writer = FakeWriterTask()
        target_writer = FakeWriterTask()
        target_session_task = FakeWriterTask()
        target_socket = object()
        admin = {
            "client_id": 1,
            "is_admin": True,
            "name": "Alice",
            "outbox": [],
            "vote": None,
            "writer_task": admin_writer,
        }
        target = {
            "client_id": 2,
            "closed": False,
            "is_admin": False,
            "name": "Bob",
            "outbox": [],
            "shutdown_after_drain": False,
            "session_task": target_session_task,
            "socket": target_socket,
            "vote": "5",
            "writer_task": target_writer,
        }
        state = {
            "kernel": kernel,
            "os": runtime,
            "session_open": True,
            "votes_visible": False,
            "connections": {
                1: admin,
                2: target,
            },
        }

        error = demo._apply_client_message(
            state,
            admin,
            {"type": "kick_user", "client_id": 2},
        )

        self.assertIsNone(error)
        self.assertNotIn(2, state["connections"])
        self.assertTrue(target["closed"])
        self.assertEqual([target_socket], kernel.closed)
        self.assertEqual([demo.OUTBOX_SIGNAL], admin_writer.signals)
        self.assertIn((target_session_task, False), runtime.cancelled)
        self.assertIn((target_writer, False), runtime.cancelled)

    def test_home_page_comes_from_static_assets(self):
        response = demo._static_asset_response("/")

        self.assertIn(b"/static/app.css", response)
        self.assertIn(b"/static/app.js", response)

    def test_resolve_connection_for_socket_reuses_existing_browser_session(self):
        runtime = FakeRuntime()
        task = FakeSpawnTask()
        state = demo._new_state(runtime)
        existing = demo._make_connection_record(
            state,
            ("127.0.0.1", 5000),
            session_token="resume-token",
            tab_id="tab-1",
        )
        existing["name"] = "Alice"
        existing["is_admin"] = True
        existing["vote"] = "8"
        existing["connected"] = False
        existing["resume_deadline_ms"] = demo._now_ms(state) + 1000

        resumed, did_resume = demo._resolve_connection_for_socket(
            task,
            state,
            object(),
            ("127.0.0.1", 5001),
            session_token="resume-token",
            tab_id="tab-1",
        )

        self.assertTrue(did_resume)
        self.assertIs(existing, resumed)
        self.assertEqual("Alice", resumed["name"])
        self.assertTrue(resumed["is_admin"])
        self.assertEqual("8", resumed["vote"])
        self.assertTrue(resumed["connected"])

    def test_resolve_connection_for_socket_creates_new_record_for_other_tab(self):
        runtime = FakeRuntime()
        task = FakeSpawnTask()
        state = demo._new_state(runtime)
        existing = demo._make_connection_record(
            state,
            ("127.0.0.1", 5000),
            session_token="shared-token",
            tab_id="tab-a",
        )
        existing["name"] = "Alice"
        existing["connected"] = True
        existing["socket"] = object()
        existing["session_task"] = FakeWriterTask()
        existing["writer_task"] = FakeWriterTask()

        created, did_resume = demo._resolve_connection_for_socket(
            task,
            state,
            object(),
            ("127.0.0.1", 5001),
            session_token="shared-token",
            tab_id="tab-b",
        )

        self.assertFalse(did_resume)
        self.assertIsNot(existing, created)
        self.assertEqual("tab-b", created["tab_id"])
        self.assertNotEqual("shared-token", created["session_token"])
        self.assertEqual(2, len(state["connections"]))

    def test_resolve_connection_for_socket_cancels_previous_transport_on_resume(self):
        runtime = FakeRuntime()
        task = FakeSpawnTask()
        state = demo._new_state(runtime)
        old_socket = object()
        old_session_task = FakeWriterTask()
        old_writer_task = FakeWriterTask()
        existing = demo._make_connection_record(
            state,
            ("127.0.0.1", 5000),
            session_token="resume-token",
            tab_id="tab-1",
        )
        existing["connected"] = True
        existing["socket"] = old_socket
        existing["session_task"] = old_session_task
        existing["writer_task"] = old_writer_task

        resumed, did_resume = demo._resolve_connection_for_socket(
            task,
            state,
            object(),
            ("127.0.0.1", 5001),
            session_token="resume-token",
            tab_id="tab-1",
        )

        self.assertTrue(did_resume)
        self.assertIs(existing, resumed)
        self.assertIn((old_session_task, False), runtime.cancelled)
        self.assertIn((old_writer_task, False), runtime.cancelled)
        self.assertEqual([old_socket], runtime.kernel.closed)

    def test_load_dotenv_file_reads_admin_passphrase(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("ADMIN_PASSPHRASE='reload-me'\n")
            dotenv_path = handle.name

        try:
            with patch.dict(os.environ, {}, clear=True):
                loaded = demo._load_dotenv_file(dotenv_path)
                self.assertEqual("reload-me", loaded["ADMIN_PASSPHRASE"])
                self.assertEqual("reload-me", os.environ["ADMIN_PASSPHRASE"])
        finally:
            os.unlink(dotenv_path)

    def test_shutdown_runtime_closes_listener_and_client_sockets(self):
        task_a = object()
        task_b = object()
        runtime = FakeRuntime(tasks=[task_a, task_b])
        listener = object()
        client_socket = object()
        state = {
            "listener": listener,
            "connections": {
                1: {
                    "client_id": 1,
                    "connected": True,
                    "outbox": [],
                    "shutdown_after_drain": False,
                    "socket": client_socket,
                    "websocket": object(),
                }
            },
        }

        demo._shutdown_runtime(runtime, state)

        self.assertIsNone(state["listener"])
        self.assertFalse(state["connections"][1]["connected"])
        self.assertTrue(state["connections"][1]["shutdown_after_drain"])
        self.assertIsNone(state["connections"][1]["socket"])
        self.assertIsNone(state["connections"][1]["websocket"])
        self.assertEqual([listener, client_socket], runtime.kernel.closed)
        self.assertEqual([(task_a, False), (task_b, False)], runtime.cancelled)


class TestScrumPokerShell(unittest.TestCase):
    def test_poker_shell_command_lists_scrum_poker_commands(self):
        shell = demo.ScrumPokerShell({"session_open": True, "connections": {}, "votes_visible": False})

        response = shell.command_poker([])

        self.assertIn("Scrum poker commands:", response)
        self.assertIn("- poker (aliases: scrum): Show ScrumPokerShell-specific commands.", response)
        self.assertIn("- session (aliases: joins): Manage team joins.", response)

    def test_session_shell_command_toggles_join_gate_and_broadcasts(self):
        writer = FakeWriterTask()
        state = {
            "session_open": True,
            "votes_visible": False,
            "connections": {
                1: {
                    "client_id": 1,
                    "closed": False,
                    "name": "Alice",
                    "outbox": [],
                    "vote": None,
                    "writer_task": writer,
                }
            },
        }

        shell = demo.ScrumPokerShell(state, allow_python=False)
        response = shell.command_session(["close"])

        self.assertEqual("joining disabled", response)
        self.assertFalse(state["session_open"])
        self.assertEqual([demo.OUTBOX_SIGNAL], writer.signals)
        self.assertEqual(1, len(state["connections"][1]["outbox"]))

    def test_session_shell_status_reports_current_state(self):
        shell = demo.ScrumPokerShell({"session_open": False, "connections": {}, "votes_visible": False})

        self.assertEqual("joining is closed", shell.command_session(["status"]))


if __name__ == "__main__":
    unittest.main()
