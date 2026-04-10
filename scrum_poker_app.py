"""Cooperative websocket scrum poker app built on top of SmallOS."""

import hmac
import os
from datetime import datetime, timezone
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import SmallOS
from SmallOS.SmallPackage import SmallTask, Unix
from SmallOS.SmallPackage.SmallConfig import SmallOSConfig
from SmallOS.SmallPackage.SmallErrors import TaskCancelledError
from SmallOS.SmallPackage.SmallOS import SmallOS as SmallOSRuntime
from SmallOS.SmallPackage.shells import BaseShell, ShellCommandError
from smallos_websocket_server import SmallWebSocketServerConnection, WebSocketServerProtocolError


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8082))
LISTEN_BACKLOG = 24
REQUEST_HEADER_LIMIT = 8 * 1024
HEADER_READ_TIMEOUT_SECONDS = 10  # close slow-loris connections after this many seconds
MAX_FRAME_SIZE = 64 * 1024
MAX_MESSAGE_SIZE = 256 * 1024
OUTBOX_SIGNAL = 9
MAX_PARTICIPANTS = 50
MAX_CONNECTIONS = 200  # total concurrent WebSocket transports (joined + anonymous)
MAX_ADMIN_FAILURES = 5  # per-connection lockout after this many wrong passphrase guesses
MESSAGE_RATE_LIMIT_MS = 50  # minimum ms between processed messages per connection (~20/sec)
MESSAGE_RATE_BURST = 100  # disconnect after this many back-to-back throttled messages
ALLOWED_VOTES = (
    "0",
    "0.5",
    "1",
    "2",
    "3",
    "5",
    "8",
    "13",
    "21",
    "34",
    "55",
    "89",
    "?",
    "coffee",
)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SMALLOS_ROOT = next(iter(SmallOS.__path__))
CONFIG_PATH = os.path.join(SMALLOS_ROOT, "smallos.config.json")
DOTENV_PATH = os.path.join(PROJECT_ROOT, ".env")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
SESSION_RESUME_GRACE_SECONDS = 45  # how long a disconnected participant's slot is held before they're removed from the board
IDLE_TIMEOUT_SECONDS = 3600  # 1 hour of no activity kicks and closes the session
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


def _http_reason(status_code):
    """Return a short reason phrase for one HTTP status code."""
    reasons = {
        101: "Switching Protocols",
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        426: "Upgrade Required",
        500: "Internal Server Error",
    }
    return reasons.get(status_code, "OK")


