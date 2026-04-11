"""Shared helpers and state utilities for the SmallOS scrum poker app."""

from collections import deque
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import SmallOS
from SmallOS.SmallPackage import Unix
from SmallOS.SmallPackage.SmallConfig import SmallOSConfig
from SmallOS.SmallPackage.SmallOS import SmallOS as SmallOSRuntime


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8082
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
MAX_PENDING_AUX_MESSAGES = 8  # non-state messages queued for one connection before it is disconnected
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
    "40",
    "60",
    "100",
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
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        426: "Upgrade Required",
        500: "Internal Server Error",
        503: "Service Unavailable",
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


def _get_host():
    """Return the configured bind host, defaulting to all interfaces."""
    return str(os.environ.get("HOST", DEFAULT_HOST)).strip() or DEFAULT_HOST


def _get_port():
    """Return the configured listen port, falling back to the default."""
    raw_value = str(os.environ.get("PORT", DEFAULT_PORT)).strip()
    try:
        port = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    if not 0 < port < 65536:
        return DEFAULT_PORT
    return port


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
        "state_version": 0,
        "shared_state_cache_version": None,
        "shared_state_cache": None,
        "shared_state_message_cache_version": None,
        "shared_state_message_cache": None,
        "stats": {
            "broadcasts": 0,
            "shared_state_rebuilds": 0,
            "dropped_state_updates": 0,
            "queue_disconnects": 0,
        },
    }


def _build_runtime():
    """Create one SmallOS runtime from the bundled project config."""
    config = SmallOSConfig.from_json_file(CONFIG_PATH)
    return SmallOSRuntime(config=config).setKernel(Unix())


def _normalize_base_path(base_path):
    """Normalize one mounted application base path."""
    text = str(base_path or "/").strip()
    if not text or text == "/":
        return "/"
    return "/" + text.strip("/")


def _build_route(base_path, suffix=""):
    """Join one base path with one route suffix."""
    normalized_base = _normalize_base_path(base_path)
    normalized_suffix = str(suffix or "").strip().lstrip("/")
    if normalized_base == "/":
        return "/" if not normalized_suffix else "/" + normalized_suffix
    return normalized_base if not normalized_suffix else normalized_base + "/" + normalized_suffix


def _path_matches_base(path, base_path):
    """Return whether one request path belongs to a mounted app base."""
    normalized_base = _normalize_base_path(base_path)
    if normalized_base == "/":
        return True
    return path == normalized_base or path.startswith(normalized_base + "/")


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


def _shutdown_state(runtime, state):
    """Close all sockets associated with one scrum poker state bucket."""
    if runtime is None or state is None:
        return

    kernel = getattr(runtime, "kernel", None)
    _close_socket_quietly(kernel, state.get("listener"))
    state["listener"] = None

    for connection in list(state.get("connections", {}).values()):
        _ensure_connection_queue_fields(connection)
        connection["connected"] = False
        connection["shutdown_after_drain"] = True
        connection["websocket"] = None
        connection["pending_shared_state_text"] = None
        connection["pending_viewer_state_text"] = None
        connection["pending_messages"].clear()
        _close_socket_quietly(kernel, connection.get("socket"))
        connection["socket"] = None


def _shutdown_runtime(runtime, host=None, apps=None):
    """Cancel active tasks and close network sockets so the port is released."""
    if runtime is None:
        return

    if host is not None and hasattr(host, "shutdown"):
        host.shutdown()
    elif isinstance(host, dict):
        _shutdown_state(runtime, host)

    for app in list(apps or ()):
        if hasattr(app, "shutdown"):
            app.shutdown()

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


def _mark_state_dirty(state):
    """Invalidate cached shared state after one meaningful board change."""
    state["state_version"] = int(state.get("state_version", 0)) + 1
    state["shared_state_cache_version"] = None
    state["shared_state_cache"] = None
    state["shared_state_message_cache_version"] = None
    state["shared_state_message_cache"] = None
    return state["state_version"]


def _mark_connection_viewer_dirty(connection):
    """Mark a connection's viewer-specific state as needing a refresh."""
    connection["viewer_state_dirty"] = True


