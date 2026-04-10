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


def build_app(app_id="root", base_path="/", runtime=None, label=None):
    runtime = runtime or FakeRuntime()
    return demo.ScrumPokerApp(
        app_id=app_id,
        base_path=base_path,
        runtime=runtime,
        title="Sprint Poker",
        label=label,
    )


class TestScrumPokerApp(unittest.TestCase):
    def test_public_state_masks_other_votes_when_hidden(self):
        app = build_app()
        app.state["session_open"] = True
        app.state["votes_visible"] = False
        app.state["connections"] = {
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
        }

        snapshot = demo._build_public_state(app.state, viewer_id=1)

        self.assertTrue(snapshot["participants"][0]["is_admin"])
        self.assertEqual("5", snapshot["participants"][0]["vote"])
        self.assertTrue(snapshot["participants"][1]["has_voted"])
        self.assertIsNone(snapshot["participants"][1]["vote"])
        self.assertTrue(snapshot["me"]["is_admin"])

    def test_apps_keep_state_isolated(self):
        root_app = build_app("root", "/")
        legalease_app = build_app("legalease", "/legalease", runtime=root_app.runtime, label="Legalease")
        root_app.state["session_open"] = True
        legalease_app.state["session_open"] = True
        root_connection = {"client_id": 1, "name": None, "vote": None, "is_admin": False}
        legalease_connection = {"client_id": 1, "name": None, "vote": None, "is_admin": False}
        root_app.state["connections"][1] = root_connection
        legalease_app.state["connections"][1] = legalease_connection

        self.assertIsNone(demo._apply_client_message(root_app.state, root_connection, {"type": "join", "name": "Alice"}))
        self.assertIsNone(demo._apply_client_message(root_app.state, root_connection, {"type": "vote", "value": "8"}))
        self.assertIsNone(
            demo._apply_client_message(legalease_app.state, legalease_connection, {"type": "join", "name": "Legal Team"})
        )

        self.assertEqual("Alice", root_connection["name"])
        self.assertEqual("8", root_connection["vote"])
        self.assertEqual("Legal Team", legalease_connection["name"])
        self.assertIsNone(legalease_connection["vote"])
        self.assertEqual(1, len(root_app.state["connections"]))
        self.assertEqual(1, len(legalease_app.state["connections"]))

    def test_legalease_index_response_uses_prefixed_routes_and_bootstrap(self):
        app = build_app("legalease", "/legalease", label="Legalease")

        response = app.index_response()

        self.assertIn(b"/legalease/static/app.css", response)
        self.assertIn(b"/legalease/static/app.js", response)
        self.assertIn(b'data-ws-path="/legalease/ws"', response)
        self.assertIn(b'data-healthz-path="/legalease/healthz"', response)
        self.assertIn(b'data-storage-key-prefix="smallos-scrum-poker-legalease"', response)
        self.assertIn(b"Legalease", response)

    def test_root_index_response_keeps_root_routes(self):
        app = build_app("root", "/")

        response = app.index_response()

        self.assertIn(b"/static/app.css", response)
        self.assertIn(b"/static/app.js", response)
        self.assertIn(b'data-ws-path="/ws"', response)
        self.assertIn(b'data-healthz-path="/healthz"', response)
        self.assertIn(b'data-storage-key-prefix="smallos-scrum-poker-root"', response)

    def test_route_list_includes_static_assets_for_each_mount(self):
        app = build_app("legalease", "/legalease", label="Legalease")

        routes = app.route_list()

        self.assertIn("/legalease/static/app.css", routes)
        self.assertIn("/legalease/static/app.js", routes)
        self.assertIn("/legalease/ws", routes)
        self.assertIn("/legalease/api/state", routes)

    def test_app_supervisor_task_is_a_watcher(self):
        app = build_app("root", "/")

        task = app.to_task()

        self.assertTrue(task.isWatcher)

    def test_host_resolves_longest_prefix_for_legalease_routes(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        host = demo.ScrumPokerHost([root_app, legalease_app])

        self.assertIs(legalease_app, host.resolve_app("/legalease"))
        self.assertIs(legalease_app, host.resolve_app("/legalease/ws"))
        self.assertIs(root_app, host.resolve_app("/"))
        self.assertIs(root_app, host.resolve_app("/unknown"))

    def test_api_state_token_is_isolated_per_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        root_connection = demo._make_connection_record(root_app.state, ("127.0.0.1", 5000), session_token="shared")
        root_connection["name"] = "Alice"

        allowed = root_app.build_http_response("GET", "/api/state?session_token=shared", "/api/state")
        forbidden = legalease_app.build_http_response(
            "GET",
            "/legalease/api/state?session_token=shared",
            "/legalease/api/state",
        )

        self.assertIn(b"200 OK", allowed)
        self.assertIn(b"403 Forbidden", forbidden)

    def test_resolve_connection_for_socket_reuses_existing_browser_session_per_app(self):
        runtime = FakeRuntime()
        task = FakeSpawnTask()
        app = build_app(runtime=runtime)
        existing = demo._make_connection_record(
            app.state,
            ("127.0.0.1", 5000),
            session_token="resume-token",
            tab_id="tab-1",
        )
        existing["name"] = "Alice"
        existing["is_admin"] = True
        existing["vote"] = "8"
        existing["connected"] = False
        existing["resume_deadline_ms"] = demo._now_ms(app.state) + 1000

        resumed, did_resume = demo._resolve_connection_for_socket(
            task,
            app.state,
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
        app = build_app(runtime=runtime)
        existing = demo._make_connection_record(
            app.state,
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
            app.state,
            object(),
            ("127.0.0.1", 5001),
            session_token="shared-token",
            tab_id="tab-b",
        )

        self.assertFalse(did_resume)
        self.assertIsNot(existing, created)
        self.assertEqual("tab-b", created["tab_id"])
        self.assertNotEqual("shared-token", created["session_token"])
        self.assertEqual(2, len(app.state["connections"]))

    def test_admin_can_kick_a_connected_user(self):
        kernel = FakeKernel()
        runtime = FakeRuntime()
        runtime.kernel = kernel
        app = build_app(runtime=runtime)
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
        app.state["kernel"] = kernel
        app.state["os"] = runtime
        app.state["session_open"] = True
        app.state["votes_visible"] = False
        app.state["connections"] = {1: admin, 2: target}

        error = demo._apply_client_message(
            app.state,
            admin,
            {"type": "kick_user", "client_id": 2},
        )

        self.assertIsNone(error)
        self.assertNotIn(2, app.state["connections"])
        self.assertTrue(target["closed"])
        self.assertEqual([target_socket], kernel.closed)
        self.assertEqual([demo.OUTBOX_SIGNAL], admin_writer.signals)
        self.assertIn((target_session_task, False), runtime.cancelled)
        self.assertIn((target_writer, False), runtime.cancelled)

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

    def test_shutdown_runtime_closes_host_and_all_app_sockets(self):
        task_a = object()
        task_b = object()
        runtime = FakeRuntime(tasks=[task_a, task_b])
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        host = demo.ScrumPokerHost([root_app, legalease_app])
        listener = object()
        root_socket = object()
        legalease_socket = object()
        host.listener = listener
        root_app.state["connections"] = {
            1: {
                "client_id": 1,
                "connected": True,
                "outbox": [],
                "shutdown_after_drain": False,
                "socket": root_socket,
                "websocket": object(),
            }
        }
        legalease_app.state["connections"] = {
            2: {
                "client_id": 2,
                "connected": True,
                "outbox": [],
                "shutdown_after_drain": False,
                "socket": legalease_socket,
                "websocket": object(),
            }
        }

        demo._shutdown_runtime(runtime, host, [root_app, legalease_app])

        self.assertIsNone(host.listener)
        self.assertIsNone(root_app.state["connections"][1]["socket"])
        self.assertIsNone(legalease_app.state["connections"][2]["socket"])
        self.assertEqual([listener, root_socket, legalease_socket], runtime.kernel.closed)
        self.assertEqual([(task_a, False), (task_b, False)], runtime.cancelled)

    def test_host_reads_host_and_port_from_current_environment(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")

        with patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "9099"}, clear=False):
            host = demo.ScrumPokerHost([root_app, legalease_app])

        self.assertEqual("127.0.0.1", host.host)
        self.assertEqual(9099, host.port)


class TestScrumPokerShell(unittest.TestCase):
    def test_poker_shell_lists_mounted_apps(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        shell = demo.ScrumPokerShell([root_app, legalease_app], allow_python=False)

        response = shell.command_poker(["apps"])

        self.assertIn("Mounted scrum poker apps:", response)
        self.assertIn("- root: /", response)
        self.assertIn("- legalease: /legalease (Legalease)", response)

    def test_namespaced_session_command_targets_only_selected_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        root_writer = FakeWriterTask()
        legalease_writer = FakeWriterTask()
        root_app.state["session_open"] = True
        root_app.state["connections"] = {
            1: {
                "client_id": 1,
                "closed": False,
                "name": "Alice",
                "outbox": [],
                "vote": None,
                "writer_task": root_writer,
            }
        }
        legalease_app.state["session_open"] = True
        legalease_app.state["connections"] = {
            2: {
                "client_id": 2,
                "closed": False,
                "name": "Bob",
                "outbox": [],
                "vote": None,
                "writer_task": legalease_writer,
            }
        }
        shell = demo.ScrumPokerShell([root_app, legalease_app], allow_python=False)

        response = shell.command_poker(["root", "session", "close"])

        self.assertEqual("root joining disabled", response)
        self.assertFalse(root_app.state["session_open"])
        self.assertTrue(legalease_app.state["session_open"])
        self.assertEqual([demo.OUTBOX_SIGNAL], root_writer.signals)
        self.assertEqual([], legalease_writer.signals)

    def test_namespaced_idle_status_reports_targeted_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        shell = demo.ScrumPokerShell([root_app], allow_python=False)

        response = shell.command_poker(["root", "idle", "status"])

        self.assertIn("root idle for", response)


if __name__ == "__main__":
    unittest.main()