def _json_bytes(value):
    """Encode one JSON object as compact UTF-8 bytes."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _json_text(value):
    """Encode one JSON object as compact UTF-8 text."""
    return _json_bytes(value).decode("utf-8")


def _http_response(status_code, body, content_type="text/plain; charset=utf-8", headers=None):
    """Build one complete HTTP/1.1 response payload."""
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray, memoryview)):
        body_bytes = bytes(body)
    else:
        raise TypeError("HTTP response body must be str or bytes-like.")

    response_headers = [
        "HTTP/1.1 {} {}".format(status_code, _http_reason(status_code)),
        "Content-Type: {}".format(content_type),
        "Content-Length: {}".format(len(body_bytes)),
        "Connection: close",
        "Cache-Control: no-store",
        "X-Content-Type-Options: nosniff",
        "X-Frame-Options: DENY",
        "Referrer-Policy: no-referrer",
        "Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'",
    ]
    for name, value in list(headers or ()):
        response_headers.append("{}: {}".format(name, value))
    response_headers.extend(["", ""])
    return "\r\n".join(response_headers).encode("utf-8") + body_bytes


def _strip_dotenv_comment(line):
    """Remove one inline dotenv comment while respecting quotes."""
    quote = None
    chars = []

    for char in line:
        if quote is None and char == "#":
            break
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        chars.append(char)

    return "".join(chars).strip()


def _parse_dotenv_assignment(line):
    """Parse one ``KEY=VALUE`` assignment from a dotenv file line."""
    text = _strip_dotenv_comment(line)
    if not text or "=" not in text:
        return None

    key, value = text.split("=", 1)
    key = key.strip()
    value = value.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if not key:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def _load_dotenv_file(path=DOTENV_PATH, override=False):
    """Load environment variables from a local `.env` file."""
    loaded = {}

    try:
        with open(path, "r", encoding="utf-8") as dotenv_file:
            for line in dotenv_file:
                assignment = _parse_dotenv_assignment(line)
                if assignment is None:
                    continue
                key, value = assignment
                if override or key not in os.environ:
                    os.environ[key] = value
                loaded[key] = os.environ.get(key, value)
    except OSError:
        return {}

    return loaded


_load_dotenv_file()


def _get_admin_passphrase():
    """Return the currently configured admin passphrase."""
    return os.environ.get("ADMIN_PASSPHRASE", "").strip()


def _admin_auth_enabled():
    """Report whether browser-side admin elevation is configured."""
    return bool(_get_admin_passphrase())


def _read_static_asset(filename):
    """Load one static asset from disk."""
    asset_path = os.path.join(STATIC_DIR, filename)
    with open(asset_path, "rb") as asset_file:
        return asset_file.read()


def _static_asset_response(path):
    """Build an HTTP response for one known static asset path."""
    asset = STATIC_ASSETS.get(path)
    if asset is None:
        return None

    filename, content_type = asset
    try:
        body = _read_static_asset(filename)
    except OSError:
        return _http_response(500, "static asset unavailable\n")
    return _http_response(200, body, content_type)


def _new_state(runtime):
    """Return the shared in-memory scrum poker session state."""
    now = runtime.kernel.scheduler_now_ms()
    return {
        "kernel": runtime.kernel,
        "listener": None,
        "os": runtime,
        "started_ms": now,
        "last_activity_ms": now,
        "session_open": False,
        "votes_visible": False,
        "next_connection_id": 1,
        "connections": {},
        "connections_by_token": {},
    }


def _build_runtime():
    """Create one SmallOS runtime from the bundled project config."""
    config = SmallOSConfig.from_json_file(CONFIG_PATH)
    return SmallOSRuntime(config=config).setKernel(Unix())


def _close_socket_quietly(kernel, sock):
    """Close one socket-like object while ignoring shutdown races."""
    if kernel is None or sock is None:
        return
    if hasattr(sock, "shutdown") and hasattr(sock, "SHUT_RDWR"):
        try:
            sock.shutdown(sock.SHUT_RDWR)
        except Exception:
            pass
    try:
        kernel.socket_close(sock)
    except Exception:
        pass


def _shutdown_runtime(runtime, state):
    """Cancel active tasks and close network sockets so the port is released."""
    if runtime is None:
        return

    kernel = getattr(runtime, "kernel", None)
    _close_socket_quietly(kernel, state.get("listener"))
    state["listener"] = None

    for connection in list(state.get("connections", {}).values()):
        connection["connected"] = False
        connection["shutdown_after_drain"] = True
        connection["websocket"] = None
        _close_socket_quietly(kernel, connection.get("socket"))
        connection["socket"] = None

    task_registry = getattr(runtime, "tasks", None)
    if task_registry is None or not hasattr(task_registry, "list"):
        return

    for active_task in list(task_registry.list()):
        try:
            runtime.cancel_task(active_task)
        except Exception:
            pass


def _normalize_name(value):
    """Trim and validate one participant name."""
    text = " ".join(str("" if value is None else value).split()).strip()
    if not text or len(text) > 32:
        return None
    return text


def _normalize_vote(value):
    """Normalize one incoming vote value into the shared string format."""
    text = str("" if value is None else value).strip()
    if text in ALLOWED_VOTES:
        return text
    return None


def _normalize_session_token(value):
    """Normalize one browser session token."""
    token = str("" if value is None else value).strip()
    if not token:
        return None
    return token


def _normalize_tab_id(value):
    """Normalize one per-tab browser identifier."""
    tab_id = str("" if value is None else value).strip()
    if not tab_id:
        return None
    return tab_id


def _now_ms(state):
    """Return the scheduler clock used for reconnect grace periods."""
    kernel = state.get("kernel")
    if kernel is None or not hasattr(kernel, "scheduler_now_ms"):
        return 0
    return kernel.scheduler_now_ms()


def _touch_activity(state):
    """Record that meaningful activity just occurred, resetting the idle clock."""
    state["last_activity_ms"] = _now_ms(state)


def _connected_count(state):
    """Return the number of live websocket transports."""
    return sum(1 for connection in state.get("connections", {}).values() if connection.get("connected", True))


def _joined_count(state):
    """Return the number of participants that currently occupy a board slot."""
    _expire_stale_connections(state)
    return sum(1 for connection in state.get("connections", {}).values() if connection.get("name") and not connection.get("closed"))


def _new_session_token():
    """Return one opaque browser session token."""
    return secrets.token_urlsafe(24)


def _remove_connection_record(state, connection):
    """Remove one participant record from the in-memory registries."""
    if connection is None:
        return

    token = connection.get("session_token")
    if token and state.get("connections_by_token", {}).get(token) is connection:
        del state["connections_by_token"][token]
    if state.get("connections", {}).get(connection.get("client_id")) is connection:
        del state["connections"][connection["client_id"]]
    connection["closed"] = True


def _expire_stale_connections(state):
    """Drop disconnected participant records once their resume window expires."""
    now_ms = _now_ms(state)
    for connection in list(state.get("connections", {}).values()):
        if connection.get("closed"):
            _remove_connection_record(state, connection)
            continue
        deadline = connection.get("resume_deadline_ms")
        if connection.get("connected", True) or deadline is None:
            continue
        if now_ms >= deadline:
            _remove_connection_record(state, connection)


def _connections_over_hard_cap(state):
    """Return True when the total connection record count is at the hard cap.

    Triggers a forced stale-connection sweep first so normal expiry doesn't
    count against legitimate users.
    """
    _expire_stale_connections(state)
    return len(state.get("connections", {})) >= MAX_CONNECTIONS * 2


def _make_connection_record(state, client_addr, session_token=None, tab_id=None):
    """Create one persistent participant record."""
    client_id = state["next_connection_id"]
    state["next_connection_id"] += 1

    record = {
        "addr": str(client_addr),
        "client_id": client_id,
        "closed": False,
        "connected": False,
        "is_admin": False,
        "name": None,
        "outbox": [],
        "resume_deadline_ms": None,
        "session_token": session_token or _new_session_token(),
        "shutdown_after_drain": False,
        "socket": None,
        "session_task": None,
        "tab_id": tab_id,
        "transport_id": 0,
        "vote": None,
        "websocket": None,
        "writer_task": None,
    }
    state["connections"][client_id] = record
    state["connections_by_token"][record["session_token"]] = record
    return record


def _attach_connection_transport(task, state, connection, sock, client_addr):
    """Attach the current websocket transport to one participant record."""
    os_ref = state.get("os")
    old_session = connection.get("session_task")
    if old_session is not None and old_session is not task and not old_session.done:
        if os_ref is not None:
            try:
                os_ref.cancel_task(old_session)
            except Exception:
                pass

    old_writer = connection.get("writer_task")
    if old_writer is not None and not old_writer.done:
        if os_ref is not None:
            try:
                os_ref.cancel_task(old_writer)
            except Exception:
                pass

    old_socket = connection.get("socket")
    if old_socket is not None and old_socket is not sock:
        try:
            state["kernel"].socket_close(old_socket)
        except Exception:
            pass

    connection["addr"] = str(client_addr)
    connection["closed"] = False
    connection["connected"] = True
    connection["outbox"] = []
    connection["resume_deadline_ms"] = None
    connection["session_task"] = task
    connection["shutdown_after_drain"] = False
    connection["socket"] = sock
    connection["transport_id"] = int(connection.get("transport_id", 0)) + 1
    connection["websocket"] = None

    writer_task = task.spawn(
        websocket_writer_task,
        priority=max(1, task.priority - 1),
        name="ws_writer",
        args=(connection,),
    )
    connection["writer_task"] = writer_task
    return writer_task


def _resolve_connection_for_socket(task, state, sock, client_addr, session_token=None, tab_id=None):
    """Resolve the persistent participant record for one inbound websocket socket."""
    _expire_stale_connections(state)
    normalized_token = _normalize_session_token(session_token)
    normalized_tab_id = _normalize_tab_id(tab_id)
    if normalized_token is not None:
        existing = state.get("connections_by_token", {}).get(normalized_token)
        if existing is not None and not existing.get("closed"):
            existing_tab_id = existing.get("tab_id")
            if normalized_tab_id is not None and existing_tab_id in (None, normalized_tab_id):
                existing["tab_id"] = normalized_tab_id
                _attach_connection_transport(task, state, existing, sock, client_addr)
                return existing, True
            normalized_token = None

    record = _make_connection_record(
        state,
        client_addr,
        session_token=normalized_token,
        tab_id=normalized_tab_id,
    )
    _attach_connection_transport(task, state, record, sock, client_addr)
    return record, False


def _iter_participants(state):
    """Return joined participants in a stable display order."""
    _expire_stale_connections(state)
    joined = [
        connection
        for connection in state["connections"].values()
        if connection.get("name") and not connection.get("closed")
    ]
    joined.sort(key=lambda connection: connection["client_id"])
    return joined


def _build_public_state(state, viewer_id=None):
    """Build the browser-visible session snapshot for one connection."""
    _expire_stale_connections(state)
    participants = []
    viewer = state["connections"].get(viewer_id)

    for connection in _iter_participants(state):
        is_self = connection["client_id"] == viewer_id
        vote_value = connection.get("vote")
        if not state["votes_visible"] and not is_self:
            vote_value = None

        participants.append(
            {
                "client_id": connection["client_id"],
                "has_voted": connection.get("vote") is not None,
                "is_admin": bool(connection.get("is_admin")),
                "is_connected": bool(connection.get("connected", True)),
                "is_self": is_self,
                "name": connection["name"],
                "vote": vote_value,
            }
        )

    return {
        "admin_auth_enabled": _admin_auth_enabled(),
        "connected_count": _connected_count(state),
        "me": (
            {
                "client_id": viewer["client_id"],
                "is_admin": bool(viewer.get("is_admin")),
                "name": viewer.get("name"),
                "session_token": viewer.get("session_token"),
                "vote": viewer.get("vote"),
            }
            if viewer is not None
            else None
        ),
        "participant_count": len(participants),
        "participants": participants,
        "server_time": datetime.now(timezone.utc).strftime("%H:%M:%SZ"),
        "session_open": bool(state["session_open"]),
        "vote_options": list(ALLOWED_VOTES),
        "votes_visible": bool(state["votes_visible"]),
    }


def _build_state_message(state, viewer_id=None):
    """Encode one personalized websocket state payload."""
    return _json_text(
        {
            "clear_error": True,
            "state": _build_public_state(state, viewer_id),
            "type": "state",
        }
    )


def _queue_connection_text(connection, text):
    """Append one text message to a connection outbox and wake its writer."""
    if connection.get("closed") or not connection.get("connected", True):
        return
    connection["outbox"].append(text)
    writer_task = connection.get("writer_task")
    if writer_task is not None and not writer_task.done:
        writer_task.acceptSignal(OUTBOX_SIGNAL)


def _broadcast_state(state):
    """Queue a fresh state snapshot for every connected websocket client."""
    _expire_stale_connections(state)
    for connection in list(state["connections"].values()):
        _queue_connection_text(connection, _build_state_message(state, connection["client_id"]))


def _queue_error(connection, message):
    """Queue one server-side error payload for a single client."""
    _queue_connection_text(connection, _json_text({"message": message, "type": "error"}))


def _queue_notice(connection, message, kind="info"):
    """Queue one user-facing notice payload for a single client."""
    _queue_connection_text(
        connection,
        _json_text({"kind": kind, "message": message, "type": "notice"}),
    )


def _clear_votes(state):
    """Discard all active votes and collapse the board back to hidden mode."""
    for connection in state["connections"].values():
        connection["vote"] = None
    state["votes_visible"] = False


def _set_session_open(state, is_open):
    """Update whether new participants may join the session."""
    state["session_open"] = bool(is_open)


def _parse_client_id(value):
    """Parse one client identifier from a websocket payload."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kick_connection(state, target_connection):
    """Disconnect one websocket client and remove it from the shared state."""
    if target_connection is None:
        return

    os_ref = state.get("os")
    session_task = target_connection.get("session_task")
    if session_task is not None and not session_task.done and os_ref is not None:
        try:
            os_ref.cancel_task(session_task)
        except Exception:
            pass

    target_connection["connected"] = False
    target_connection["closed"] = True
    target_connection["shutdown_after_drain"] = True
    target_connection["outbox"] = []
    target_connection["websocket"] = None
    target_connection["session_task"] = None
    _remove_connection_record(state, target_connection)

    writer_task = target_connection.get("writer_task")
    if writer_task is not None and not writer_task.done:
        if os_ref is not None:
            try:
                os_ref.cancel_task(writer_task)
            except Exception:
                writer_task.acceptSignal(OUTBOX_SIGNAL)
        else:
            writer_task.acceptSignal(OUTBOX_SIGNAL)

    socket_obj = target_connection.get("socket")
    kernel = state.get("kernel")
    if socket_obj is None:
        return

    try:
        if kernel is not None:
            kernel.socket_close(socket_obj)
        else:
            socket_obj.close()
    except Exception:
        pass