def _ensure_connection_queue_fields(connection):
    """Backfill queue-related connection fields for newer bounded queues."""
    if connection is None:
        return

    if "pending_messages" not in connection or connection["pending_messages"] is None:
        legacy_messages = connection.get("outbox", [])
        connection["pending_messages"] = deque(legacy_messages)
    elif not isinstance(connection["pending_messages"], deque):
        connection["pending_messages"] = deque(connection["pending_messages"])

    if "pending_shared_state_text" not in connection:
        connection["pending_shared_state_text"] = None
    if "pending_viewer_state_text" not in connection:
        connection["pending_viewer_state_text"] = None
    if "dropped_state_updates" not in connection:
        connection["dropped_state_updates"] = 0
    if "disconnect_reason" not in connection:
        connection["disconnect_reason"] = None
    if "viewer_state_dirty" not in connection:
        connection["viewer_state_dirty"] = True
    if "last_msg_ms" not in connection:
        connection["last_msg_ms"] = 0
    if "rate_burst" not in connection:
        connection["rate_burst"] = 0


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
    removed_any = False

    for connection in list(state.get("connections", {}).values()):
        if connection.get("closed"):
            _remove_connection_record(state, connection)
            removed_any = True
            continue

        deadline = connection.get("resume_deadline_ms")
        if connection.get("connected", True) or deadline is None:
            continue
        if now_ms >= deadline:
            _remove_connection_record(state, connection)
            removed_any = True

    if removed_any:
        _mark_state_dirty(state)


def _connections_over_hard_cap(state):
    """Return True when the total connection record count is at the hard cap."""
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
        "disconnect_reason": None,
        "dropped_state_updates": 0,
        "is_admin": False,
        "last_msg_ms": 0,
        "name": None,
        "pending_messages": deque(),
        "pending_shared_state_text": None,
        "pending_viewer_state_text": None,
        "rate_burst": 0,
        "resume_deadline_ms": None,
        "session_task": None,
        "session_token": session_token or _new_session_token(),
        "shutdown_after_drain": False,
        "socket": None,
        "state": state,
        "tab_id": tab_id,
        "transport_id": 0,
        "viewer_state_dirty": True,
        "vote": None,
        "websocket": None,
        "writer_task": None,
    }
    state["connections"][client_id] = record
    state["connections_by_token"][record["session_token"]] = record
    return record


def _attach_connection_transport(task, state, connection, sock, client_addr):
    """Attach the current websocket transport to one participant record."""
    _ensure_connection_queue_fields(connection)
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
    connection["disconnect_reason"] = None
    connection["pending_messages"].clear()
    connection["pending_shared_state_text"] = None
    connection["pending_viewer_state_text"] = None
    connection["rate_burst"] = 0
    connection["resume_deadline_ms"] = None
    connection["session_task"] = task
    connection["shutdown_after_drain"] = False
    connection["socket"] = sock
    connection["state"] = state
    connection["transport_id"] = int(connection.get("transport_id", 0)) + 1
    connection["websocket"] = None
    _mark_connection_viewer_dirty(connection)
    _mark_state_dirty(state)

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


def _build_viewer_state(state, viewer_id=None):
    """Build the viewer-specific payload for one connection."""
    viewer = state.get("connections", {}).get(viewer_id)
    if viewer is None or viewer.get("closed"):
        return None
    return {
        "client_id": viewer["client_id"],
        "is_admin": bool(viewer.get("is_admin")),
        "name": viewer.get("name"),
        "session_token": viewer.get("session_token"),
        "vote": viewer.get("vote"),
    }


def _build_shared_state(state):
    """Build or reuse the cached room-wide state snapshot."""
    _expire_stale_connections(state)
    version = state.get("state_version", 0)
    if state.get("shared_state_cache_version") == version and state.get("shared_state_cache") is not None:
        return state["shared_state_cache"]

    participants = []
    votes_visible = bool(state["votes_visible"])

    for connection in _iter_participants(state):
        vote_value = connection.get("vote") if votes_visible else None
        participants.append(
            {
                "client_id": connection["client_id"],
                "has_voted": connection.get("vote") is not None,
                "is_admin": bool(connection.get("is_admin")),
                "is_connected": bool(connection.get("connected", True)),
                "name": connection["name"],
                "vote": vote_value,
            }
        )

    shared_state = {
        "admin_auth_enabled": _admin_auth_enabled(),
        "connected_count": _connected_count(state),
        "participant_count": len(participants),
        "participants": participants,
        "server_time": datetime.now(timezone.utc).strftime("%H:%M:%SZ"),
        "session_open": bool(state["session_open"]),
        "state_version": version,
        "vote_options": list(ALLOWED_VOTES),
        "votes_visible": votes_visible,
    }
    state["shared_state_cache_version"] = version
    state["shared_state_cache"] = shared_state
    state["stats"]["shared_state_rebuilds"] += 1
    return shared_state


