"""
Native smallOS scheduler.

This module is the core of the async runtime. Tasks yield ``TaskInstruction``
objects, the scheduler interprets them, and tasks are resumed according to
smallOS priority rules rather than ``asyncio``'s event loop behavior.

That separation is what makes the project-specific features possible:
- custom priorities
- explicit signal integration
- a runtime shape that is easier to port to MicroPython
"""

from .awaitables import TaskInstruction
from .SmallIO import SmallIO
from .SmallConfig import SmallOSConfig
from .OSlist import OSList
from .SmallErrors import MaxProcessError, UnsupportedAwaitableError


_MISSING = object()


class SmallOS(SmallIO):
    """
    Cooperative event loop for ``SmallTask`` coroutines.

    The scheduler deliberately stays narrow in scope: pick a runnable task,
    advance it once, interpret the yielded instruction, and update queues.
    Keeping that control flow explicit makes the runtime easier to understand
    and easier to customize.
    """

    def __init__(self, size=None, config=None, **kwargs):
        """
        Create the runtime shell plus its task and shell registries.

        ``config`` may be a ``SmallOSConfig`` instance or a plain dict. Older
        constructor-style overrides such as ``size`` still work and take
        precedence over the loaded config values when both are supplied.
        """
        config_overrides = {}
        if size is not None:
            config_overrides["task_capacity"] = size
        if "priority_levels" in kwargs:
            config_overrides["priority_levels"] = kwargs.pop("priority_levels")
        if "io_buffer_length" in kwargs:
            config_overrides["io_buffer_length"] = kwargs.pop("io_buffer_length")
        if "eternal_watchers" in kwargs:
            config_overrides["eternal_watchers"] = kwargs.pop("eternal_watchers")

        self.config = SmallOSConfig.from_dict(config)
        if config_overrides:
            self.config = self.config.copy(**config_overrides)

        self.sleepTasks = []
        self.waitingTasks = []
        self.wakeUpdate = []
        self.ioReadWaiters = {}
        self.ioWriteWaiters = {}
        self.shells = []
        self.tasks = OSList(self.config.priority_levels, self.config.task_capacity)
        self.kernel = None
        self.eternalWatchers = self.config.eternal_watchers
        self.cursor = None

        SmallIO.__init__(self, self.config.io_buffer_length)
        if kwargs:
            if kwargs.get("tasks", False):
                self.fork(kwargs["tasks"])
            if kwargs.get("shells", False):
                shells = kwargs["shells"]
                if isinstance(shells, list):
                    [shell.setOS(self) for shell in shells]
                    self.shells.extend(shells)
                else:
                    shells.setOS(self)
                    self.shells.append(shells)

    def startOS(self):
        """Compatibility entrypoint kept from the earlier project API."""
        return self.start()

    def start(self):
        """
        Run the scheduler until no live tasks remain.

        Each pass wakes expired sleepers, selects the highest-priority runnable
        task, advances it once, and then either finalizes it or handles the wait
        condition it requested.
        """
        while len(self.tasks) != 0:
            self._wake_sleeping_tasks()
            self._wake_io_tasks(timeout_ms=0)
            self.cursor = self.tasks.pop()

            if self.cursor is None:
                if not self._idle_until_next_task():
                    break
                continue

            yielded = self.cursor.execute()
            if self.cursor.done:
                # Finished tasks are finalized immediately so PID lookup and join
                # bookkeeping always see a consistent terminal state.
                self._finalize_task(self.cursor)
            else:
                self._handle_yield(self.cursor, yielded)

            if not self.eternalWatchers and len(self.tasks) != 0 and self.tasks.isOnlyWatchers():
                return
        return

    def next(self):
        """Return the next runnable task without advancing the main loop."""
        self.cursor = self.tasks.pop()
        return self.cursor

    def fork(self, children):
        """Register one task or a list of tasks with the runtime."""
        if isinstance(children, list):
            ids = []
            for item in children:
                ids.append(self._fork_one(item))
            return ids
        return self._fork_one(children)

    def _fork_one(self, task):
        """Assign a PID, attach the runtime, and enqueue the task if runnable."""
        pid = self.tasks.insert(task)
        if pid == -1:
            raise MaxProcessError("All available PIDS are in use, cannot add more tasks.")

        task.setOS(self)
        if task.getExeStatus():
            self.tasks.enqueue(task)
        return pid

    def setKernel(self, kernel):
        """Attach the platform abstraction used for time and output."""
        self.kernel = kernel
        return self

    def setEternalWatchers(self, isEternalWatcherPresent):
        """Control whether the runtime exits once only watcher tasks remain."""
        self.eternalWatchers = isEternalWatcherPresent
        return self

    def _wake_sleeping_tasks(self):
        """Promote every expired sleeping task back onto the ready queues."""
        if not self.kernel:
            return

        for task in self.tasks.wake_sleeping(self.kernel.scheduler_now_ms()):
            self.resume_task(task)

    def _idle_until_next_task(self):
        """
        Sleep the host kernel until the next known wake-up time.

        This avoids busy-looping when every live task is blocked on time rather
        than CPU. If no kernel or future wake time exists, there is nothing
        useful to wait for and the scheduler should stop.
        """
        if not self.kernel:
            return False

        next_wake = self.tasks.next_wake_time()
        timeout = None
        if next_wake is not None:
            timeout = max(0, next_wake - self.kernel.scheduler_now_ms())

        has_io_waiters = bool(self.ioReadWaiters or self.ioWriteWaiters)
        if next_wake is None and not has_io_waiters:
            return False

        if has_io_waiters and hasattr(self.kernel, "io_wait"):
            self._wake_io_tasks(timeout_ms=timeout)
        elif timeout is not None and timeout > 0 and hasattr(self.kernel, "sleep_ms"):
            self.kernel.sleep_ms(timeout)
        return True

    def resume_task(self, task, value=_MISSING, exc=None, front=False):
        """
        Requeue a blocked task with the value or exception that completed it.

        Centralizing resume logic here ensures stale join registrations are
        cleared before a task gets a chance to block on something else.
        """
        if task is None or task == -1 or task.done:
            return -1
        if self.tasks.search(task.getID()) == -1:
            return -1

        self._clear_wait_registration(task)
        task.resume(value=value, exc=exc)
        self.tasks.enqueue(task, front=front)
        return 0

    def on_signal(self, task, sig):
        """Wake a task immediately if it is actively waiting on ``sig``."""
        if task._blocked_reason == "signal" and task._waiting_signal == sig:
            task.signals[sig] = 0
            self.resume_task(task, value=sig, front=True)

    def _handle_yield(self, task, yielded):
        """
        Interpret one scheduler instruction emitted by a task.

        This method is the policy hub of the runtime. Every supported ``await``
        ends up in one branch here, which makes task state transitions explicit.
        """
        if yielded is None:
            # Treat bare ``None`` as an immediate cooperative yield so the task
            # remains runnable instead of getting stranded.
            self.resume_task(task)
            return

        if not isinstance(yielded, TaskInstruction):
            task.fail(
                UnsupportedAwaitableError(
                    "Task {!r} yielded unsupported awaitable {!r}".format(task.name, yielded)
                )
            )
            self._finalize_task(task)
            return

        operation = yielded.operation
        payload = yielded.payload

        if operation == "yield_now":
            # Voluntary CPU handoff: keep the task runnable at the same
            # priority, but let other tasks get a turn first.
            self.resume_task(task)
            return

        if operation == "sleep":
            seconds = payload.get("seconds", 0)
            if seconds < 0:
                task.fail(ValueError("sleep duration must be non-negative"))
                self._finalize_task(task)
                return

            delay_ms = max(0, int(seconds * 1000))
            wake_time = self.kernel.scheduler_now_ms() + delay_ms if self.kernel else delay_ms
            task.block("sleep")
            task._wake_at = wake_time
            self.tasks.add_sleeping(task, wake_time)
            return

        if operation == "wait_signal":
            signal = payload["signal"]
            if task.checkSignal(signal):
                self.resume_task(task, value=signal, front=True)
            else:
                task.block("signal")
                task._waiting_signal = signal
            return

        if operation == "wait_readable":
            task.block("wait_readable")
            task._io_wait_obj = payload["io_obj"]
            task._io_wait_mode = "read"
            self._register_io_wait(task, payload["io_obj"], "read")
            return

        if operation == "wait_writable":
            task.block("wait_writable")
            task._io_wait_obj = payload["io_obj"]
            task._io_wait_mode = "write"
            self._register_io_wait(task, payload["io_obj"], "write")
            return

        if operation == "join":
            target = self._resolve_task(payload["target"])
            if target is None:
                task.fail(LookupError("join target does not exist"))
                self._finalize_task(task)
                return

            if target.done:
                self._resume_from_completed(task, target)
            else:
                task.block("join")
                task._join_target = target
                target.add_join_waiter(task)
            return

        if operation == "join_all":
            targets = self._normalize_targets(payload["targets"])
            if targets is None:
                task.fail(LookupError("join_all target does not exist"))
                self._finalize_task(task)
                return

            if not targets:
                self.resume_task(task, value=[], front=True)
                return

            first_exception = self._first_exception(targets)
            if first_exception is not None:
                self.resume_task(task, exc=first_exception, front=True)
                return

            pending = {child.getID() for child in targets if not child.done}
            if not pending:
                self.resume_task(task, value=[child.result for child in targets], front=True)
                return

            # Preserve the original child ordering for the eventual results
            # while also tracking a fast set of outstanding child PIDs.
            task.block("join_all")
            task._join_targets = targets
            task._join_pending = pending
            for child in targets:
                if not child.done:
                    child.add_join_waiter(task)
            return

        task.fail(UnsupportedAwaitableError("Unknown instruction {!r}".format(operation)))
        self._finalize_task(task)

    def _resolve_task(self, target):
        """Normalize either a task object or a PID to a task object."""
        if hasattr(target, "getID"):
            return target
        return self.tasks.search(target)

    def _normalize_targets(self, targets):
        """Resolve a join target list while preserving caller-specified order."""
        normalized = []
        seen = set()
        for target in targets:
            task = self._resolve_task(target)
            if task is None or task == -1:
                return None
            if task.getID() in seen:
                continue
            seen.add(task.getID())
            normalized.append(task)
        return normalized

    def _resume_from_completed(self, waiter, target):
        """Resume a join waiter with either the child result or its exception."""
        if target.exception is not None:
            self.resume_task(waiter, exc=target.exception, front=True)
        else:
            self.resume_task(waiter, value=target.result, front=True)

    def _first_exception(self, tasks):
        """Return the first terminal exception in a task list, if any."""
        for task in tasks:
            if task.exception is not None:
                return task.exception
        return None

    def _register_io_wait(self, task, io_obj, mode):
        """Register a task as waiting on an I/O object's readiness event."""
        waiters = self.ioReadWaiters if mode == "read" else self.ioWriteWaiters
        if io_obj not in waiters:
            waiters[io_obj] = []
        if task not in waiters[io_obj]:
            waiters[io_obj].append(task)

    def _wake_io_tasks(self, timeout_ms=0):
        """
        Ask the kernel which I/O objects are ready and resume their waiters.

        This keeps the runtime single-threaded: tasks suspend on readiness
        events and the scheduler wakes them when the kernel reports the socket
        or stream can make progress.
        """
        if not self.kernel or not hasattr(self.kernel, "io_wait"):
            return
        if not self.ioReadWaiters and not self.ioWriteWaiters:
            return

        readable, writable = self.kernel.io_wait(
            list(self.ioReadWaiters.keys()),
            list(self.ioWriteWaiters.keys()),
            timeout_ms,
        )
        self._resume_io_waiters(readable, self.ioReadWaiters)
        self._resume_io_waiters(writable, self.ioWriteWaiters)

    def _resume_io_waiters(self, ready_objects, waiters_map):
        """Resume every task waiting on the now-ready I/O objects."""
        for io_obj in ready_objects:
            waiters = waiters_map.pop(io_obj, [])
            for waiter in waiters:
                if waiter.done or self.tasks.search(waiter.getID()) == -1:
                    continue
                self.resume_task(waiter, value=io_obj, front=True)

    def _clear_wait_registration(self, task):
        """
        Remove a task from any join bookkeeping it currently participates in.

        This prevents leaked waiter references when a task is resumed,
        cancelled, or moved from one wait condition to another.
        """
        if task._join_target is not None:
            task._join_target.discard_join_waiter(task)
            task._join_target = None

        if task._join_targets:
            for child in task._join_targets:
                child.discard_join_waiter(task)
            task._join_targets = None
            task._join_pending = set()

        if task._io_wait_obj is not None and task._io_wait_mode is not None:
            waiters_map = self.ioReadWaiters if task._io_wait_mode == "read" else self.ioWriteWaiters
            waiters = waiters_map.get(task._io_wait_obj, [])
            while task in waiters:
                waiters.remove(task)
            if not waiters and task._io_wait_obj in waiters_map:
                del waiters_map[task._io_wait_obj]
            task._io_wait_obj = None
            task._io_wait_mode = None

    def _detach_from_parent(self, task):
        """Remove a finished child PID from its parent's child list."""
        parent = task.parent
        if not parent or parent == -1 or not hasattr(parent, "children"):
            return
        while task.getID() in parent.children:
            parent.children.remove(task.getID())

    def _notify_waiters(self, task):
        """
        Wake every task that was blocked on this task's completion.

        ``join`` waiters receive a single result or exception. ``join_all``
        waiters either fail fast on the first child exception or resume when all
        children have finished.
        """
        waiters = list(task._join_waiters)
        task._join_waiters = []

        for waiter in waiters:
            if waiter.done:
                continue
            if self.tasks.search(waiter.getID()) == -1:
                continue

            if waiter._blocked_reason == "join" and waiter._join_target is task:
                waiter._join_target = None
                self._resume_from_completed(waiter, task)
                continue

            if waiter._blocked_reason == "join_all" and waiter._join_targets:
                if task.exception is not None:
                    # ``join_all`` behaves like structured concurrency here:
                    # one child failure wakes the parent immediately.
                    self._clear_wait_registration(waiter)
                    self.resume_task(waiter, exc=task.exception, front=True)
                    continue

                waiter._join_pending.discard(task.getID())
                if not waiter._join_pending:
                    results = [child.result for child in waiter._join_targets]
                    self._clear_wait_registration(waiter)
                    self.resume_task(waiter, value=results, front=True)

    def _finalize_task(self, task):
        """Run the full shutdown sequence for a finished or cancelled task."""
        self._clear_wait_registration(task)
        self._notify_waiters(task)
        self._detach_from_parent(task)
        self.tasks.delete(task.getID())

    def cancel_task(self, task, recursive=False):
        """Cancel a task by object or PID and optionally cancel its descendants."""
        target = self._resolve_task(task)
        if target is None or target == -1:
            return -1

        if recursive:
            for child_id in list(target.children):
                child = self.tasks.search(child_id)
                if child != -1:
                    self.cancel_task(child, recursive=True)

        # Remove any active wait registrations before cancellation resets task
        # bookkeeping fields. This prevents stale I/O waiters from surviving
        # after the task is gone.
        self._clear_wait_registration(target)
        target.cancel()
        self._finalize_task(target)
        return 0

    def __str__(self):
        """Return a human-readable dump of the currently registered tasks."""
        all_tasks = list(self.tasks.tasks)
        string = "SmallOS\n"
        for count, routine in enumerate(all_tasks):
            string += str(count + 1) + ". " + str(routine) + "\n"
        return string