def _clear_everyone(state):
    """Kick every participant off the board and close the session."""
    participants = list(state.get("connections", {}).values())
    for connection in participants:
        _kick_connection(state, connection)
    state["session_open"] = False
    state["votes_visible"] = False
    _touch_activity(state)
    return len(participants)


def _apply_client_message(state, connection, payload):
    """Apply one parsed client websocket message to the shared state."""
    if not isinstance(payload, dict):
        return "messages must be JSON objects"

    message_type = payload.get("type")

    if message_type == "join":
        name = _normalize_name(payload.get("name"))
        if name is None:
            return "names must be between 1 and 32 characters"
        if not state["session_open"] and not connection.get("name") and not connection.get("is_admin"):
            return "joining is currently disabled by the administrator"
        if not connection.get("name") and not connection.get("is_admin") and _joined_count(state) >= MAX_PARTICIPANTS:
            return "this session is full (max {} participants)".format(MAX_PARTICIPANTS)
        connection["name"] = name
        _touch_activity(state)
        return None

    if message_type == "become_admin":
        expected = _get_admin_passphrase()
        if not expected:
            return "admin access is not configured on this server"
        # Lock out this connection after too many failures to prevent brute-force
        failures = connection.get("admin_failures", 0)
        if failures >= MAX_ADMIN_FAILURES:
            return "too many failed attempts — reconnect to try again"
        supplied = str("" if payload.get("passphrase") is None else payload.get("passphrase"))
        # Use constant-time comparison to prevent timing side-channel attacks
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            connection["admin_failures"] = failures + 1
            return "incorrect admin passphrase"
        connection["admin_failures"] = 0
        connection["is_admin"] = True
        _queue_notice(connection, "Admin access granted.", kind="success")
        return None

    if message_type == "vote":
        if not connection.get("name"):
            return "join the session before voting"
        vote = _normalize_vote(payload.get("value"))
        if vote is None:
            return "unsupported vote value"
        connection["vote"] = vote
        _touch_activity(state)
        return None

    if message_type == "toggle_votes":
        if not connection.get("name"):
            return "join the session before revealing votes"
        state["votes_visible"] = not state["votes_visible"]
        _touch_activity(state)
        return None

    if message_type == "show_votes":
        if not connection.get("name"):
            return "join the session before revealing votes"
        state["votes_visible"] = True
        _touch_activity(state)
        return None

    if message_type == "hide_votes":
        if not connection.get("name"):
            return "join the session before hiding votes"
        state["votes_visible"] = False
        _touch_activity(state)
        return None

    if message_type == "clear_votes":
        if not connection.get("name"):
            return "join the session before discarding votes"
        _clear_votes(state)
        _touch_activity(state)
        return None

    if message_type == "set_session_open":
        if not connection.get("is_admin"):
            return "admin privileges required"
        desired_state = payload.get("open")
        if desired_state not in (True, False):
            return "session action requires open=true or open=false"
        _set_session_open(state, desired_state)
        _touch_activity(state)
        _queue_notice(
            connection,
            "Session opened for new participants." if desired_state else "Session closed for new participants.",
            kind="success",
        )
        return None

    if message_type == "kick_user":
        if not connection.get("is_admin"):
            return "admin privileges required"
        target_id = _parse_client_id(payload.get("client_id"))
        if target_id is None:
            return "kick_user requires a numeric client_id"
        if target_id == connection.get("client_id"):
            return "admins cannot kick themselves"
        target_connection = state["connections"].get(target_id)
        if target_connection is None:
            return "that user is no longer connected"
        target_label = target_connection.get("name") or "client {}".format(target_id)
        _kick_connection(state, target_connection)
        _touch_activity(state)
        _queue_notice(connection, "Removed {} from the session.".format(target_label), kind="success")
        return None

    return "unsupported message type"


