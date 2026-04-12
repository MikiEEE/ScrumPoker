"""Cooperative websocket scrum poker app built on top of SmallOS."""

from SmallOS.SmallPackage.SmallErrors import TaskCancelledError

from scrum_poker_app import ScrumPokerApp
from scrum_poker_core import *  # noqa: F401,F403 - preserve the existing helper surface for tests and callers
from scrum_poker_core import __all__ as _core_exports
from scrum_poker_host import ScrumPokerHost
from scrum_poker_shell import ScrumPokerShell


__all__ = list(_core_exports) + ["ScrumPokerApp", "ScrumPokerHost", "ScrumPokerShell", "build_premium_room", "main"]


def build_premium_room(runtime):
    """Create the permanent configurable premium room."""
    premium_slug = _get_premium_room_slug()
    return ScrumPokerApp(
        premium_slug,
        "/{}".format(premium_slug),
        runtime,
        title="Sprint Poker",
        label=_get_premium_room_label(),
        room_kind="premium",
        join_limit=PREMIUM_JOIN_LIMIT,
        admin_auth_mode="premium",
    )


def main():
    """Start the premium + ephemeral SmallOS scrum poker runtime."""
    runtime = _build_runtime()
    premium_room = build_premium_room(runtime)
    host = ScrumPokerHost([premium_room], host=_get_host(), port=_get_port())
    premium_room.host = host

    shell = ScrumPokerShell(host, prompt="poker> ", allow_python=False)
    runtime.shells.append(shell.setOS(runtime))

    host_task = host.to_task()
    room_tasks = [legalease_room.make_watchdog_task()]
    shell_stdin = shell.make_task(
        priority=1,
        name="shell_stdin",
        is_watcher=True,
        poll_interval=0.1,
        banner_text=(
            "\nInteractive scrum poker shell enabled.\n"
            "Commands: poker rooms, poker {} session open, poker <guid> session open, ps, stat <pid>, toggle, help\n".format(
                premium_room.app_id
            )
        ),
        force_output=True,
    )

    runtime.fork([host_task, *room_tasks, shell_stdin])

    try:
        runtime.startOS()

        if host_task.exception is not None and not isinstance(host_task.exception, TaskCancelledError):
            raise host_task.exception

        for room_task in room_tasks:
            if room_task.exception is not None and not isinstance(room_task.exception, TaskCancelledError):
                raise room_task.exception

        if shell_stdin.exception is not None and not isinstance(shell_stdin.exception, TaskCancelledError):
            raise shell_stdin.exception
    finally:
        _shutdown_runtime(runtime, host, [premium_room])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nscrum poker app stopped")
