"""Cooperative websocket scrum poker app built on top of SmallOS."""

from SmallOS.SmallPackage.SmallErrors import TaskCancelledError

from scrum_poker_board import ScrumPokerApp
from scrum_poker_core import *  # noqa: F401,F403 - preserve the existing helper surface for tests and callers
from scrum_poker_core import __all__ as _core_exports
from scrum_poker_host import ScrumPokerHost
from scrum_poker_shell import ScrumPokerShell


__all__ = list(_core_exports) + ["ScrumPokerApp", "ScrumPokerHost", "ScrumPokerShell", "main"]


def main():
    """Start the multi-instance SmallOS scrum poker runtime."""
    runtime = _build_runtime()
    root_app = ScrumPokerApp("root", "/", runtime, title="Sprint Poker")
    legalease_app = ScrumPokerApp("legalease", "/legalease", runtime, title="Sprint Poker", label="Legalease")
    apps = [root_app, legalease_app]
    host = ScrumPokerHost(apps, host=_get_host(), port=_get_port())

    shell = ScrumPokerShell(apps, prompt="poker> ", allow_python=False)
    runtime.shells.append(shell.setOS(runtime))

    host_task = host.to_task()
    app_tasks = [app.to_task() for app in apps]
    shell_stdin = shell.make_task(
        priority=1,
        name="shell_stdin",
        is_watcher=True,
        poll_interval=0.1,
        banner_text=(
            "\nInteractive scrum poker shell enabled.\n"
            "Commands: poker apps, poker root session open, poker legalease session open, ps, stat <pid>, toggle, help\n"
        ),
        force_output=True,
    )

    runtime.fork([host_task, *app_tasks, shell_stdin])

    try:
        runtime.startOS()

        if host_task.exception is not None and not isinstance(host_task.exception, TaskCancelledError):
            raise host_task.exception

        for app_task in app_tasks:
            if app_task.exception is not None and not isinstance(app_task.exception, TaskCancelledError):
                raise app_task.exception

        if shell_stdin.exception is not None and not isinstance(shell_stdin.exception, TaskCancelledError):
            raise shell_stdin.exception
    finally:
        _shutdown_runtime(runtime, host, apps)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nscrum poker app stopped")
