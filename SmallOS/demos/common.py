"""
Shared demo helpers for desktop and board-specific smallOS examples.

These helpers keep the individual demo files short while still showing the
recommended public API: load a config file, choose a kernel, spawn tasks, and
start the runtime.
"""

import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


from SmallPackage.SmallConfig import SmallOSConfig
from SmallPackage.SmallOS import SmallOS
from SmallPackage.SmallTask import SmallTask


CONFIG_PATH = os.path.join(REPO_ROOT, "smallos.config.json")
DEMO_SIGNAL = 3


def load_demo_config(**overrides):
    """Load the repo-level config file and apply any demo-specific overrides."""
    config = SmallOSConfig.from_json_file(CONFIG_PATH)
    if overrides:
        config = config.copy(**overrides)
    return config


def build_runtime(kernel, **config_overrides):
    """Create a ``SmallOS`` instance wired to the chosen kernel."""
    return SmallOS(config=load_demo_config(**config_overrides)).setKernel(kernel)


async def worker(task):
    """Simple cooperative child used by several demos."""
    for step in range(3):
        task.OS.print("[{}] step {}\n".format(task.name, step))
        await task.sleep(0.05)
    return task.name


async def join_demo(task):
    """Show child spawning plus ordered ``join_all`` collection."""
    task.OS.print("join demo starting\n")
    fast = task.spawn(worker, priority=1, name="fast")
    medium = task.spawn(worker, priority=3, name="medium")
    slow = task.spawn(worker, priority=5, name="slow")
    results = await task.join_all([fast, medium, slow])
    task.OS.print("join demo results: {}\n".format(results))
    return results


async def signal_sender(task):
    """Wake the parent after a short delay."""
    await task.sleep(0.1)
    task.OS.print("sender raising signal {}\n".format(DEMO_SIGNAL))
    task.sendSignal(task.parent.pid, DEMO_SIGNAL)
    return "signal sent"


async def signal_demo(task):
    """Show a task blocked on a signal and then joined with its sender."""
    task.OS.print("signal demo waiting\n")
    sender = task.spawn(signal_sender, priority=max(1, task.priority - 1), name="signal_sender")
    signal = await task.wait_signal(DEMO_SIGNAL)
    sender_result = await task.join(sender)
    task.OS.print("signal demo resumed on {} with {}\n".format(signal, sender_result))
    return sender_result


async def startup_banner(task, board_name):
    """Print one short startup banner and yield once."""
    task.OS.print("smallOS demo booted on {}\n".format(board_name))
    await task.yield_now()
    return board_name


def default_tasks(board_name):
    """Return a small starter task set used by most demos."""
    return [
        SmallTask(2, startup_banner, name="startup_banner", args=(board_name,)),
        SmallTask(4, signal_demo, name="signal_demo"),
        SmallTask(6, join_demo, name="join_demo"),
    ]
