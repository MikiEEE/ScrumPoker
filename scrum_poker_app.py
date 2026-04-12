"""Mounted scrum poker room implementation."""

import html
import json
import os

from SmallOS.SmallPackage import SmallTask
from SmallOS.SmallPackage.SmallErrors import TaskCancelledError

from smallos_websocket_server import SmallWebSocketServerConnection, WebSocketServerProtocolError
from scrum_poker_core import (
    EPHEMERAL_ROOM_TTL_SECONDS,
    IDLE_TIMEOUT_SECONDS,
    MAX_FRAME_SIZE,
    MAX_MESSAGE_SIZE,
    MAX_PARTICIPANTS,
    MESSAGE_RATE_BURST,
    MESSAGE_RATE_LIMIT_MS,
    OUTBOX_SIGNAL,
    SESSION_RESUME_GRACE_SECONDS,
    _apply_client_message,
    _broadcast_state,
    _build_public_state,
    _build_route,
    _clear_everyone,
    _connected_count,
    _connections_over_hard_cap,
    _expire_stale_connections,
    _get_max_connections,
    _http_response,
    _json_bytes,
    _new_state,
    _normalize_base_path,
    _now_ms,
    _parse_request_target,
    _path_matches_base,
    _queue_error,
    _read_static_asset,
    _resolve_connection_for_socket,
    _room_has_expired,
    _send_all,
    _shutdown_state,
)


async def idle_watchdog_task(task, app):
    """Handle idle cleanup for one room."""
    check_interval = 60
    state = app.state
    while True:
        await task.sleep(check_interval)
        if state.get("destroyed"):
            return

        idle_ms = _now_ms(state) - state.get("last_activity_ms", _now_ms(state))
        if idle_ms < IDLE_TIMEOUT_SECONDS * 1000:
            continue

        if app.room_kind == "ephemeral":
            task.OS.print("[scrum-poker:{}] idle timeout reached - destroying ephemeral room\n".format(app.app_id))
            if app.host is not None:
                app.host.destroy_ephemeral_room(app.room_id, reason="idle-timeout", current_task=task)
            return

        task.OS.print("[scrum-poker:{}] idle timeout reached - kicking all participants\n".format(app.app_id))
        _clear_everyone(state)
        app.broadcast_state()
        task.OS.print("[scrum-poker:{}] session closed after idle timeout\n".format(app.app_id))


