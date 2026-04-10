"""
Task registry and scheduling queues for smallOS.

The runtime needs two different access patterns:
- look up a task quickly by PID
- choose the next runnable task by priority

This module keeps those responsibilities together so the scheduler can stay
small and focused. PID lookup uses a sorted list, ready tasks live in one FIFO
queue per priority, and sleeping tasks live in a wake-time heap.
"""

from collections import deque
import heapq

from .list_util.binSearchList import insert, search
from .SmallPID import SmallPID


class OSList(SmallPID):
    """
    Combined PID registry and queue manager for the cooperative scheduler.
    """

    def __init__(self, priors=5, length=2**12):
        """Create the PID registry plus ready/sleep queue structures."""
        SmallPID.__init__(self, length)
        self.num_priorities = priors
        self.tasks = []
        self.ready = [deque() for _ in range(priors)]
        self.sleeping = []
        self._sleep_seq = 0
        self.numWatchers = 0
        self.func = lambda data, index: data[index].getID()

    def resetCatSel(self):
        """Compatibility no-op kept for older callers."""
        return

    def insert(self, task):
        """Assign a PID and register a task in the PID-sorted backing list."""
        priority = task.priority
        if not 0 < priority < self.num_priorities:
            return -1

        pid = self.newPID()
        if pid == -1:
            return -1

        task.setID(pid)
        if task.isWatcher:
            self.numWatchers += 1

        index = insert(self.tasks, pid, 0, len(self.tasks), func=self.func)
        self.tasks.insert(index, task)
        return pid

    def search(self, pid):
        """Look up a task by PID."""
        length = len(self.tasks)
        index = search(self.tasks, pid, 0, length, self.func)
        if index == -1:
            return -1
        return self.tasks[index]

    def delete(self, pid):
        """Remove a task from PID storage and watcher accounting."""
        length = len(self.tasks)
        index = search(self.tasks, pid, 0, length, self.func)
        if index == -1:
            return -1

        task = self.tasks[index]
        if task.isWatcher:
            self.numWatchers -= 1
        del self.tasks[index]
        self.freePID(pid)
        return 0

    def enqueue(self, task, front=False):
        """
        Put a runnable task on its per-priority ready queue.

        ``front=True`` is used when a task should resume before other tasks of
        the same priority, such as when a join or signal completes.
        """
        if task == -1 or task is None or task.done:
            return -1
        if self.search(task.getID()) == -1:
            return -1
        if task._queued:
            return 0

        queue = self.ready[task.priority]
        if front:
            queue.appendleft(task)
        else:
            queue.append(task)
        task._queued = True
        return 0

    def pop(self):
        """
        Return the next runnable task.

        Lower numeric priority values run first. Within the same priority,
        arrival order is preserved by the deque.
        """
        for priority in range(1, self.num_priorities):
            queue = self.ready[priority]
            while queue:
                task = queue.popleft()
                task._queued = False
                if self.search(task.getID()) == -1:
                    continue
                if not task.getExeStatus():
                    continue
                return task
        return None

    def add_sleeping(self, task, wake_time):
        """Push a sleeping task onto the wake-time heap."""
        self._sleep_seq += 1
        heapq.heappush(self.sleeping, (wake_time, self._sleep_seq, task))

    def wake_sleeping(self, now):
        """
        Return every task whose scheduled wake time has arrived.

        Stale heap entries are ignored so cancelled or already-resumed tasks do
        not need to be eagerly removed from the heap.
        """
        ready = []
        while self.sleeping and self.sleeping[0][0] <= now:
            _, _, task = heapq.heappop(self.sleeping)
            if self.search(task.getID()) == -1:
                continue
            if task.done or task._blocked_reason != "sleep":
                continue
            ready.append(task)
        return ready

    def next_wake_time(self):
        """Peek at the next valid wake time, discarding stale heap entries."""
        while self.sleeping:
            wake_time, _, task = self.sleeping[0]
            if self.search(task.getID()) == -1 or task.done or task._blocked_reason != "sleep":
                heapq.heappop(self.sleeping)
                continue
            return wake_time
        return None

    def list(self):
        """Return a snapshot list of currently registered tasks."""
        return [task for task in self.tasks]

    def isOnlyWatchers(self):
        """Report whether every remaining task is marked as a watcher."""
        return len(self.tasks) == self.numWatchers

    def __len__(self):
        """Return the number of registered tasks."""
        return len(self.tasks)

    def __str__(self):
        """Return a newline-separated dump of all known tasks."""
        return "\n".join([str(x) for x in self.tasks])
