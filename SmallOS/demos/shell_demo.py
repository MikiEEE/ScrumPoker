"""Demo showing a shell session running alongside other cooperative tasks."""

from common import build_runtime

from SmallPackage import SmallTask, Unix
from SmallPackage.shells import BaseShell


async def background_worker(task):
    """Keep producing app output while the shell inspects the runtime."""
    for step in range(12):
        task.OS.print("[{}] working step {}\n".format(task.name, step))
        await task.sleep(0.05)
    return task.name


def _pid_for_name(os_ref, name):
    """Look up a live task PID by task name for shell scripting."""
    for item in os_ref.tasks.list():
        if item.name == name:
            return item.getID()
    return None


async def shell_session(task):
    """
    Drive a scripted shell session while other tasks continue to run.

    This avoids a blocking stdin loop and makes the demo portable. The shell
    still uses the same command parser and runtime APIs as an interactive shell
    would.
    """
    shell = task.OS.shells[0]
    worker_pid = _pid_for_name(task.OS, "background_worker")
    script = [
        "toggle",
        "help",
        "count",
        "ps",
        "signal {} 3".format(worker_pid) if worker_pid is not None else "ps",
        "signals {}".format(worker_pid) if worker_pid is not None else "ps",
        "io status",
        "stat {}".format(worker_pid) if worker_pid is not None else "ps",
        "toggle",
    ]

    for command in script:
        await task.sleep(0.05)
        shell.run(command, show_prompt=False, echo_command=True, force_output=True)
        if not shell.is_running:
            break
    return "shell session complete"


def main():
    shell = BaseShell()
    runtime = build_runtime(Unix())
    runtime.shells.append(shell.setOS(runtime))
    runtime.fork(
        [
            SmallTask(2, shell_session, name="shell_session"),
            SmallTask(4, background_worker, name="background_worker"),
            SmallTask(6, background_worker, name="background_worker_low"),
        ]
    )
    runtime.startOS()


if __name__ == "__main__":
    main()
