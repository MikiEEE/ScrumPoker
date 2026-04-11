"""Shell commands for multi-board scrum poker administration."""

from SmallOS.SmallPackage.shells import BaseShell, ShellCommandError

from scrum_poker_core import IDLE_TIMEOUT_SECONDS, _clear_everyone, _now_ms, _set_session_open, _touch_activity


class ScrumPokerShell(BaseShell):
    """Multi-app SmallOS shell extension for scrum poker instances."""

    def __init__(self, apps, host=None, *args, **kwargs):
        self.apps = {app.app_id: app for app in apps}
        self.host = host
        super().__init__(*args, **kwargs)

    def _register_builtin_commands(self):
        super()._register_builtin_commands()
        self._register_command(
            "poker",
            self.command_poker,
            "Manage mounted scrum poker apps. Usage: poker apps | poker stats | poker <app_id> session|idle|clear ...",
            aliases=("scrum",),
        )

    def _list_apps(self):
        """Return the mounted app registry summary."""
        lines = ["Mounted scrum poker apps:"]
        for app_id in sorted(self.apps):
            app = self.apps[app_id]
            label = " ({})".format(app.label) if app.label else ""
            lines.append("- {}: {}{}".format(app.app_id, app.base_path, label))
        return "\n".join(lines)

    def _get_app(self, app_id):
        """Resolve one mounted app id or raise a shell-friendly error."""
        app = self.apps.get(app_id)
        if app is None:
            raise ShellCommandError("unknown scrum poker app {!r}".format(app_id))
        return app

    def _poker_help(self, app=None):
        """Return the poker-specific shell help text."""
        if app is None:
            return "\n".join(
                [
                    "Scrum poker commands:",
                    "- poker apps",
                    "- poker stats",
                    "- poker <app_id> session [open|close|status|toggle]",
                    "- poker <app_id> idle [status|reset]",
                    "- poker <app_id> clear everyone",
                ]
            )

        return "\n".join(
            [
                "Scrum poker commands for {}:".format(app.app_id),
                "- poker {} session [open|close|status|toggle]".format(app.app_id),
                "- poker {} idle [status|reset]".format(app.app_id),
                "- poker {} clear everyone".format(app.app_id),
            ]
        )

    def command_poker(self, args):
        """Dispatch one namespaced scrum poker command."""
        if not args or args[0] in ("help", "?"):
            return self._poker_help()

        if args[0] == "apps":
            return self._list_apps()
        if args[0] == "stats":
            return self._stats_text()

        app = self._get_app(args[0])
        if len(args) == 1:
            return self._poker_help(app)

        command = args[1]
        if command == "session":
            return self._command_session(app, args[2:])
        if command == "idle":
            return self._command_idle(app, args[2:])
        if command == "clear":
            return self._command_clear(app, args[2:])
        raise ShellCommandError("usage: poker apps | poker stats | poker <app_id> session|idle|clear ...")

    def _stats_snapshot(self):
        """Return the current aggregate stats snapshot."""
        if self.host is not None and hasattr(self.host, "stats_snapshot"):
            return self.host.stats_snapshot()

        apps = [app.stats_snapshot() for app in self.apps.values()]
        return {
            "apps": apps,
            "totals": {
                "connected_transports": sum(item.get("connected_transports", 0) for item in apps),
                "dropped_state_updates": sum(item.get("dropped_state_updates", 0) for item in apps),
                "joined_users": sum(item.get("joined_users", 0) for item in apps),
                "pending_messages": sum(item.get("pending_messages", 0) for item in apps),
                "pending_state_messages": sum(item.get("pending_state_messages", 0) for item in apps),
                "queue_disconnects": sum(item.get("queue_disconnects", 0) for item in apps),
            },
        }

    def _stats_text(self):
        """Format one human-readable stats summary for all mounted apps."""
        snapshot = self._stats_snapshot()
        lines = ["Scrum poker stats:"]
        for app_stats in snapshot.get("apps", ()):
            lines.append(
                "- {app_id} ({base_path}): joined={joined_users} connected={connected_transports} "
                "pending_state={pending_state_messages} pending_aux={pending_messages} "
                "dropped_state={dropped_state_updates} queue_disconnects={queue_disconnects}".format(**app_stats)
            )
        totals = snapshot.get("totals", {})
        lines.append(
            "Totals: joined={joined_users} connected={connected_transports} pending_state={pending_state_messages} "
            "pending_aux={pending_messages} dropped_state={dropped_state_updates} "
            "queue_disconnects={queue_disconnects}".format(
                joined_users=totals.get("joined_users", 0),
                connected_transports=totals.get("connected_transports", 0),
                pending_state_messages=totals.get("pending_state_messages", 0),
                pending_messages=totals.get("pending_messages", 0),
                dropped_state_updates=totals.get("dropped_state_updates", 0),
                queue_disconnects=totals.get("queue_disconnects", 0),
            )
        )
        return "\n".join(lines)

    def _command_session(self, app, args):
        """Open, close, or inspect the join gate for one mounted app."""
        action = args[0] if args else "status"
        state = app.state

        if action == "status":
            return "{} joining is {}".format(app.app_id, "open" if state["session_open"] else "closed")

        if action == "open":
            _set_session_open(state, True)
            _touch_activity(state)
            app.broadcast_state()
            return "{} joining enabled".format(app.app_id)

        if action == "close":
            _set_session_open(state, False)
            _touch_activity(state)
            app.broadcast_state()
            return "{} joining disabled".format(app.app_id)

        if action == "toggle":
            _set_session_open(state, not state["session_open"])
            _touch_activity(state)
            app.broadcast_state()
            return "{} joining {}".format(app.app_id, "enabled" if state["session_open"] else "disabled")

        raise ShellCommandError("usage: poker {} session [open|close|status|toggle]".format(app.app_id))

    def _command_idle(self, app, args):
        """Show time since last activity, or reset the idle clock for one app."""
        action = args[0] if args else "status"
        state = app.state

        if action == "reset":
            _touch_activity(state)
            return "{} idle clock reset".format(app.app_id)

        if action == "status":
            idle_ms = _now_ms(state) - state.get("last_activity_ms", _now_ms(state))
            idle_s = max(0, idle_ms // 1000)
            remaining_s = max(0, IDLE_TIMEOUT_SECONDS - idle_s)
            return "{} idle for {}s / timeout in {}s ({}min)".format(
                app.app_id,
                idle_s,
                remaining_s,
                remaining_s // 60,
            )

        raise ShellCommandError("usage: poker {} idle [status|reset]".format(app.app_id))

    def _command_clear(self, app, args):
        """Kick everyone off the board and close the session for one app."""
        action = args[0] if args else "everyone"
        if action not in ("everyone", "all"):
            raise ShellCommandError("usage: poker {} clear everyone".format(app.app_id))

        cleared_count = _clear_everyone(app.state)
        app.broadcast_state()
        return "{} cleared {} participant(s); session closed".format(app.app_id, cleared_count)
