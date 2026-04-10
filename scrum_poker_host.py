"""Shared listener and router for mounted scrum poker boards."""

from SmallOS.SmallPackage import SmallTask

from scrum_poker_core import (
    LISTEN_BACKLOG,
    _close_socket_quietly,
    _get_host,
    _get_port,
    _http_response,
    _parse_http_request,
    _parse_request_target,
    _read_request_head,
    _send_all,
)


class ScrumPokerHost:
    """Shared listener and router for multiple mounted scrum poker apps."""

    def __init__(self, apps, host=None, port=None):
        self.apps = list(apps)
        self.apps_by_id = {app.app_id: app for app in self.apps}
        self.host = _get_host() if host is None else host
        self.port = _get_port() if port is None else port
        self.listener = None
        self._apps_by_specificity = sorted(
            self.apps,
            key=lambda app: len(app.base_path),
            reverse=True,
        )

    def to_task(self):
        """Return the single shared listener task."""
        return SmallTask(2, self.web_server_task, name="scrum_poker_host")

    def shutdown(self):
        """Release the shared listener socket."""
        kernel = getattr(getattr(self.apps[0], "runtime", None), "kernel", None) if self.apps else None
        _close_socket_quietly(kernel, self.listener)
        self.listener = None

    def resolve_app(self, path):
        """Resolve the mounted app for one inbound request path."""
        for app in self._apps_by_specificity:
            if app.matches_path(path):
                return app
        return None

    def route_summary(self):
        """Return one compact route summary string for startup output."""
        routes = []
        for app in self._apps_by_specificity:
            routes.extend(app.route_list())
        return " ".join(routes)

    async def web_client_handler(self, task, client_sock, client_addr):
        """Handle one inbound HTTP/WebSocket client using mounted app routing."""
        kernel = task.OS.kernel

        try:
            try:
                request_head = await _read_request_head(task, client_sock)
            except TimeoutError:
                return
            except ValueError:
                await _send_all(task, client_sock, _http_response(413, "request header too large\n"))
                return

            method, target, headers = _parse_http_request(request_head)
            if method is None:
                return

            path, _ = _parse_request_target(target)
            app = self.resolve_app(path)
            if app is None:
                await _send_all(task, client_sock, _http_response(404, "route not found\n"))
                return

            is_websocket = (
                method == "GET"
                and path == app.ws_path
                and "upgrade" in headers.get("connection", "").lower()
                and headers.get("upgrade", "").lower() == "websocket"
            )
            if is_websocket:
                headers = dict(headers)
                headers[":target"] = target
                await app.websocket_session(task, client_sock, client_addr, headers)
                return

            payload = app.build_http_response(method, target, path)
            await _send_all(task, client_sock, payload)
        finally:
            _close_socket_quietly(kernel, client_sock)

    async def web_server_task(self, task):
        """Run the cooperative shared HTTP and WebSocket listener."""
        kernel = task.OS.kernel
        if kernel is None:
            raise RuntimeError("web_server_task requires a kernel-enabled runtime.")

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
        task.OS.print("Open the shell and use: poker apps | poker root session open | poker legalease session open\n")

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
