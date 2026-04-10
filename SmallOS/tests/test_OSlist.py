import sys
import unittest

sys.path.append("..")

from SmallPackage.OSlist import OSList
from SmallPackage.SmallTask import SmallTask


class TestOSList(unittest.TestCase):
    def test_insert_assigns_incrementing_pids(self):
        tasks = OSList(10)
        inserted = [SmallTask((index % 9) + 1, None, name=str(index)) for index in range(32)]

        for task in inserted:
            tasks.insert(task)

        self.assertEqual(list(range(32)), [task.pid for task in tasks.list()])

    def test_pop_respects_priority_order(self):
        tasks = OSList(10)
        items = [
            SmallTask(5, None, name="slow"),
            SmallTask(1, None, name="fast"),
            SmallTask(3, None, name="medium"),
        ]

        for task in items:
            tasks.insert(task)
            tasks.enqueue(task)

        popped = [tasks.pop(), tasks.pop(), tasks.pop()]
        self.assertEqual(["fast", "medium", "slow"], [task.name for task in popped])

    def test_search_and_delete(self):
        tasks = OSList(10)
        task = SmallTask(2, None, name="worker")
        pid = tasks.insert(task)

        self.assertIs(tasks.search(pid), task)
        self.assertEqual(0, tasks.delete(pid))
        self.assertEqual(-1, tasks.search(pid))


if __name__ == "__main__":
    unittest.main()