def _build_public_state(state, viewer_id=None):
    """Build the browser-visible session snapshot for one connection."""
    shared_state = dict(_build_shared_state(state))
    shared_state["me"] = _build_viewer_state(state, viewer_id)
    return shared_state


def _build_shared_state_message(state):
    """Encode the room-wide websocket payload once per state version."""
    shared_state = _build_shared_state(state)
    version = shared_state["state_version"]
    if state.get("shared_state_message_cache_version") == version and state.get("shared_state_message_cache") is not None:
        return state["shared_state_message_cache"]

    message = _json_text({"clear_error": True, "state": shared_state, "type": "state"})
    state["shared_state_message_cache_version"] = version
    state["shared_state_message_cache"] = message
    return message


def _build_viewer_state_message(connection):
    """Encode one viewer-specific websocket payload."""
    state = connection.get("state")
    viewer_state = _build_viewer_state(state, connection.get("client_id")) if state is not None else None
    return _json_text({"me": viewer_state, "type": "viewer_state"})


def _build_state_message(state, viewer_id=None):
    """Encode one compatibility websocket payload with combined state."""
    return _json_text(
        {
            "clear_error": True,
            "state": _build_public_state(state, viewer_id),
            "type": "state",
        }
    )


def _signal_writer(connection):
    """Wake a connection writer task when queued work is available."""
    writer_task = connection.get("writer_task")
    if writer_task is not None and not writer_task.done:
        writer_task.acceptSignal(OUTBOX_SIGNAL)


def _request_backpressure_disconnect(state, connection, reason="client fell behind"):
    """Disconnect one slow client after exhausting state coalescing."""
    if state is not None:
        state["stats"]["queue_disconnects"] += 1
    connection["disconnect_reason"] = reason
    _kick_connection(state, connection)


def _queue_state_update(connection, shared_state_text, viewer_state_text=None):
    """Coalesce the latest state payload for one connection."""
    _ensure_connection_queue_fields(connection)
    if connection.get("closed") or not connection.get("connected", True):
        return

    state = connection.get("state")
    if connection.get("pending_shared_state_text") is not None or (
        viewer_state_text is not None and connection.get("pending_viewer_state_text") is not None
    ):
        connection["dropped_state_updates"] = int(connection.get("dropped_state_updates", 0)) + 1
        if state is not None:
            state["stats"]["dropped_state_updates"] += 1

    connection["pending_shared_state_text"] = shared_state_text
    if viewer_state_text is not None:
        connection["pending_viewer_state_text"] = viewer_state_text
    _signal_writer(connection)


def _queue_connection_text(connection, text):
    """Append one non-state message to a bounded queue and wake its writer."""
    _ensure_connection_queue_fields(connection)
    if connection.get("closed") or not connection.get("connected", True):
        return

    state = connection.get("state")
    pending_messages = connection["pending_messages"]
    if len(pending_messages) >= MAX_PENDING_AUX_MESSAGES:
        _request_backpressure_disconnect(state, connection)
        return

    pending_messages.append(text)
    _signal_writer(connection)


def _broadcast_state(state):
    """Queue the latest shared state, plus dirty viewer state, for every live client."""
    _expire_stale_connections(state)
    state["stats"]["broadcasts"] += 1
    shared_state_text = _build_shared_state_message(state)

    for connection in list(state["connections"].values()):
        if connection.get("closed") or not connection.get("connected", True):
            continue
        viewer_state_text = None
        if connection.get("viewer_state_dirty"):
            viewer_state_text = _build_viewer_state_message(connection)
            connection["viewer_state_dirty"] = False
        _queue_state_update(connection, shared_state_text, viewer_state_text=viewer_state_text)


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
    changed = bool(state.get("votes_visible"))
    for connection in state["connections"].values():
        if connection.get("vote") is not None:
            changed = True
            connection["vote"] = None
            _mark_connection_viewer_dirty(connection)
    state["votes_visible"] = False
    if changed:
        _mark_state_dirty(state)
    return changed


