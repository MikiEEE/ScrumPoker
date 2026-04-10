import sys
import unittest

sys.path.append("..")

from SmallPackage.SmallIO import SmallIO
from SmallPackage.SmallTask import SmallTask
from SmallPackage.shells import BaseShell


class FakeKernel:
    def __init__(self):
        self.writes = []
        self.shell_split_calls = []

    def write(self, msg):
        self.writes.append(msg)

    def shell_split(self, line):
        self.shell_split_calls.append(line)
        if line == 'echo "hello shell"':
            return ["echo", "hello shell"]
        return line.split()


class FakeTask:
    def __init__(self, pid, name, children=None):
        self.pid = pid
        self.name = name
        self.priority = pid
        self.children = list(children or [])
        self._signals = [0] * 32

    def getID(self):
        return self.pid

    def getSignals(self):
        return [index for index, value in enumerate(self._signals) if value]

    def acceptSignal(self, sig):
        self._signals[sig] = 1

    def stat(self):
        return "task {} stat".format(self.pid)

    def __str__(self):
        return "PID={}, name={}".format(self.pid, self.name)


class FakeTaskRegistry:
    def __init__(self, tasks):
        self._tasks = list(tasks)
        self.numWatchers = 0

    def list(self):
        return list(self._tasks)

    def search(self, pid):
        for task in self._tasks:
            if task.getID() == pid:
                return task
        return -1

    def remove(self, pid):
        self._tasks = [task for task in self._tasks if task.getID() != pid]

    def __len__(self):
        return len(self._tasks)


class FakeOS(SmallIO):
    def __init__(self, tasks, buffer_length=4):
        super().__init__(buffer_length)
        self.kernel = FakeKernel()
        self.tasks = FakeTaskRegistry(tasks)
        self.cancelled = []

    def cancel_task(self, task, recursive=False):
        self.cancelled.append((task.getID(), recursive))
        self.tasks.remove(task.getID())
        return 0

    def __str__(self):
        return "FakeOS dump"


class TestSmallIO(unittest.TestCase):
    def test_smallio_buffers_app_output_until_terminal_returns(self):
        os_ref = FakeOS([])
        os_ref.print("app-one\n")
        self.assertEqual(["app-one\n"], os_ref.kernel.writes)

        os_ref.toggleTerminal()
        os_ref.print("hidden-one\n")
        os_ref.print("hidden-two\n")
        self.assertEqual(["hidden-one\n", "hidden-two\n"], os_ref.getBufferedOutput())

        os_ref.toggleTerminal()
        self.assertEqual(
            [
                "app-one\n",
                "****************\n",
                "****************\n",
                "hidden-one\n",
                "hidden-two\n",
            ],
            os_ref.kernel.writes,
        )
        self.assertEqual([], os_ref.getBufferedOutput())


class TestShells(unittest.TestCase):
    def setUp(self):
        tasks = [
            FakeTask(1, "shell_session", children=[2]),
            FakeTask(2, "worker"),
        ]
        self.os_ref = FakeOS(tasks)
        self.shell = BaseShell().setOS(self.os_ref)
        self.os_ref.toggleTerminal()
        self.os_ref.kernel.writes.clear()

    def test_shell_can_count_stat_and_signal_tasks(self):
        self.shell.run("count", show_prompt=False)
        self.shell.run("stat 2", show_prompt=False)
        self.shell.run("signal 2 3", show_prompt=False)
        output = "".join(self.os_ref.kernel.writes)

        self.assertIn("2 task(s) registered, 0 watcher(s)", output)
        self.assertIn("task 2 stat", output)
        self.assertIn("sent signal 3 to PID 2", output)
        self.assertEqual([3], self.os_ref.tasks.search(2).getSignals())

    def test_shell_can_manage_io_python_and_exit(self):
        self.os_ref.print("buffered-one\n")
        self.os_ref.print("buffered-two\n")

        self.shell.run("io status", show_prompt=False)
        self.shell.run("io show", show_prompt=False)
        self.shell.run("python 1 + 1", show_prompt=False)
        self.shell.run("kill 2 -r", show_prompt=False)
        self.shell.run("exit", show_prompt=False)
        output = "".join(self.os_ref.kernel.writes)

        self.assertIn("buffered_messages=2", output)
        self.assertIn("buffered-one", output)
        self.assertIn("2\n", output)
        self.assertIn("cancelled PID 2 recursively", output)
        self.assertEqual([(2, True)], self.os_ref.cancelled)
        self.assertFalse(self.shell.is_running)

    def test_shell_uses_kernel_shell_splitter(self):
        self.shell.run('echo "hello shell"', show_prompt=False)
        output = "".join(self.os_ref.kernel.writes)

        self.assertEqual(['echo "hello shell"'], self.os_ref.kernel.shell_split_calls)
        self.assertIn("hello shell", output)

    def test_shell_can_build_task_from_factory(self):
        shell_task = self.shell.make_task(
            priority=6,
            name="custom_shell_task",
            is_watcher=True,
            poll_interval=0.25,
            show_banner=False,
            prompt_on_start=False,
            force_output=False,
            echo_commands=True,
        )

        self.assertIsInstance(shell_task, SmallTask)
        self.assertEqual(6, shell_task.priority)
        self.assertEqual("custom_shell_task", shell_task.name)
        self.assertTrue(shell_task.isWatcher)
        self.assertTrue(callable(shell_task.routine))


if __name__ == "__main__":
    unittest.main()