class ScrumPokerApp:
    """One isolated scrum poker room mounted at a specific base path."""

    def __init__(
        self,
        app_id,
        base_path,
        runtime,
        title=None,
        label=None,
        host=None,
        room_kind="premium",
        join_limit=MAX_PARTICIPANTS,
        admin_auth_mode="premium",
        room_admin_passphrase=None,
        created_ms=None,
        expires_at_ms=None,
        creator_claim_token=None,
    ):
        self.app_id = str(app_id).strip() or "room"
        self.room_id = self.app_id
        self.base_path = _normalize_base_path(base_path)
        self.runtime = runtime
        self.host = host
        self.room_kind = room_kind
        self.join_limit = int(join_limit)
        self.admin_auth_mode = admin_auth_mode
        self.room_admin_passphrase = room_admin_passphrase
        self.created_ms = created_ms if created_ms is not None else runtime.kernel.scheduler_now_ms()
        self.expires_at_ms = expires_at_ms
        self.creator_claim_token = creator_claim_token
        self.title = title or "Sprint Poker"
        self.label = label or ""
        self.page_title = self.title if not self.label else "{} · {}".format(self.title, self.label)
        self.storage_key_prefix = "smallos-scrum-poker-{}".format(self.app_id)
        self.room_task = None
        self.destroyed = False
        self.state = _new_state(
            runtime,
            room_id=self.room_id,
            room_kind=self.room_kind,
            join_limit=self.join_limit,
            admin_auth_mode=self.admin_auth_mode,
            room_admin_passphrase=self.room_admin_passphrase,
            created_ms=self.created_ms,
            expires_at_ms=self.expires_at_ms,
            label=self.label,
            creator_claim_token=self.creator_claim_token,
        )
        self.state["session_open"] = self.room_kind == "ephemeral"
        self.index_paths = {self.base_path}
        if self.base_path != "/":
            self.index_paths.add(self.base_path + "/")
        self.ws_path = _build_route(self.base_path, "ws")
        self.state_path = _build_route(self.base_path, "api/state")
        self.healthz_path = _build_route(self.base_path, "healthz")
        self.static_base_path = _build_route(self.base_path, "static")
        self.static_routes = {
            _build_route(self.base_path, "static/app.css"): ("app.css", "text/css; charset=utf-8"),
            _build_route(self.base_path, "static/app.js"): ("app.js", "application/javascript; charset=utf-8"),
        }

    def make_watchdog_task(self):
        """Return the per-room SmallOS idle-watchdog task."""
        return SmallTask(
            2,
            idle_watchdog_task,
            isWatcher=True,
            name="{}_room".format(self.app_id),
            args=(self,),
        )

    def shutdown(self):
        """Release all connections tracked by this room instance."""
        _shutdown_state(self.runtime, self.state)

    def matches_path(self, path):
        """Return whether the request path belongs to this mounted room."""
        return _path_matches_base(path, self.base_path)

    def route_list(self):
        """Return the public routes served by this room."""
        routes = sorted(self.index_paths)
        routes.extend(sorted(self.static_routes))
        routes.extend([self.ws_path, self.state_path, self.healthz_path])
        return routes

    def shell_open_command(self):
        """Return the namespaced shell command used to open this room."""
        return "poker {} session open".format(self.app_id)

    def shell_close_command(self):
        """Return the namespaced shell command used to close this room."""
        return "poker {} session close".format(self.app_id)

    def _render_index_html(self):
        """Render the HTML shell for one mounted room instance."""
        template = _read_static_asset("index.html").decode("utf-8")
        replacements = {
            "__APP_ID__": html.escape(self.app_id, quote=True),
            "__APP_LABEL__": html.escape(self.label, quote=True),
            "__BASE_PATH__": html.escape(self.base_path, quote=True),
            "__CREATOR_CLAIM_STORAGE_KEY__": html.escape(
                "smallos-scrum-poker-creator-claim-{}".format(self.room_id),
                quote=True,
            ),
            "__HEALTHZ_PATH__": html.escape(self.healthz_path, quote=True),
            "__INSTANCE_BADGE_HIDDEN__": "hidden" if not self.label else "",
            "__PAGE_TITLE__": html.escape(self.page_title, quote=True),
            "__ROOM_ID__": html.escape(self.room_id, quote=True),
            "__ROOM_KIND__": html.escape(self.room_kind, quote=True),
            "__SHELL_CLOSE_COMMAND__": html.escape(self.shell_close_command(), quote=True),
            "__SHELL_OPEN_COMMAND__": html.escape(self.shell_open_command(), quote=True),
            "__STATE_PATH__": html.escape(self.state_path, quote=True),
            "__STATIC_BASE_PATH__": html.escape(self.static_base_path, quote=True),
            "__STORAGE_KEY_PREFIX__": html.escape(self.storage_key_prefix, quote=True),
            "__WS_PATH__": html.escape(self.ws_path, quote=True),
        }
        for placeholder, value in replacements.items():
            template = template.replace(placeholder, value)
        return template

    def index_response(self):
        """Build the dynamic HTML response for this room."""
        try:
            body = self._render_index_html()
        except OSError:
            return _http_response(500, "static asset unavailable\n")
        return _http_response(200, body, "text/html; charset=utf-8")

    def static_asset_response(self, path):
        """Build one static asset response for this mounted room."""
        asset = self.static_routes.get(path)
        if asset is None:
            return None

        filename, content_type = asset
        try:
            body = _read_static_asset(filename)
        except OSError:
            return _http_response(500, "static asset unavailable\n")
        return _http_response(200, body, content_type)

    def unavailable_response(self):
        """Build a room-unavailable response for an expired or destroyed room."""
        try:
            body = _read_static_asset("room_unavailable.html")
        except OSError:
            return _http_response(404, "room unavailable\n")
        return _http_response(404, body, "text/html; charset=utf-8")

    def build_http_response(self, method, target, path):
        """Build one HTTP response for this room and request path."""
        if self.destroyed or self.state.get("destroyed") or _room_has_expired(self.state):
            return self.unavailable_response()

        if method != "GET":
            return _http_response(405, "only GET is supported\n")

        if path in self.index_paths:
            return self.index_response()

        static_payload = self.static_asset_response(path)
        if static_payload is not None:
            return static_payload

        if path == self.state_path:
            _, api_params = _parse_request_target(target)
            api_token = api_params.get("session_token")
            viewer = self.state.get("connections_by_token", {}).get(api_token) if api_token else None
            if viewer is None or viewer.get("closed"):
                return _http_response(403, "forbidden\n")
            return _http_response(
                200,
                _json_bytes(_build_public_state(self.state, viewer_id=viewer["client_id"])),
                "application/json; charset=utf-8",
            )

        if path == self.healthz_path:
            return _http_response(200, "ok\n")

        return _http_response(404, "route not found\n")

    async def websocket_session(self, task, sock, client_addr, headers):
        """Upgrade one HTTP client into a live websocket scrum poker session."""
        state = self.state
        if self.destroyed or state.get("destroyed") or _room_has_expired(state):
            await _send_all(task, sock, _http_response(404, "room unavailable\n"))
            return

        allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
        if allowed_origins_env:
            allowed_origins = {origin.strip().rstrip("/") for origin in allowed_origins_env.split(",") if origin.strip()}
            request_origin = headers.get("origin", "").strip().rstrip("/")
            if request_origin and request_origin not in allowed_origins:
                await _send_all(task, sock, _http_response(403, "origin not allowed\n"))
                return

        connected_limit = _get_max_connections()
        total_connections = _connected_count(state)
        total_records = len(state.get("connections", {}))
        if self.host is not None:
            total_connections = self.host.total_connected_count()
            total_records = self.host.total_connection_records()

        if total_connections >= connected_limit or total_records >= connected_limit * 2 or _connections_over_hard_cap(state):
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

        _, params = _parse_request_target(headers.get(":target", self.ws_path))
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
            "[scrum-poker:{}] websocket client {} {} from {}\n".format(
                self.app_id,
                connection["client_id"],
                "resumed" if resumed else "connected",
                client_addr,
            )
        )
        self.broadcast_state()

        skip_async_cleanup = False
        try:
            while True:
                if self.destroyed or state.get("destroyed") or _room_has_expired(state):
                    return

                event = await websocket.receive()
                event_type = event["type"]

                if event_type == "pong":
                    continue

                if event_type == "close":
                    return

                if event_type == "binary":
                    _queue_error(connection, "binary websocket messages are not supported")
                    continue

                now_ms = _now_ms(state)
                if now_ms - connection.get("last_msg_ms", 0) < MESSAGE_RATE_LIMIT_MS:
                    burst = connection.get("rate_burst", 0) + 1
                    connection["rate_burst"] = burst
                    if burst > MESSAGE_RATE_BURST:
                        _queue_error(connection, "rate limit exceeded - slowing down too fast")
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

                self.broadcast_state()
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
                task.OS.print(
                    "[scrum-poker:{}] websocket client {} disconnected\n".format(self.app_id, connection["client_id"])
                )
                _expire_stale_connections(state)
                if not self.destroyed and not state.get("destroyed"):
                    self.broadcast_state()

    def broadcast_state(self):
        """Queue a fresh state snapshot for every websocket client in this room."""
        if not self.destroyed and not self.state.get("destroyed"):
            _broadcast_state(self.state)


__all__ = ["ScrumPokerApp", "idle_watchdog_task", "EPHEMERAL_ROOM_TTL_SECONDS"]
