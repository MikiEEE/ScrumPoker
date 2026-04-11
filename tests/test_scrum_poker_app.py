import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


import app as demo


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


def response_body(response):
    return response.split(b"\r\n\r\n", 1)[1]


def response_json(response):
    return json.loads(response_body(response).decode("utf-8"))


def build_host(runtime=None):
    runtime = runtime or FakeRuntime()
    legalease_room = demo.build_premium_room(runtime)
    host = demo.ScrumPokerHost([legalease_room], host="127.0.0.1", port=8082)
    legalease_room.host = host
    return runtime, host, legalease_room


def add_connection(room, name=None, is_admin=False, vote=None, connected=True, session_token=None, tab_id=None):
    connection = demo._make_connection_record(
        room.state,
        ("127.0.0.1", 5000 + len(room.state["connections"])),
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


class TestPublicFlow(unittest.TestCase):
    def test_landing_and_setup_pages_are_served_by_host(self):
        _, host, _ = build_host()

        landing = host.landing_response()
        setup = host.setup_room_response()

        self.assertIn(b"Create a private room", landing)
        self.assertIn(b"/setupRoom", landing)
        self.assertIn(b"/legalease", landing)
        self.assertIn(b"Room admin password", setup)
        self.assertIn(b"/static/setup_room.js", setup)

    def test_room_creation_api_returns_guid_and_claim_token(self):
        _, host, _ = build_host()

        response = host.create_room_api_response(demo._json_bytes({"admin_passphrase": "room-secret"}))
        payload = response_json(response)

        self.assertIn(b"201 Created", response)
        self.assertTrue(re.fullmatch(r"[0-9a-f-]{36}", payload["room_id"]))
        self.assertEqual("/" + payload["room_id"], payload["room_url"])
        self.assertTrue(payload["creator_claim_token"])
        self.assertEqual(1, host.active_ephemeral_count())
        room = host.get_room(payload["room_id"])
        self.assertIsNotNone(room)
        self.assertTrue(room.state["session_open"])
        self.assertEqual(demo.EPHEMERAL_JOIN_LIMIT, room.join_limit)

    def test_room_creation_rejects_when_public_pool_is_full(self):
        _, host, _ = build_host()

        for index in range(demo.EPHEMERAL_ROOM_LIMIT):
            room, _ = host.create_ephemeral_room("room-secret-{}".format(index))
            self.assertIsNotNone(room)

        response = host.create_room_api_response(demo._json_bytes({"admin_passphrase": "extra-room"}))

        self.assertIn(b"503 Service Unavailable", response)
        self.assertEqual(demo.EPHEMERAL_ROOM_LIMIT, host.active_ephemeral_count())

    def test_host_resolves_legalease_and_dynamic_guid_rooms(self):
        _, host, legalease_room = build_host()
        room, _ = host.create_ephemeral_room("room-secret")

        self.assertIs(legalease_room, host.resolve_room("/legalease"))
        self.assertIs(legalease_room, host.resolve_room("/legalease/ws"))
        self.assertIs(room, host.resolve_room(room.base_path))
        self.assertIs(room, host.resolve_room(room.ws_path))
        self.assertIsNone(host.resolve_room("/"))

    def test_room_unavailable_page_is_friendly(self):
        _, host, _ = build_host()

        response = host.room_unavailable_response()

        self.assertIn(b"room is no longer available", response.lower())
        self.assertIn(b"/setupRoom", response)


class TestRoomAuthAndLimits(unittest.TestCase):
    def test_legalease_uses_dedicated_passphrase_and_falls_back_to_legacy(self):
        runtime, _, legalease_room = build_host()
        legalease_room.state["session_open"] = True
        connection = add_connection(legalease_room)

        with patch.dict(
            os.environ,
            {"LEGALEASE_ADMIN_PASSPHRASE": "legal-only", "ADMIN_PASSPHRASE": "legacy", "SUPER_USER_PASSPHRASE": ""},
            clear=False,
        ):
            self.assertIsNone(
                demo._apply_client_message(legalease_room.state, connection, {"type": "become_admin", "passphrase": "legal-only"})
            )

        self.assertTrue(connection["is_admin"])
        self.assertEqual([demo.OUTBOX_SIGNAL], connection["writer_task"].signals)
        self.assertEqual(runtime.kernel.now_ms, legalease_room.state["last_activity_ms"])

        fallback_connection = add_connection(legalease_room)
        with patch.dict(
            os.environ,
            {"LEGALEASE_ADMIN_PASSPHRASE": "", "ADMIN_PASSPHRASE": "legacy", "SUPER_USER_PASSPHRASE": ""},
            clear=False,
        ):
            self.assertIsNone(
                demo._apply_client_message(legalease_room.state, fallback_connection, {"type": "become_admin", "passphrase": "legacy"})
            )
        self.assertTrue(fallback_connection["is_admin"])

    def test_ephemeral_room_uses_room_password_not_legacy_admin_password(self):
        _, host, _ = build_host()
        room, _ = host.create_ephemeral_room("room-secret")
        connection = add_connection(room)

        with patch.dict(
            os.environ,
            {"ADMIN_PASSPHRASE": "legacy", "LEGALEASE_ADMIN_PASSPHRASE": "", "SUPER_USER_PASSPHRASE": ""},
            clear=False,
        ):
            error = demo._apply_client_message(room.state, connection, {"type": "become_admin", "passphrase": "legacy"})
            self.assertEqual("incorrect admin passphrase", error)
            self.assertIsNone(
                demo._apply_client_message(room.state, connection, {"type": "become_admin", "passphrase": "room-secret"})
            )

        self.assertTrue(connection["is_admin"])

    def test_super_user_passphrase_works_in_every_room(self):
        _, host, legalease_room = build_host()
        room, _ = host.create_ephemeral_room("room-secret")
        premium_connection = add_connection(legalease_room)
        ephemeral_connection = add_connection(room)

        with patch.dict(
            os.environ,
            {
                "LEGALEASE_ADMIN_PASSPHRASE": "legal-only",
                "ADMIN_PASSPHRASE": "",
                "SUPER_USER_PASSPHRASE": "all-access",
            },
            clear=False,
        ):
            self.assertIsNone(
                demo._apply_client_message(legalease_room.state, premium_connection, {"type": "become_admin", "passphrase": "all-access"})
            )
            self.assertIsNone(
                demo._apply_client_message(room.state, ephemeral_connection, {"type": "become_admin", "passphrase": "all-access"})
            )

        self.assertTrue(premium_connection["is_admin"])
        self.assertTrue(ephemeral_connection["is_admin"])

    def test_creator_claim_grants_admin_once(self):
        _, host, _ = build_host()
        room, creator_claim_token = host.create_ephemeral_room("room-secret")
        creator = add_connection(room)
        another = add_connection(room)

        self.assertIsNone(
            demo._apply_client_message(room.state, creator, {"type": "claim_creator_admin", "token": creator_claim_token})
        )
        self.assertTrue(creator["is_admin"])
        error = demo._apply_client_message(room.state, another, {"type": "claim_creator_admin", "token": creator_claim_token})
        self.assertEqual("creator admin claim is no longer available", error)

    def test_legalease_join_limit_is_20(self):
        _, _, legalease_room = build_host()
        legalease_room.state["session_open"] = True

        for index in range(demo.PREMIUM_JOIN_LIMIT):
            connection = add_connection(legalease_room)
            self.assertIsNone(
                demo._apply_client_message(legalease_room.state, connection, {"type": "join", "name": "Legal {}".format(index)})
            )

        overflow = add_connection(legalease_room)
        error = demo._apply_client_message(legalease_room.state, overflow, {"type": "join", "name": "Overflow"})

        self.assertEqual("this session is full (max 20 participants)", error)

    def test_ephemeral_join_limit_is_8(self):
        _, host, _ = build_host()
        room, _ = host.create_ephemeral_room("room-secret")

        for index in range(demo.EPHEMERAL_JOIN_LIMIT):
            connection = add_connection(room)
            self.assertIsNone(
                demo._apply_client_message(room.state, connection, {"type": "join", "name": "User {}".format(index)})
            )

        overflow = add_connection(room)
        error = demo._apply_client_message(room.state, overflow, {"type": "join", "name": "Overflow"})

        self.assertEqual("this session is full (max 8 participants)", error)


class TestCleanupAndShell(unittest.TestCase):
    def test_destroy_ephemeral_room_cleans_up_state_and_registry(self):
        runtime, host, _ = build_host()
        room, creator_claim_token = host.create_ephemeral_room("room-secret")
        connection = add_connection(room, name="Alice")
        connection_socket = connection["socket"]
        room_task = FakeWriterTask()
        room.room_task = room_task

        destroyed = host.destroy_ephemeral_room(room.room_id, reason="expired")

        self.assertTrue(destroyed)
        self.assertNotIn(room.room_id, host.ephemeral_rooms)
        self.assertTrue(room.destroyed)
        self.assertTrue(room.state["destroyed"])
        self.assertEqual("expired", room.state["destroy_reason"])
        self.assertIsNone(room.state["room_admin_passphrase"])
        self.assertIsNone(room.state["creator_claim_token"])
        self.assertTrue(room.state["creator_claim_used"])
        self.assertEqual({}, room.state["connections"])
        self.assertEqual({}, room.state["connections_by_token"])
        self.assertEqual([(room_task, False)], runtime.cancelled)
        self.assertEqual([connection_socket], runtime.kernel.closed)
        self.assertEqual(creator_claim_token is not None, True)

    def test_expire_ephemeral_rooms_removes_only_expired_rooms(self):
        runtime, host, _ = build_host()
        expired_room, _ = host.create_ephemeral_room("expired-room")
        active_room, _ = host.create_ephemeral_room("active-room")
        expired_room.state["expires_at_ms"] = runtime.kernel.now_ms - 1
        active_room.state["expires_at_ms"] = runtime.kernel.now_ms + 5000

        removed = host.expire_ephemeral_rooms(now_ms=runtime.kernel.now_ms)

        self.assertEqual(1, removed)
        self.assertIsNone(host.get_room(expired_room.room_id))
        self.assertIs(active_room, host.get_room(active_room.room_id))

    def test_shell_lists_rooms_and_targets_dynamic_room(self):
        _, host, legalease_room = build_host()
        room, _ = host.create_ephemeral_room("room-secret")
        room_connection = add_connection(room, name="Alice")
        legalease_connection = add_connection(legalease_room, name="Bob")
        room.state["session_open"] = True
        legalease_room.state["session_open"] = True
        shell = demo.ScrumPokerShell(host, allow_python=False)

        listed = shell.command_poker(["rooms"])
        response = shell.command_poker([room.room_id, "session", "close"])

        self.assertIn("Active scrum poker rooms:", listed)
        self.assertIn("- legalease: /legalease [premium]", listed)
        self.assertIn(room.room_id, listed)
        self.assertEqual("{} joining disabled".format(room.room_id), response)
        self.assertFalse(room.state["session_open"])
        self.assertTrue(legalease_room.state["session_open"])
        self.assertEqual([demo.OUTBOX_SIGNAL], room_connection["writer_task"].signals)
        self.assertEqual([], legalease_connection["writer_task"].signals)


class TestSharedHelpers(unittest.TestCase):
    def test_public_state_masks_other_votes_when_hidden(self):
        _, _, legalease_room = build_host()
        legalease_room.state["session_open"] = True
        legalease_room.state["votes_visible"] = False
        legalease_room.state["connections"] = {
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

        snapshot = demo._build_public_state(legalease_room.state, viewer_id=1)

        self.assertTrue(snapshot["participants"][0]["is_admin"])
        self.assertEqual("5", snapshot["participants"][0]["vote"])
        self.assertTrue(snapshot["participants"][1]["has_voted"])
        self.assertIsNone(snapshot["participants"][1]["vote"])
        self.assertEqual(demo.PREMIUM_JOIN_LIMIT, snapshot["join_limit"])
        self.assertTrue(snapshot["me"]["is_admin"])

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

    def test_shutdown_runtime_closes_host_and_all_room_sockets(self):
        task_a = object()
        task_b = object()
        runtime = FakeRuntime(tasks=[task_a, task_b])
        _, host, legalease_room = build_host(runtime=runtime)
        room, _ = host.create_ephemeral_room("room-secret")
        listener = object()
        legalease_socket = object()
        room_socket = object()
        host.listener = listener
        legalease_room.state["connections"] = {
            1: {
                "client_id": 1,
                "connected": True,
                "outbox": [],
                "shutdown_after_drain": False,
                "socket": legalease_socket,
                "websocket": object(),
            }
        }
        room.state["connections"] = {
            2: {
                "client_id": 2,
                "connected": True,
                "outbox": [],
                "shutdown_after_drain": False,
                "socket": room_socket,
                "websocket": object(),
            }
        }

        demo._shutdown_runtime(runtime, host, [legalease_room])

        self.assertIsNone(host.listener)
        self.assertEqual([listener, room_socket, legalease_socket], runtime.kernel.closed)
        self.assertEqual([(task_a, False), (task_b, False)], runtime.cancelled)


if __name__ == "__main__":
    unittest.main()
