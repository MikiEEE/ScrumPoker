import sys
import unittest

sys.path.append("..")

from SmallPackage.Kernel import Kernel
from SmallPackage.SmallOS import SmallOS
from SmallPackage.SmallTask import SmallTask
from SmallPackage.SmallErrors import TaskCancelledError


class FakeKernel(Kernel):
    def __init__(self):
        super().__init__()
        self.now = 0
        self.output = []
        self.readable = set()
        self.writable = set()

    def write(self, msg):
        self.output.append(msg)

    def time_epoch(self):
        return self.now / 1000

    def time_monotonic(self):
        return self.now / 1000

    def ticks_ms(self):
        return self.now

    def ticks_add(self, base, delta_ms):
        return base + delta_ms

    def ticks_diff(self, end, start):
        return end - start

    def sleep(self, secs):
        if secs > 0:
            self.now += int(secs * 1000)

    def sleep_ms(self, delay_ms):
        if delay_ms > 0:
            self.now += int(delay_ms)

    def io_wait(self, readables, writables, timeout_ms=None):
        if timeout_ms is not None and timeout_ms > 0:
            self.now += int(timeout_ms)

        ready_read = [obj for obj in readables if obj in self.readable]
        ready_write = [obj for obj in writables if obj in self.writable]

        for obj in ready_read:
            self.readable.discard(obj)
        for obj in ready_write:
            self.writable.discard(obj)

        return ready_read, ready_write

    def mark_readable(self, obj):
        self.readable.add(obj)

    def mark_writable(self, obj):
        self.writable.add(obj)


class TestRuntime(unittest.TestCase):
    def build_os(self, *tasks):
        kernel = FakeKernel()
        runtime = SmallOS().setKernel(kernel)
        runtime.fork(list(tasks))
        runtime.startOS()
        return runtime, kernel

    def test_priority_order_is_preserved_before_and_after_sleep(self):
        events = []

        async def worker(task):
            events.append("{}-start".format(task.name))
            await task.sleep(1)
            events.append("{}-end".format(task.name))
            return task.name

        high = SmallTask(1, worker, name="high")
        low = SmallTask(5, worker, name="low")

        self.build_os(low, high)

        self.assertEqual(
            ["high-start", "low-start", "high-end", "low-end"],
            events,
        )

    def test_join_all_returns_results_in_requested_order(self):
        async def child(task, value, delay):
            await task.sleep(delay)
            return value

        async def parent(task):
            first = task.spawn(child, priority=4, name="first", args=("first", 0.3))
            second = task.spawn(child, priority=2, name="second", args=("second", 0.1))
            return await task.join_all([first, second])

        parent_task = SmallTask(1, parent, name="parent")
        self.build_os(parent_task)

        self.assertEqual(["first", "second"], parent_task.result)

    def test_wait_signal_and_join_resume_once(self):
        async def sender(task):
            await task.sleep(0.2)
            task.sendSignal(task.parent.pid, 7)
            return "sent"

        async def parent(task):
            child = task.spawn(sender, priority=2, name="sender")
            signal = await task.wait_signal(7)
            result = await task.join(child)
            return (signal, result)

        parent_task = SmallTask(1, parent, name="parent")
        self.build_os(parent_task)

        self.assertEqual((7, "sent"), parent_task.result)

    def test_killing_joined_child_raises_into_waiter(self):
        async def sleeper(task):
            await task.sleep(10)
            return "too late"

        async def canceller(task, target):
            await task.sleep(1)
            target.kill()
            return "cancelled"

        async def parent(task):
            child = task.spawn(sleeper, priority=3, name="sleeper")
            task.spawn(canceller, priority=1, name="canceller", args=(child,))
            try:
                await task.join(child)
            except TaskCancelledError:
                return "caught-cancel"
            return "missed-cancel"

        parent_task = SmallTask(2, parent, name="parent")
        self.build_os(parent_task)

        self.assertEqual("caught-cancel", parent_task.result)

    def test_wait_readable_resumes_on_kernel_io_event(self):
        io_obj = object()

        async def notifier(task, watched):
            await task.sleep(0.2)
            task.OS.kernel.mark_readable(watched)
            return "marked"

        async def waiter(task, watched):
            task.spawn(notifier, priority=1, name="notifier", args=(watched,))
            ready_obj = await task.wait_readable(watched)
            return ready_obj is watched

        waiter_task = SmallTask(2, waiter, name="waiter", args=(io_obj,))
        self.build_os(waiter_task)

        self.assertTrue(waiter_task.result)

    def test_killing_io_waiter_clears_wait_registration(self):
        io_obj = object()

        async def io_waiter(task, watched):
            await task.wait_readable(watched)
            return "unexpected"

        async def killer(task, target):
            await task.sleep(0.2)
            target.kill()
            return "killed"

        async def parent(task, watched):
            child = task.spawn(io_waiter, priority=3, name="io_waiter", args=(watched,))
            task.spawn(killer, priority=1, name="killer", args=(child,))
            await task.sleep(0.5)
            return watched not in task.OS.ioReadWaiters

        parent_task = SmallTask(2, parent, name="parent", args=(io_obj,))
        self.build_os(parent_task)

        self.assertTrue(parent_task.result)


if __name__ == "__main__":
    unittest.main()
