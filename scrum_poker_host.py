"""Shared listener, public setup flow, and dynamic room router."""

import json
import uuid

from SmallOS.SmallPackage import SmallTask

from scrum_poker_app import ScrumPokerApp
from scrum_poker_core import (
    EPHEMERAL_JOIN_LIMIT,
    EPHEMERAL_ROOM_LIMIT,
    EPHEMERAL_ROOM_TTL_SECONDS,
    LISTEN_BACKLOG,
    PREMIUM_JOIN_LIMIT,
    _close_socket_quietly,
    _connected_count,
    _get_host,
    _get_port,
    _http_response,
    _json_bytes,
    _normalize_admin_passphrase,
    _now_ms,
    _parse_http_request,
    _parse_request_target,
    _read_exact,
    _read_request_head,
    _read_static_asset,
    _room_has_been_empty_too_long,
    _room_has_expired,
    _send_all,
)


HOST_STATIC_ROUTES = {
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/setup_room.js": ("setup_room.js", "application/javascript; charset=utf-8"),
}
ROOT_HTML_ROUTES = {
    "/": "landing.html",
    "/setupRoom": "setup_room.html",
}
RESERVED_SEGMENTS = {"", "api", "healthz", "legalease", "setupRoom", "static"}


class ScrumPokerHost:
    """Shared listener and router for premium and ephemeral scrum poker rooms."""

    def __init__(self, fixed_rooms, host=None, port=None):
        self.fixed_rooms = list(fixed_rooms)
        self.fixed_rooms_by_id = {room.app_id: room for room in self.fixed_rooms}
        self.ephemeral_rooms = {}
        self.host = _get_host() if host is None else host
        self.port = _get_port() if port is None else port
        self.listener = None
        self.sweeper_task = None
        self.runtime = getattr(self.fixed_rooms[0], "runtime", None) if self.fixed_rooms else None
        for room in self.fixed_rooms:
            room.host = self

    def to_task(self):
        """Return the shared listener task."""
        return SmallTask(2, self.web_server_task, name="scrum_poker_host")

    def all_rooms(self):
        """Return every currently active playable room."""
        dynamic_rooms = sorted(self.ephemeral_rooms.values(), key=lambda room: room.created_ms)
        return list(self.fixed_rooms) + dynamic_rooms

    def get_room(self, room_id):
        """Return one room by id."""
        return self.fixed_rooms_by_id.get(room_id) or self.ephemeral_rooms.get(room_id)

    def total_connected_count(self):
        """Return total live websocket transports across all rooms."""
        return sum(_connected_count(room.state) for room in self.all_rooms())

    def total_connection_records(self):
        """Return total stored connection records across all rooms."""
        return sum(len(room.state.get("connections", {})) for room in self.all_rooms())

    def active_ephemeral_count(self):
        """Return the number of active ephemeral rooms."""
        return len(self.ephemeral_rooms)

    def _first_path_segment(self, path):
        stripped = str(path or "/").strip("/")
        if not stripped:
            return ""
        return stripped.split("/", 1)[0]

    def resolve_room(self, path):
        """Resolve the playable room for one request path."""
        for room in self.fixed_rooms:
            if room.matches_path(path):
                return room

        first_segment = self._first_path_segment(path)
        room = self.ephemeral_rooms.get(first_segment)
        if room is not None and room.matches_path(path):
            return room
        return None

    def route_summary(self):
        """Return one compact route summary string for startup output."""
        routes = ["/", "/setupRoom", "/api/rooms", "/healthz"]
        for room in self.all_rooms():
            routes.extend(room.route_list())
        return " ".join(routes)

    def _static_response(self, path):
        """Serve one host-level static asset."""
        asset = HOST_STATIC_ROUTES.get(path)
        if asset is None:
            return None

        filename, content_type = asset
        try:
            body = _read_static_asset(filename)
        except OSError:
            return _http_response(500, "static asset unavailable\n")
        return _http_response(200, body, content_type)

    def landing_response(self):
        """Return the public landing page."""
        try:
            body = _read_static_asset(ROOT_HTML_ROUTES["/"])
        except OSError:
            return _http_response(500, "static asset unavailable\n")
        return _http_response(200, body, "text/html; charset=utf-8")

    def setup_room_response(self):
        """Return the room setup page."""
        try:
            body = _read_static_asset(ROOT_HTML_ROUTES["/setupRoom"])
        except OSError:
            return _http_response(500, "static asset unavailable\n")
        return _http_response(200, body, "text/html; charset=utf-8")

    def room_unavailable_response(self):
        """Return a friendly room-unavailable page."""
        try:
            body = _read_static_asset("room_unavailable.html")
        except OSError:
            return _http_response(404, "room unavailable\n")
        return _http_response(404, body, "text/html; charset=utf-8")

    def _new_room_id(self):
        """Generate one non-reserved room GUID."""
        while True:
            room_id = str(uuid.uuid4())
            if room_id not in self.ephemeral_rooms and room_id not in RESERVED_SEGMENTS:
                return room_id

    def create_ephemeral_room(self, admin_passphrase, task=None):
        """Create and register one new ephemeral room."""
        normalized_passphrase = _normalize_admin_passphrase(admin_passphrase)
        if normalized_passphrase is None:
            raise ValueError("room admin password must be between 1 and 128 characters")
        if self.active_ephemeral_count() >= EPHEMERAL_ROOM_LIMIT:
            raise RuntimeError("ephemeral room capacity is full")

        now_ms = self.runtime.kernel.scheduler_now_ms()
        room_id = self._new_room_id()
        creator_claim_token = uuid.uuid4().hex + uuid.uuid4().hex
        room = ScrumPokerApp(
            room_id,
            "/{}".format(room_id),
            self.runtime,
            title="Sprint Poker",
            label=room_id[:8],
            host=self,
            room_kind="ephemeral",
            join_limit=EPHEMERAL_JOIN_LIMIT,
            admin_auth_mode="room",
            room_admin_passphrase=normalized_passphrase,
            created_ms=now_ms,
            expires_at_ms=now_ms + (EPHEMERAL_ROOM_TTL_SECONDS * 1000),
            creator_claim_token=creator_claim_token,
        )
        self.ephemeral_rooms[room_id] = room
        if task is not None:
            room.room_task = task.spawn(
                room.to_task(),
                priority=2,
            )
        return room, creator_claim_token

    def create_room_api_response(self, body_bytes, task=None):
        """Handle one room-creation API request."""
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _http_response(400, "invalid JSON body\n")

        admin_passphrase = payload.get("admin_passphrase")
        try:
            room, creator_claim_token = self.create_ephemeral_room(admin_passphrase, task=task)
        except ValueError as exc:
            return _http_response(400, str(exc) + "\n")
        except RuntimeError:
            return _http_response(503, "all public rooms are currently in use\n")

        response_body = {
            "creator_claim_token": creator_claim_token,
            "expires_at_ms": room.expires_at_ms,
            "room_id": room.room_id,
            "room_url": room.base_path,
        }
        return _http_response(
            201,
            _json_bytes(response_body),
            "application/json; charset=utf-8",
        )

    def destroy_ephemeral_room(self, room_id, reason="expired", current_task=None):
        """Fully remove one ephemeral room and all of its in-memory state."""
        room = self.ephemeral_rooms.get(room_id)
        if room is None:
            return False
        if room.destroyed:
            self.ephemeral_rooms.pop(room_id, None)
            return False

        room.destroyed = True
        room.state["destroyed"] = True
        room.state["destroy_reason"] = reason
        room.shutdown()

        room_task = room.room_task
        if room_task is not None and room_task is not current_task and not room_task.done:
            try:
                self.runtime.cancel_task(room_task)
            except Exception:
                pass
        room.room_task = None

        room.state["connections"].clear()
        room.state["connections_by_token"].clear()
        room.state["creator_claim_token"] = None
        room.state["creator_claim_used"] = True
        room.state["room_admin_passphrase"] = None
        self.ephemeral_rooms.pop(room_id, None)
        return True

    def expire_ephemeral_rooms(self, now_ms=None, current_task=None):
        """Destroy every expired ephemeral room."""
        if now_ms is None:
            now_ms = self.runtime.kernel.scheduler_now_ms()

        removed = 0
        for room_id, room in list(self.ephemeral_rooms.items()):
            reason = None
            if room.destroyed or _room_has_expired(room.state, now_ms=now_ms):
                reason = "expired"
            elif _room_has_been_empty_too_long(room.state, now_ms=now_ms):
                reason = "empty-timeout"

            if reason is not None:
                if self.destroy_ephemeral_room(room_id, reason=reason, current_task=current_task):
                    removed += 1
        return removed

    async def room_sweeper_loop(self, task):
        """Destroy expired ephemeral rooms even when no room is active."""
        while True:
            await task.sleep(30)
            self.expire_ephemeral_rooms(current_task=task)

    def shutdown(self):
        """Release the shared listener socket and all ephemeral rooms."""
        kernel = getattr(self.runtime, "kernel", None)
        _close_socket_quietly(kernel, self.listener)
        self.listener = None

        if self.sweeper_task is not None and not self.sweeper_task.done:
            try:
                self.runtime.cancel_task(self.sweeper_task)
            except Exception:
                pass
        self.sweeper_task = None

        for room_id in list(self.ephemeral_rooms.keys()):
            self.destroy_ephemeral_room(room_id, reason="shutdown")

    async def web_client_handler(self, task, client_sock, client_addr):
        """Handle one inbound HTTP/WebSocket client."""
        kernel = task.OS.kernel

        try:
            try:
                raw_request = await _read_request_head(task, client_sock)
            except TimeoutError:
                return
            except ValueError:
                await _send_all(task, client_sock, _http_response(413, "request header too large\n"))
                return

            header_bytes, _, initial_body = raw_request.partition(b"\r\n\r\n")
            method, target, headers = _parse_http_request(header_bytes)
            if method is None:
                return

            path, _ = _parse_request_target(target)

            if method == "GET" and path == "/healthz":
                await _send_all(task, client_sock, _http_response(200, "ok\n"))
                return

            if method == "GET" and path == "/":
                await _send_all(task, client_sock, self.landing_response())
                return

            if method == "GET" and path == "/setupRoom":
                await _send_all(task, client_sock, self.setup_room_response())
                return

            static_payload = self._static_response(path)
            if static_payload is not None:
                await _send_all(task, client_sock, static_payload)
                return

            if method == "POST" and path == "/api/rooms":
                content_length = headers.get("content-length", "0").strip()
                try:
                    expected_length = int(content_length)
                except (TypeError, ValueError):
                    expected_length = 0
                if expected_length < len(initial_body):
                    body_bytes = initial_body[:expected_length]
                else:
                    remaining = expected_length - len(initial_body)
                    extra_bytes = b""
                    if remaining > 0:
                        extra_bytes = await _read_exact(task, client_sock, remaining)
                    body_bytes = initial_body + extra_bytes
                await _send_all(task, client_sock, self.create_room_api_response(body_bytes, task=task))
                return

            room = self.resolve_room(path)
            if room is None:
                if method == "GET":
                    first_segment = self._first_path_segment(path)
                    if first_segment not in RESERVED_SEGMENTS and "/" not in path.strip("/"):
                        await _send_all(task, client_sock, self.room_unavailable_response())
                        return
                await _send_all(task, client_sock, _http_response(404, "route not found\n"))
                return

            is_websocket = (
                method == "GET"
                and path == room.ws_path
                and "upgrade" in headers.get("connection", "").lower()
                and headers.get("upgrade", "").lower() == "websocket"
            )
            if is_websocket:
                headers = dict(headers)
                headers[":target"] = target
                await room.websocket_session(task, client_sock, client_addr, headers)
                return

            payload = room.build_http_response(method, target, path)
            await _send_all(task, client_sock, payload)
        finally:
            _close_socket_quietly(kernel, client_sock)

    async def web_server_task(self, task):
        """Run the cooperative shared HTTP and WebSocket listener."""
        kernel = task.OS.kernel
        if kernel is None:
            raise RuntimeError("web_server_task requires a kernel-enabled runtime.")

        self.sweeper_task = task.spawn(self.room_sweeper_loop, priority=1, name="room_sweeper")

        address_info = kernel.resolve_address(self.host, self.port)
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
        self.listener = listener

        task.OS.print("smallOS scrum poker running on http://{}:{}/\n".format(self.host, self.port))
        task.OS.print("routes: {}\n".format(self.route_summary()))
        task.OS.print("Open the shell and use: poker rooms | poker legalease session open | poker <guid> session open\n")

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

                self.expire_ephemeral_rooms()
                kernel.socket_setblocking(client_sock, False)
                task.spawn(
                    self.web_client_handler,
                    priority=max(1, task.priority - 1),
                    name="http_client",
                    args=(client_sock, client_addr),
                )
                await task.yield_now()
        finally:
            if self.listener is listener:
                self.listener = None
            _close_socket_quietly(kernel, listener)


__all__ = ["ScrumPokerHost", "EPHEMERAL_ROOM_LIMIT", "HOST_STATIC_ROUTES", "RESERVED_SEGMENTS"]
