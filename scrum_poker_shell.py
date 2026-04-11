"""Shell commands for premium and ephemeral scrum poker rooms."""

from SmallOS.SmallPackage.shells import BaseShell, ShellCommandError

from scrum_poker_core import IDLE_TIMEOUT_SECONDS, _clear_everyone, _now_ms, _touch_activity


class ScrumPokerShell(BaseShell):
    """SmallOS shell extension for scrum poker room administration."""

    def __init__(self, host, *args, **kwargs):
        self.host = host
        super().__init__(*args, **kwargs)

    def _register_builtin_commands(self):
        super()._register_builtin_commands()
        self._register_command(
            "poker",
            self.command_poker,
            "Manage scrum poker rooms. Usage: poker rooms | poker <room_id> session|idle|clear ...",
            aliases=("scrum",),
        )

    def _list_rooms(self):
        """Return the active room registry summary."""
        lines = ["Active scrum poker rooms:"]
        for room in self.host.all_rooms():
            label = " ({})".format(room.label) if room.label else ""
            room_type = "premium" if room.room_kind == "premium" else "ephemeral"
            lines.append("- {}: {} [{}]{}".format(room.app_id, room.base_path, room_type, label))
        return "\n".join(lines)

    def _get_room(self, room_id):
        """Resolve one mounted room id or raise a shell-friendly error."""
        room = self.host.get_room(room_id)
        if room is None:
            raise ShellCommandError("unknown scrum poker room {!r}".format(room_id))
        return room

    def _poker_help(self, room=None):
        """Return the poker-specific shell help text."""
        if room is None:
            return "\n".join(
                [
                    "Scrum poker commands:",
                    "- poker rooms",
                    "- poker apps",
                    "- poker <room_id> session [open|close|status|toggle]",
                    "- poker <room_id> idle [status|reset]",
                    "- poker <room_id> clear everyone",
                ]
            )

        return "\n".join(
            [
                "Scrum poker commands for {}:".format(room.app_id),
                "- poker {} session [open|close|status|toggle]".format(room.app_id),
                "- poker {} idle [status|reset]".format(room.app_id),
                "- poker {} clear everyone".format(room.app_id),
            ]
        )

    def command_poker(self, args):
        """Dispatch one namespaced scrum poker command."""
        if not args or args[0] in ("help", "?"):
            return self._poker_help()

        if args[0] in ("rooms", "apps"):
            return self._list_rooms()

        room = self._get_room(args[0])
        if len(args) == 1:
            return self._poker_help(room)

        command = args[1]
        if command == "session":
            return self._command_session(room, args[2:])
        if command == "idle":
            return self._command_idle(room, args[2:])
        if command == "clear":
            return self._command_clear(room, args[2:])
        raise ShellCommandError("usage: poker rooms | poker <room_id> session|idle|clear ...")

    def _command_session(self, room, args):
        """Open, close, or inspect the join gate for one room."""
        action = args[0] if args else "status"
        state = room.state

        if action == "status":
            return "{} joining is {}".format(room.app_id, "open" if state["session_open"] else "closed")

        if action == "open":
            state["session_open"] = True
            _touch_activity(state)
            room.broadcast_state()
            return "{} joining enabled".format(room.app_id)

        if action == "close":
            state["session_open"] = False
            _touch_activity(state)
            room.broadcast_state()
            return "{} joining disabled".format(room.app_id)

        if action == "toggle":
            state["session_open"] = not state["session_open"]
            _touch_activity(state)
            room.broadcast_state()
            return "{} joining {}".format(room.app_id, "enabled" if state["session_open"] else "disabled")

        raise ShellCommandError("usage: poker {} session [open|close|status|toggle]".format(room.app_id))

    def _command_idle(self, room, args):
        """Show time since last activity, or reset the idle clock for one room."""
        action = args[0] if args else "status"
        state = room.state

        if action == "reset":
            _touch_activity(state)
            return "{} idle clock reset".format(room.app_id)

        if action == "status":
            idle_ms = _now_ms(state) - state.get("last_activity_ms", _now_ms(state))
            idle_s = max(0, idle_ms // 1000)
            remaining_s = max(0, IDLE_TIMEOUT_SECONDS - idle_s)
            return "{} idle for {}s / timeout in {}s ({}min)".format(
                room.app_id,
                idle_s,
                remaining_s,
                remaining_s // 60,
            )

        raise ShellCommandError("usage: poker {} idle [status|reset]".format(room.app_id))

    def _command_clear(self, room, args):
        """Kick everyone off the board and close the session for one room."""
        action = args[0] if args else "everyone"
        if action not in ("everyone", "all"):
            raise ShellCommandError("usage: poker {} clear everyone".format(room.app_id))

        cleared_count = _clear_everyone(room.state)
        room.broadcast_state()
        return "{} cleared {} participant(s); session closed".format(room.app_id, cleared_count)