def _set_session_open(state, is_open):
    """Update whether new participants may join the session."""
    desired_state = bool(is_open)
    if state.get("session_open") == desired_state:
        return False
    state["session_open"] = desired_state
    _mark_state_dirty(state)
    return True


def _parse_client_id(value):
    """Parse one client identifier from a websocket payload."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kick_connection(state, target_connection, mark_dirty=True):
    """Disconnect one websocket client and remove it from the shared state."""
    if target_connection is None:
        return

    _ensure_connection_queue_fields(target_connection)
    os_ref = state.get("os") if state is not None else None
    session_task = target_connection.get("session_task")
    if session_task is not None and not session_task.done and os_ref is not None:
        try:
            os_ref.cancel_task(session_task)
        except Exception:
            pass

    target_connection["connected"] = False
    target_connection["closed"] = True
    target_connection["shutdown_after_drain"] = True
    target_connection["pending_shared_state_text"] = None
    target_connection["pending_viewer_state_text"] = None
    target_connection["pending_messages"].clear()
    target_connection["websocket"] = None
    target_connection["session_task"] = None
    if state is not None:
        _remove_connection_record(state, target_connection)

    writer_task = target_connection.get("writer_task")
    if writer_task is not None and not writer_task.done:
        if os_ref is not None:
            try:
                os_ref.cancel_task(writer_task)
            except Exception:
                _signal_writer(target_connection)
        else:
            _signal_writer(target_connection)

    socket_obj = target_connection.get("socket")
    kernel = state.get("kernel") if state is not None else None
    target_connection["socket"] = None
    if socket_obj is not None:
        try:
            if kernel is not None:
                kernel.socket_close(socket_obj)
            else:
                socket_obj.close()
        except Exception:
            pass

    if state is not None and mark_dirty:
        _mark_state_dirty(state)


def _clear_everyone(state):
    """Kick every participant off the board and close the session."""
    participants = list(state.get("connections", {}).values())
    had_state = bool(participants) or bool(state.get("session_open")) or bool(state.get("votes_visible"))
    for connection in participants:
        _kick_connection(state, connection, mark_dirty=False)
    state["session_open"] = False
    state["votes_visible"] = False
    _touch_activity(state)
    if had_state:
        _mark_state_dirty(state)
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
        _mark_connection_viewer_dirty(connection)
        _mark_state_dirty(state)
        _touch_activity(state)
        return None

    if message_type == "become_admin":
        expected = _get_admin_passphrase()
        if not expected:
            return "admin access is not configured on this server"
        failures = connection.get("admin_failures", 0)
        if failures >= MAX_ADMIN_FAILURES:
            return "too many failed attempts — reconnect to try again"
        supplied = str("" if payload.get("passphrase") is None else payload.get("passphrase"))
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            connection["admin_failures"] = failures + 1
            return "incorrect admin passphrase"
        connection["admin_failures"] = 0
        connection["is_admin"] = True
        _mark_connection_viewer_dirty(connection)
        _mark_state_dirty(state)
        _queue_notice(connection, "Admin access granted.", kind="success")
        return None

    if message_type == "vote":
        if not connection.get("name"):
            return "join the session before voting"
        vote = _normalize_vote(payload.get("value"))
        if vote is None:
            return "unsupported vote value"
        connection["vote"] = vote
        _mark_connection_viewer_dirty(connection)
        _mark_state_dirty(state)
        _touch_activity(state)
        return None

    if message_type == "toggle_votes":
        if not connection.get("name") and not connection.get("is_admin"):
            return "join the session before revealing votes"
        state["votes_visible"] = not state["votes_visible"]
        _mark_state_dirty(state)
        _touch_activity(state)
        return None

    if message_type == "show_votes":
        if not connection.get("name") and not connection.get("is_admin"):
            return "join the session before revealing votes"
        if not state.get("votes_visible"):
            state["votes_visible"] = True
            _mark_state_dirty(state)
        _touch_activity(state)
        return None

    if message_type == "hide_votes":
        if not connection.get("name") and not connection.get("is_admin"):
            return "join the session before hiding votes"
        if state.get("votes_visible"):
            state["votes_visible"] = False
            _mark_state_dirty(state)
        _touch_activity(state)
        return None

    if message_type == "clear_votes":
        if not connection.get("name") and not connection.get("is_admin"):
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
    """Serialize writes for one websocket client connection with bounded queues."""
    websocket = connection["websocket"]
    _ensure_connection_queue_fields(connection)

    try:
        while True:
            while connection.get("pending_shared_state_text") is not None:
                message = connection["pending_shared_state_text"]
                connection["pending_shared_state_text"] = None
                await websocket.send_text(message)

            while connection.get("pending_viewer_state_text") is not None:
                message = connection["pending_viewer_state_text"]
                connection["pending_viewer_state_text"] = None
                await websocket.send_text(message)

            while connection["pending_messages"]:
                message = connection["pending_messages"].popleft()
                await websocket.send_text(message)

            if connection.get("shutdown_after_drain"):
                reason = connection.get("disconnect_reason") or ""
                if getattr(websocket, "connected", False):
                    try:
                        await websocket.send_close(code=1001, reason=reason[:120])
                    except Exception:
                        pass
                return "writer stopped"

            await task.wait_signal(OUTBOX_SIGNAL)
    except Exception:
        if connection.get("closed") or connection.get("shutdown_after_drain"):
            return "writer stopped"
        raise


__all__ = [
    "ALLOWED_VOTES",
    "CONFIG_PATH",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DOTENV_PATH",
    "HEADER_READ_TIMEOUT_SECONDS",
    "IDLE_TIMEOUT_SECONDS",
    "LISTEN_BACKLOG",
    "MAX_ADMIN_FAILURES",
    "MAX_CONNECTIONS",
    "MAX_FRAME_SIZE",
    "MAX_MESSAGE_SIZE",
    "MAX_PARTICIPANTS",
    "MAX_PENDING_AUX_MESSAGES",
    "MESSAGE_RATE_BURST",
    "MESSAGE_RATE_LIMIT_MS",
    "OUTBOX_SIGNAL",
    "PROJECT_ROOT",
    "REQUEST_HEADER_LIMIT",
    "SESSION_RESUME_GRACE_SECONDS",
    "SMALLOS_ROOT",
    "STATIC_ASSETS",
    "STATIC_DIR",
    "_admin_auth_enabled",
    "_apply_client_message",
    "_attach_connection_transport",
    "_broadcast_state",
    "_build_public_state",
    "_build_route",
    "_build_runtime",
    "_build_shared_state",
    "_build_shared_state_message",
    "_build_state_message",
    "_build_viewer_state",
    "_build_viewer_state_message",
    "_clear_everyone",
    "_clear_votes",
    "_close_socket_quietly",
    "_connected_count",
    "_connections_over_hard_cap",
    "_ensure_connection_queue_fields",
    "_expire_stale_connections",
    "_get_admin_passphrase",
    "_get_host",
    "_get_port",
    "_http_reason",
    "_http_response",
    "_iter_participants",
    "_joined_count",
    "_json_bytes",
    "_json_text",
    "_kick_connection",
    "_load_dotenv_file",
    "_make_connection_record",
    "_mark_connection_viewer_dirty",
    "_mark_state_dirty",
    "_new_session_token",
    "_new_state",
    "_normalize_base_path",
    "_normalize_name",
    "_normalize_session_token",
    "_normalize_tab_id",
    "_normalize_vote",
    "_now_ms",
    "_parse_client_id",
    "_parse_dotenv_assignment",
    "_parse_http_request",
    "_parse_request_target",
    "_path_matches_base",
    "_queue_connection_text",
    "_queue_error",
    "_queue_notice",
    "_queue_state_update",
    "_read_exact",
    "_read_request_head",
    "_read_static_asset",
    "_remove_connection_record",
    "_request_backpressure_disconnect",
    "_resolve_connection_for_socket",
    "_send_all",
    "_set_session_open",
    "_shutdown_runtime",
    "_shutdown_state",
    "_signal_writer",
    "_static_asset_response",
    "_strip_dotenv_comment",
    "_touch_activity",
    "websocket_writer_task",
]
