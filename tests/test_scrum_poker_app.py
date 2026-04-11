import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


import app as app_entry
import scrum_poker_core as core
from scrum_poker_app import ScrumPokerApp
from scrum_poker_host import ScrumPokerHost
from scrum_poker_shell import ScrumPokerShell


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
    return ScrumPokerApp(
        app_id=app_id,
        base_path=base_path,
        runtime=runtime,
        title="Sprint Poker",
        label=label,
    )


def add_connection(
    app,
    name=None,
    is_admin=False,
    vote=None,
    connected=True,
    session_token=None,
    tab_id=None,
):
    connection = core._make_connection_record(
        app.state,
        ("127.0.0.1", 5000 + len(app.state["connections"])),
        session_token=session_token,
        tab_id=tab_id,
    )
    connection["name"] = name
    connection["is_admin"] = is_admin
    connection["vote"] = vote
    connection["connected"] = connected
    connection["writer_task"] = FakeWriterTask()
    connection["session_task"] = FakeWriterTask()
    connection["socket"] = object() if connected else None
    return connection


def response_json(response):
    _, body = response.split(b"\r\n\r\n", 1)
    return json.loads(body.decode("utf-8"))


class TestScrumPokerBoardState(unittest.TestCase):
    def test_public_state_masks_other_votes_when_hidden(self):
        app = build_app()
        app.state["session_open"] = True
        app.state["votes_visible"] = False
        alice = add_connection(app, name="Alice", is_admin=True, vote="5")
        bob = add_connection(app, name="Bob", vote="8")

        snapshot = core._build_public_state(app.state, viewer_id=alice["client_id"])
        participants = {participant["name"]: participant for participant in snapshot["participants"]}

        self.assertTrue(participants["Alice"]["is_admin"])
        self.assertTrue(participants["Alice"]["has_voted"])
        self.assertIsNone(participants["Alice"]["vote"])
        self.assertTrue(participants["Bob"]["has_voted"])
        self.assertIsNone(participants["Bob"]["vote"])
        self.assertNotIn("is_self", participants["Alice"])
        self.assertTrue(snapshot["me"]["is_admin"])
        self.assertEqual("5", snapshot["me"]["vote"])

    def test_apps_keep_state_isolated(self):
        root_app = build_app("root", "/")
        legalease_app = build_app("legalease", "/legalease", runtime=root_app.runtime, label="Legalease")
        root_app.state["session_open"] = True
        legalease_app.state["session_open"] = True
        root_connection = add_connection(root_app)
        legalease_connection = add_connection(legalease_app)

        self.assertIsNone(core._apply_client_message(root_app.state, root_connection, {"type": "join", "name": "Alice"}))
        self.assertIsNone(core._apply_client_message(root_app.state, root_connection, {"type": "vote", "value": "8"}))
        self.assertIsNone(
            core._apply_client_message(legalease_app.state, legalease_connection, {"type": "join", "name": "Legal Team"})
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

    def test_api_state_token_is_isolated_per_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        root_connection = add_connection(root_app, name="Alice", session_token="shared")

        allowed = root_app.build_http_response("GET", "/api/state?session_token=shared", "/api/state")
        forbidden = legalease_app.build_http_response(
            "GET",
            "/legalease/api/state?session_token=shared",
            "/legalease/api/state",
        )

        self.assertIn(b"200 OK", allowed)
        self.assertEqual("Alice", response_json(allowed)["me"]["name"])
        self.assertIn(b"403 Forbidden", forbidden)
        self.assertEqual("shared", root_connection["session_token"])

    def test_build_apps_uses_declarative_board_configs(self):
        runtime = FakeRuntime()
        board_configs = [
            {"app_id": "root", "base_path": "/", "title": "Sprint Poker"},
            {"app_id": "pricing", "base_path": "/pricing", "title": "Sprint Poker", "label": "Pricing"},
        ]

        apps = app_entry.build_apps(runtime, board_configs=board_configs)

        self.assertEqual(["root", "pricing"], [app.app_id for app in apps])
        self.assertEqual(["/", "/pricing"], [app.base_path for app in apps])
        self.assertEqual("Pricing", apps[1].label)

    def test_stats_snapshot_counts_queue_pressure(self):
        app = build_app()
        alice = add_connection(app, name="Alice", vote="5")
        bob = add_connection(app, name="Bob")
        core._ensure_connection_queue_fields(alice)
        core._ensure_connection_queue_fields(bob)
        alice["pending_shared_state_text"] = "state"
        alice["pending_viewer_state_text"] = "viewer"
        alice["pending_messages"].append("notice")
        alice["dropped_state_updates"] = 2
        app.state["stats"]["queue_disconnects"] = 1

        snapshot = app.stats_snapshot()

        self.assertEqual("root", snapshot["app_id"])
        self.assertEqual(2, snapshot["joined_users"])
        self.assertEqual(2, snapshot["connected_transports"])
        self.assertEqual(2, snapshot["pending_state_messages"])
        self.assertEqual(1, snapshot["pending_messages"])
        self.assertEqual(2, snapshot["dropped_state_updates"])
        self.assertEqual(1, snapshot["queue_disconnects"])


class TestScrumPokerPerformanceHelpers(unittest.TestCase):
    def test_shared_state_cache_reused_until_dirty(self):
        app = build_app()
        add_connection(app, name="Alice", vote="5")
        add_connection(app, name="Bob")

        first = core._build_shared_state(app.state)
        second = core._build_shared_state(app.state)

        self.assertIs(first, second)
        self.assertEqual(1, app.state["stats"]["shared_state_rebuilds"])

        core._mark_state_dirty(app.state)
        third = core._build_shared_state(app.state)

        self.assertIsNot(first, third)
        self.assertEqual(2, app.state["stats"]["shared_state_rebuilds"])

    def test_broadcast_state_coalesces_pending_state_messages(self):
        app = build_app()
        alice = add_connection(app, name="Alice")
        core._mark_connection_viewer_dirty(alice)

        core._broadcast_state(app.state)
        first_shared = alice["pending_shared_state_text"]
        first_viewer = alice["pending_viewer_state_text"]

        alice["vote"] = "8"
        core._mark_connection_viewer_dirty(alice)
        core._mark_state_dirty(app.state)
        core._broadcast_state(app.state)

        self.assertIsNotNone(alice["pending_shared_state_text"])
        self.assertIsNotNone(alice["pending_viewer_state_text"])
        self.assertNotEqual(first_shared, alice["pending_shared_state_text"])
        self.assertNotEqual(first_viewer, alice["pending_viewer_state_text"])
        self.assertGreaterEqual(alice["dropped_state_updates"], 1)
        self.assertGreaterEqual(app.state["stats"]["dropped_state_updates"], 1)
        self.assertEqual([core.OUTBOX_SIGNAL, core.OUTBOX_SIGNAL], alice["writer_task"].signals)

    def test_non_state_queue_overflow_disconnects_slow_client(self):
        app = build_app()
        target = add_connection(app, name="Alice")
        target_socket = target["socket"]
        target_session_task = target["session_task"]
        target_writer_task = target["writer_task"]

        for index in range(core.MAX_PENDING_AUX_MESSAGES):
            core._queue_notice(target, "notice {}".format(index))

        self.assertEqual(core.MAX_PENDING_AUX_MESSAGES, len(target["pending_messages"]))
        self.assertIn(target["client_id"], app.state["connections"])

        core._queue_error(target, "too much backlog")

        self.assertNotIn(target["client_id"], app.state["connections"])
        self.assertEqual(1, app.state["stats"]["queue_disconnects"])
        self.assertIn(target_socket, app.runtime.kernel.closed)
        self.assertIn((target_session_task, False), app.runtime.cancelled)
        self.assertIn((target_writer_task, False), app.runtime.cancelled)

    def test_multi_room_broadcast_only_updates_target_room(self):
        runtime = FakeRuntime()
        apps = [
            build_app(
                app_id="room{}".format(index),
                base_path="/" if index == 0 else "/room{}".format(index),
                runtime=runtime,
                label="" if index == 0 else "Room {}".format(index),
            )
            for index in range(20)
        ]

        for app in apps:
            for user_index in range(10):
                add_connection(app, name="User {} {}".format(app.app_id, user_index))

        apps[0].broadcast_state()

        targeted = list(apps[0].state["connections"].values())
        untouched = [connection for app in apps[1:] for connection in app.state["connections"].values()]

        self.assertTrue(all(connection["pending_shared_state_text"] is not None for connection in targeted))
        self.assertTrue(all(connection["writer_task"].signals for connection in targeted))
        self.assertTrue(all(connection["pending_shared_state_text"] is None for connection in untouched))
        self.assertTrue(all(not connection["writer_task"].signals for connection in untouched))


class TestScrumPokerHostAndShell(unittest.TestCase):
    def test_host_resolves_longest_prefix_for_legalease_routes(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        host = ScrumPokerHost([root_app, legalease_app])

        self.assertIs(legalease_app, host.resolve_app("/legalease"))
        self.assertIs(legalease_app, host.resolve_app("/legalease/ws"))
        self.assertIs(root_app, host.resolve_app("/"))
        self.assertIs(root_app, host.resolve_app("/unknown"))

    def test_host_stats_snapshot_isolated_per_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        root_user = add_connection(root_app, name="Alice", vote="5")
        legal_user = add_connection(legalease_app, name="Bob")
        core._ensure_connection_queue_fields(root_user)
        core._ensure_connection_queue_fields(legal_user)
        root_user["pending_shared_state_text"] = "state"
        root_user["pending_messages"].append("notice")
        root_user["dropped_state_updates"] = 3
        legalease_app.state["stats"]["queue_disconnects"] = 2
        host = ScrumPokerHost([root_app, legalease_app])

        snapshot = host.stats_snapshot()
        app_stats = {item["app_id"]: item for item in snapshot["apps"]}

        self.assertEqual(1, app_stats["root"]["joined_users"])
        self.assertEqual(1, app_stats["root"]["pending_state_messages"])
        self.assertEqual(1, app_stats["root"]["pending_messages"])
        self.assertEqual(3, app_stats["root"]["dropped_state_updates"])
        self.assertEqual(2, app_stats["legalease"]["queue_disconnects"])
        self.assertEqual(2, snapshot["totals"]["joined_users"])
        self.assertEqual(3, snapshot["totals"]["dropped_state_updates"])
        self.assertEqual(2, snapshot["totals"]["queue_disconnects"])

    def test_poker_shell_lists_mounted_apps(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        shell = ScrumPokerShell([root_app, legalease_app], allow_python=False)

        response = shell.command_poker(["apps"])

        self.assertIn("Mounted scrum poker apps:", response)
        self.assertIn("- root: /", response)
        self.assertIn("- legalease: /legalease (Legalease)", response)

    def test_poker_stats_reports_all_apps(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        add_connection(root_app, name="Alice")
        add_connection(legalease_app, name="Bob")
        host = ScrumPokerHost([root_app, legalease_app])
        shell = ScrumPokerShell([root_app, legalease_app], host=host, allow_python=False)

        response = shell.command_poker(["stats"])

        self.assertIn("Scrum poker stats:", response)
        self.assertIn("- root (/): joined=1", response)
        self.assertIn("- legalease (/legalease): joined=1", response)
        self.assertIn("Totals: joined=2 connected=2", response)

    def test_namespaced_session_command_targets_only_selected_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")
        root_connection = add_connection(root_app, name="Alice")
        legalease_connection = add_connection(legalease_app, name="Bob")
        root_app.state["session_open"] = True
        legalease_app.state["session_open"] = True
        shell = ScrumPokerShell([root_app, legalease_app], allow_python=False)

        response = shell.command_poker(["root", "session", "close"])

        self.assertEqual("root joining disabled", response)
        self.assertFalse(root_app.state["session_open"])
        self.assertTrue(legalease_app.state["session_open"])
        self.assertEqual([core.OUTBOX_SIGNAL], root_connection["writer_task"].signals)
        self.assertEqual([], legalease_connection["writer_task"].signals)

    def test_namespaced_idle_status_reports_targeted_app(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        shell = ScrumPokerShell([root_app], allow_python=False)

        response = shell.command_poker(["root", "idle", "status"])

        self.assertIn("root idle for", response)


class TestScrumPokerConnectionLifecycle(unittest.TestCase):
    def test_resolve_connection_for_socket_reuses_existing_browser_session_per_app(self):
        runtime = FakeRuntime()
        task = FakeSpawnTask()
        app = build_app(runtime=runtime)
        existing = core._make_connection_record(
            app.state,
            ("127.0.0.1", 5000),
            session_token="resume-token",
            tab_id="tab-1",
        )
        existing["name"] = "Alice"
        existing["is_admin"] = True
        existing["vote"] = "8"
        existing["connected"] = False
        existing["resume_deadline_ms"] = core._now_ms(app.state) + 1000

        resumed, did_resume = core._resolve_connection_for_socket(
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
        existing = core._make_connection_record(
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

        created, did_resume = core._resolve_connection_for_socket(
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
        runtime = FakeRuntime()
        app = build_app(runtime=runtime)
        admin = add_connection(app, name="Alice", is_admin=True)
        target = add_connection(app, name="Bob", vote="5")
        target_socket = target["socket"]
        target_session_task = target["session_task"]
        target_writer_task = target["writer_task"]
        app.state["session_open"] = True
        app.state["votes_visible"] = False

        error = core._apply_client_message(
            app.state,
            admin,
            {"type": "kick_user", "client_id": target["client_id"]},
        )

        self.assertIsNone(error)
        self.assertNotIn(target["client_id"], app.state["connections"])
        self.assertTrue(target["closed"])
        self.assertIn(target_socket, runtime.kernel.closed)
        self.assertEqual([core.OUTBOX_SIGNAL], admin["writer_task"].signals)
        self.assertIn((target_session_task, False), runtime.cancelled)
        self.assertIn((target_writer_task, False), runtime.cancelled)

    def test_load_dotenv_file_reads_admin_passphrase(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("ADMIN_PASSPHRASE='reload-me'\n")
            dotenv_path = handle.name

        try:
            with patch.dict(os.environ, {}, clear=True):
                loaded = core._load_dotenv_file(dotenv_path)
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
        host = ScrumPokerHost([root_app, legalease_app])
        listener = object()
        root_connection = add_connection(root_app, name="Alice")
        legalease_connection = add_connection(legalease_app, name="Bob")
        root_socket = root_connection["socket"]
        legalease_socket = legalease_connection["socket"]
        host.listener = listener

        core._shutdown_runtime(runtime, host, [root_app, legalease_app])

        self.assertIsNone(host.listener)
        self.assertIsNone(root_connection["socket"])
        self.assertIsNone(legalease_connection["socket"])
        self.assertEqual([listener, root_socket, legalease_socket], runtime.kernel.closed)
        self.assertEqual([(task_a, False), (task_b, False)], runtime.cancelled)

    def test_host_reads_host_and_port_from_current_environment(self):
        runtime = FakeRuntime()
        root_app = build_app("root", "/", runtime=runtime)
        legalease_app = build_app("legalease", "/legalease", runtime=runtime, label="Legalease")

        with patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "9099"}, clear=False):
            host = ScrumPokerHost([root_app, legalease_app])

        self.assertEqual("127.0.0.1", host.host)
        self.assertEqual(9099, host.port)


if __name__ == "__main__":
    unittest.main()