class ScrumPokerShell(BaseShell):
    """SmallOS shell extension that controls whether the room accepts joins."""

    def __init__(self, poker_state, *args, **kwargs):
        self.poker_state = poker_state
        self._poker_command_names = []
        super().__init__(*args, **kwargs)

    def _register_poker_command(self, name, handler, help_text, aliases=()):
        """Register one scrum-poker-specific shell command."""
        self._register_command(name, handler, help_text, aliases=aliases)
        self._poker_command_names.append(name)

    def _register_builtin_commands(self):
        super()._register_builtin_commands()
        self._register_poker_command(
            "poker",
            self.command_poker,
            "Show ScrumPokerShell-specific commands. Usage: poker [command]",
            aliases=("scrum",),
        )
        self._register_poker_command(
            "session",
            self.command_session,
            "Manage team joins. Usage: session [open|close|status|toggle]",
            aliases=("joins",),
        )
        self._register_poker_command(
            "idle",
            self.command_idle,
            "Show or reset the idle timeout clock. Usage: idle [reset]",
        )
        self._register_poker_command(
            "clear",
            self.command_clear,
            "Kick everyone off the board. Usage: clear everyone",
        )

    def command_poker(self, args):
        """Show scrum-poker-specific shell commands."""
        if args:
            name = self.aliases.get(args[0], args[0])
            if name not in self._poker_command_names:
                raise ShellCommandError("unknown scrum poker command {!r}".format(args[0]))
            aliases = sorted(alias for alias, target in self.aliases.items() if target == name)
            alias_text = " (aliases: {})".format(", ".join(aliases)) if aliases else ""
            return "{}{}: {}".format(name, alias_text, self._command_help[name])

        lines = ["Scrum poker commands:"]
        for name in sorted(self._poker_command_names):
            aliases = sorted(alias for alias, target in self.aliases.items() if target == name)
            alias_text = " (aliases: {})".format(", ".join(aliases)) if aliases else ""
            lines.append("- {}{}: {}".format(name, alias_text, self._command_help[name]))
        return "\n".join(lines)

    def command_session(self, args):
        """Open, close, or inspect the scrum poker join gate."""
        action = args[0] if args else "status"

        if action == "status":
            return "joining is {}".format("open" if self.poker_state["session_open"] else "closed")

        if action == "open":
            self.poker_state["session_open"] = True
            _touch_activity(self.poker_state)
            _broadcast_state(self.poker_state)
            return "joining enabled"

        if action == "close":
            self.poker_state["session_open"] = False
            _touch_activity(self.poker_state)
            _broadcast_state(self.poker_state)
            return "joining disabled"

        if action == "toggle":
            self.poker_state["session_open"] = not self.poker_state["session_open"]
            _touch_activity(self.poker_state)
            _broadcast_state(self.poker_state)
            return "joining {}".format("enabled" if self.poker_state["session_open"] else "disabled")

        raise ShellCommandError("usage: session [open|close|status|toggle]")

    def command_idle(self, args):
        """Show time since last activity, or reset the idle clock."""
        action = args[0] if args else "status"

        if action == "reset":
            _touch_activity(self.poker_state)
            return "idle clock reset"

        if action == "status":
            idle_ms = _now_ms(self.poker_state) - self.poker_state.get("last_activity_ms", _now_ms(self.poker_state))
            idle_s = max(0, idle_ms // 1000)
            remaining_s = max(0, IDLE_TIMEOUT_SECONDS - idle_s)
            return "idle for {}s / timeout in {}s ({}min)".format(idle_s, remaining_s, remaining_s // 60)

        raise ShellCommandError("usage: idle [status|reset]")

    def command_clear(self, args):
        """Kick everyone off the board and close the session."""
        action = args[0] if args else "everyone"

        if action not in ("everyone", "all"):
            raise ShellCommandError("usage: clear everyone")

        cleared_count = _clear_everyone(self.poker_state)
        _broadcast_state(self.poker_state)
        return "cleared {} participant(s); session closed".format(cleared_count)


async def idle_watchdog_task(task, state):
    """Kick everyone and close the session after IDLE_TIMEOUT_SECONDS of no activity."""
    check_interval = 60  # wake up every minute to check
    while True:
        await task.sleep(check_interval)
        idle_ms = _now_ms(state) - state.get("last_activity_ms", _now_ms(state))
        if idle_ms < IDLE_TIMEOUT_SECONDS * 1000:
            continue

        task.OS.print("[scrum-poker] idle timeout reached — kicking all participants\n")
        for connection in list(state.get("connections", {}).values()):
            if connection.get("connected") and connection.get("name"):
                _queue_notice(connection, "Session closed due to inactivity.", kind="error")
        # Give the writer tasks a brief moment to flush the notice before kicking.
        await task.sleep(1)
        _clear_everyone(state)
        task.OS.print("[scrum-poker] session closed after idle timeout\n")


async def _send_all(task, sock, data):
    """Write all bytes to a non-blocking socket using cooperative waits."""
    kernel = task.OS.kernel
    remaining = memoryview(bytes(data))

    while remaining:
        try:
            sent = kernel.socket_send(sock, remaining)
        except Exception as exc:
            if kernel.socket_needs_read(exc):
                await task.wait_readable(sock)
                continue
            if kernel.socket_needs_write(exc):
                await task.wait_writable(sock)
                continue
            raise

        if sent == 0:
            raise ConnectionError("socket closed while sending")
        remaining = remaining[sent:]


async def _read_exact(task, sock, length):
    """Read exactly ``length`` bytes or raise EOFError."""
    kernel = task.OS.kernel
    data = bytearray()

    while len(data) < length:
        try:
            chunk = kernel.socket_recv(sock, length - len(data))
        except Exception as exc:
            if kernel.socket_needs_read(exc):
                await task.wait_readable(sock)
                continue
            if kernel.socket_needs_write(exc):
                await task.wait_writable(sock)
                continue
            raise

        if not chunk:
            raise EOFError("socket closed")
        data.extend(chunk)

    return bytes(data)


async def _read_request_head(task, sock):
    """Read HTTP headers until CRLFCRLF or until the safety cap is hit."""
    kernel = task.OS.kernel
    data = bytearray()
    deadline_ms = kernel.scheduler_now_ms() + (HEADER_READ_TIMEOUT_SECONDS * 1000)

    while b"\r\n\r\n" not in data:
        if kernel.scheduler_now_ms() > deadline_ms:
            raise TimeoutError("request header read timed out")
        try:
            chunk = kernel.socket_recv(sock, 1024)
        except Exception as exc:
            if kernel.socket_needs_read(exc):
                await task.wait_readable(sock)
                continue
            if kernel.socket_needs_write(exc):
                await task.wait_writable(sock)
                continue
            raise

        if not chunk:
            break

        data.extend(chunk)
        if len(data) > REQUEST_HEADER_LIMIT:
            raise ValueError("request headers exceeded {} bytes".format(REQUEST_HEADER_LIMIT))

    return bytes(data)


def _parse_http_request(raw_request):
    """Parse the request line and headers from one HTTP request head."""
    if not raw_request:
        return None, None, {}

    lines = raw_request.split(b"\r\n")
    request_line = lines[0].decode("iso-8859-1", errors="replace")
    parts = request_line.split(" ")
    if len(parts) < 2:
        return None, None, {}

    method = parts[0].upper()
    target = parts[1]
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        headers[name.decode("iso-8859-1", errors="replace").strip().lower()] = value.decode(
            "iso-8859-1", errors="replace"
        ).strip()
    return method, target, headers


def _parse_request_target(target):
    """Split one request target into path plus flattened query params."""
    parsed = urlsplit(target)
    params = {}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if values:
            params[key] = values[-1]
    return parsed.path or "/", params


async def websocket_writer_task(task, connection):
    """Serialize all writes for one websocket client connection."""
    websocket = connection["websocket"]

    try:
        while True:
            while connection["outbox"]:
                message = connection["outbox"].pop(0)
                await websocket.send_text(message)

            if connection.get("shutdown_after_drain"):
                return "writer stopped"

            await task.wait_signal(OUTBOX_SIGNAL)
    except Exception:
        if connection.get("closed") or connection.get("shutdown_after_drain"):
            return "writer stopped"
        raise


async def websocket_session(task, sock, client_addr, state, headers):
    """Upgrade one HTTP client into a live websocket scrum poker session."""
    # H3: Validate WebSocket Origin to prevent cross-site hijacking.
    # Set ALLOWED_ORIGINS env var to a comma-separated list of allowed origins
    # (e.g. "https://poker.example.com"). Leave unset to allow all origins
    # (safe when nginx/Cloudflare restricts public access to one hostname).
    allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if allowed_origins_env:
        allowed_origins = {o.strip().rstrip("/") for o in allowed_origins_env.split(",") if o.strip()}
        request_origin = headers.get("origin", "").strip().rstrip("/")
        if request_origin and request_origin not in allowed_origins:
            await _send_all(task, sock, _http_response(403, "origin not allowed\n"))
            return

    # C1/M3: Reject new WebSocket connections when the server is at capacity.
    # Also enforces a hard cap on total connection records (including stale ones)
    # to prevent rapid connect/disconnect from accumulating unbounded memory.
    if _connected_count(state) >= MAX_CONNECTIONS or _connections_over_hard_cap(state):
        await _send_all(task, sock, _http_response(503, "too many connections\n"))
        return

    try:
        websocket = await SmallWebSocketServerConnection.accept(
            task,
            sock,
            headers,
            max_frame_size=MAX_FRAME_SIZE,
            max_message_size=MAX_MESSAGE_SIZE,
        )
    except WebSocketServerProtocolError as exc:
        await _send_all(task, sock, _http_response(400, str(exc) + "\n"))
        return
    _, params = _parse_request_target(headers.get(":target", "/ws"))
    connection, resumed = _resolve_connection_for_socket(
        task,
        state,
        sock,
        client_addr,
        session_token=params.get("session_token"),
        tab_id=params.get("tab_id"),
    )
    transport_id = connection["transport_id"]
    connection["websocket"] = websocket
    writer_task = connection.get("writer_task")

    task.OS.print(
        "[scrum-poker] websocket client {} {} from {}\n".format(
            connection["client_id"],
            "resumed" if resumed else "connected",
            client_addr,
        )
    )
    _broadcast_state(state)

    skip_async_cleanup = False
    try:
        while True:
            event = await websocket.receive()
            event_type = event["type"]

            if event_type == "pong":
                continue

            if event_type == "close":
                return

            if event_type == "binary":
                _queue_error(connection, "binary websocket messages are not supported")
                continue

            # H5: Per-connection message rate limiting (~20 messages/sec).
            now_ms = _now_ms(state)
            if now_ms - connection.get("last_msg_ms", 0) < MESSAGE_RATE_LIMIT_MS:
                burst = connection.get("rate_burst", 0) + 1
                connection["rate_burst"] = burst
                if burst > MESSAGE_RATE_BURST:
                    _queue_error(connection, "rate limit exceeded — slowing down too fast")
                    connection["shutdown_after_drain"] = True
                    if writer_task is not None:
                        writer_task.acceptSignal(OUTBOX_SIGNAL)
                    return
                continue
            connection["last_msg_ms"] = now_ms
            connection["rate_burst"] = 0

            try:
                message = json.loads(event["data"])
            except (ValueError, RecursionError):
                _queue_error(connection, "messages must contain valid JSON")
                continue

            error = _apply_client_message(state, connection, message)
            if error is not None:
                _queue_error(connection, error)
                continue

            _broadcast_state(state)
    except WebSocketServerProtocolError:
        try:
            await websocket.send_close(code=1002, reason="protocol error")
        except Exception:
            pass
        return
    except EOFError:
        return
    except GeneratorExit:
        skip_async_cleanup = True
        raise
    finally:
        is_current_transport = (
            state.get("connections", {}).get(connection["client_id"]) is connection
            and connection.get("transport_id") == transport_id
            and connection.get("socket") is sock
        )

        if is_current_transport:
            connection["connected"] = False
            connection["shutdown_after_drain"] = True
            connection["resume_deadline_ms"] = _now_ms(state) + (SESSION_RESUME_GRACE_SECONDS * 1000)
            connection["socket"] = None
            connection["session_task"] = None
            connection["websocket"] = None
            if writer_task is not None:
                writer_task.acceptSignal(OUTBOX_SIGNAL)

        if skip_async_cleanup:
            if writer_task is not None:
                try:
                    task.OS.cancel_task(writer_task)
                except Exception:
                    pass
        elif writer_task is not None:
            try:
                await task.join(writer_task)
            except TaskCancelledError:
                pass
            except LookupError:
                pass

        if is_current_transport:
            connection["writer_task"] = None
            task.OS.print("[scrum-poker] websocket client {} disconnected\n".format(connection["client_id"]))
            _expire_stale_connections(state)
            _broadcast_state(state)


async def web_client_handler(task, client_sock, client_addr, state):
    """Handle one inbound HTTP client connection."""
    kernel = task.OS.kernel

    try:
        try:
            request_head = await _read_request_head(task, client_sock)
        except TimeoutError:
            # C2: Slow-loris connection — header arrived too slowly; drop it silently.
            return
        except ValueError:
            await _send_all(task, client_sock, _http_response(413, "request header too large\n"))
            return

        method, target, headers = _parse_http_request(request_head)
        if method is None:
            return

        path, _ = _parse_request_target(target)
        is_websocket = (
            method == "GET"
            and path == "/ws"
            and "upgrade" in headers.get("connection", "").lower()
            and headers.get("upgrade", "").lower() == "websocket"
        )
        if is_websocket:
            headers = dict(headers)
            headers[":target"] = target
            await websocket_session(task, client_sock, client_addr, state, headers)
            return

        static_payload = _static_asset_response(path) if method == "GET" else None
        if method != "GET":
            payload = _http_response(405, "only GET is supported\n")
        elif static_payload is not None:
            payload = static_payload
        elif path == "/api/state":
            # M2: Require a valid session token to access the raw state endpoint.
            # This prevents unauthenticated metadata harvesting.
            _, api_params = _parse_request_target(target)
            api_token = api_params.get("session_token")
            if not api_token or api_token not in state.get("connections_by_token", {}):
                payload = _http_response(403, "forbidden\n")
            else:
                payload = _http_response(
                    200,
                    _json_bytes(_build_public_state(state)),
                    "application/json; charset=utf-8",
                )
        elif path == "/healthz":
            payload = _http_response(200, "ok\n")
        else:
            payload = _http_response(404, "route not found\n")

        await _send_all(task, client_sock, payload)
    finally:
        kernel.socket_close(client_sock)


async def web_server_task(task, state):
    """Run the cooperative HTTP and WebSocket server."""
    kernel = task.OS.kernel
    if kernel is None:
        raise RuntimeError("web_server_task requires a kernel-enabled runtime.")

    address_info = kernel.resolve_address(HOST, PORT)
    listener = kernel.socket_open(address_info)

    if hasattr(listener, "setsockopt") and hasattr(listener, "SOL_SOCKET"):
        for option_name in ("SO_REUSEADDR", "SO_REUSEPORT"):
            if not hasattr(listener, option_name):
                continue
            try:
                listener.setsockopt(listener.SOL_SOCKET, getattr(listener, option_name), 1)
            except Exception:
                pass

    listener.bind(address_info[4])
    listener.listen(LISTEN_BACKLOG)
    kernel.socket_setblocking(listener, False)
    state["listener"] = listener

    task.OS.print("smallOS scrum poker running on http://{}:{}/\n".format(HOST, PORT))
    task.OS.print("routes: /  /ws  /api/state  /healthz\n")
    task.OS.print("Open the shell and use: poker | session open | session close | session status\n")

    try:
        while True:
            try:
                client_sock, client_addr = listener.accept()
            except Exception as exc:
                if kernel.socket_needs_read(exc):
                    await task.wait_readable(listener)
                    continue
                if kernel.socket_needs_write(exc):
                    await task.wait_writable(listener)
                    continue
                raise

            kernel.socket_setblocking(client_sock, False)
            task.spawn(
                web_client_handler,
                priority=max(1, task.priority - 1),
                name="http_client",
                args=(client_sock, client_addr, state),
            )
            await task.yield_now()
    finally:
        if state.get("listener") is listener:
            state["listener"] = None
        _close_socket_quietly(kernel, listener)


def main():
    """Start the SmallOS scrum poker runtime."""
    runtime = _build_runtime()
    state = _new_state(runtime)

    shell = ScrumPokerShell(state, prompt="poker> ", allow_python=False)
    runtime.shells.append(shell.setOS(runtime))

    web_server = SmallTask(2, web_server_task, name="web_server", args=(state,))
    idle_watchdog = SmallTask(2, idle_watchdog_task, name="idle_watchdog", args=(state,))
    shell_stdin = shell.make_task(
        priority=3,
        name="shell_stdin",
        is_watcher=True,
        poll_interval=0.1,
        banner_text=(
            "\nInteractive scrum poker shell enabled.\n"
            "Commands: session open, session close, session status, clear everyone, ps, stat <pid>, toggle, help\n"
        ),
        force_output=True,
    )

    runtime.fork([web_server, idle_watchdog, shell_stdin])

    try:
        runtime.startOS()

        if web_server.exception is not None and not isinstance(web_server.exception, TaskCancelledError):
            raise web_server.exception

        if shell_stdin.exception is not None:
            raise shell_stdin.exception
    finally:
        _shutdown_runtime(runtime, state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nscrum poker demo stopped")
