"""
Task object for the native smallOS coroutine runtime.

Each ``SmallTask`` owns the per-coroutine state that used to be split between
generator helpers and ``asyncio`` tasks: pending resume values, terminal
results, exceptions, and join bookkeeping. The scheduler can stay relatively
small because most task-local lifecycle details live here.
"""

import inspect

from .awaitables import join_instruction, join_all_instruction
from .SmallErrors import PIDError, TaskCancelledError
from .SmallSignals import SmallSignals
from .list_util.linkedList import Node
from .TaskState import TaskState


_MISSING = object()


class SmallTask(SmallSignals, Node):
    """
    Priority-scheduled unit of execution managed by ``SmallOS``.

    The important design change from the old hybrid runtime is that a task now
    stores and drives its coroutine directly. That keeps resume timing and
    priority ordering under smallOS control instead of delegating that behavior
    to ``asyncio``.
    """

    def __init__(self, priority, routine, **kwargs):
        """
        Create a task shell around a routine or coroutine object.

        ``routine`` is usually an ``async def`` function that accepts the task as
        its first argument. Extra user arguments are stored in ``args`` and are
        applied lazily the first time the task is stepped.
        """
        self.pid = -1
        self.priority = priority
        self.isReady = 1
        self.isLocked = 0
        self.isWatcher = False
        self.parent = None
        self.OS = None
        self.state = TaskState()
        self.children = []
        self.name = ""
        self.args = ()
        self.updateFunc = None
        self.routine = routine

        self._coroutine = None
        self._done = False
        self._result = None
        self._exception = None
        self._queued = False
        self._blocked_reason = None
        self._wake_at = None
        self._pending_send = _MISSING
        self._pending_throw = None
        self._waiting_signal = None
        self._join_target = None
        self._join_targets = None
        self._join_pending = set()
        self._join_waiters = []
        self._io_wait_obj = None
        self._io_wait_mode = None

        self.state.update({"return_status": 0}, "system")

        SmallSignals.__init__(self, self.OS, kwargs)

        if kwargs:
            if "name" in kwargs:
                self.name = kwargs["name"]
            if "update" in kwargs:
                self.updateFunc = kwargs["update"]
            if "parent" in kwargs:
                self.parent = kwargs["parent"]
            if "isReady" in kwargs:
                self.isReady = kwargs["isReady"]
            if "isWatcher" in kwargs:
                self.isWatcher = kwargs["isWatcher"]
            if "args" in kwargs:
                self.args = kwargs["args"]

    @property
    def done(self):
        """Whether the task has reached a terminal state."""
        return self._done

    @property
    def result(self):
        """Return the stored result after successful completion."""
        return self._result

    @property
    def exception(self):
        """Return the stored terminal exception, if any."""
        return self._exception

    def _invoke_routine(self):
        """Call the user routine using the task's stored argument convention."""
        if self.args == ():
            return self.routine(self)
        if isinstance(self.args, tuple):
            return self.routine(self, *self.args)
        if isinstance(self.args, list):
            return self.routine(self, *self.args)
        if isinstance(self.args, dict):
            return self.routine(self, **self.args)
        return self.routine(self, self.args)

    def _ensure_coroutine(self):
        """
        Materialize the coroutine the first time the task actually runs.

        This lazy setup is intentional: spawning a task should register work, not
        execute user code immediately.
        """
        if self._coroutine is not None or self._done:
            return

        if self.routine is None:
            self.complete(None)
            return

        if inspect.isawaitable(self.routine) and not callable(self.routine):
            candidate = self.routine
        else:
            candidate = self._invoke_routine()

        if inspect.isawaitable(candidate):
            if hasattr(candidate, "send") and hasattr(candidate, "throw"):
                self._coroutine = candidate
            else:
                self._coroutine = candidate.__await__()
            return

        self.complete(candidate)

    def execute(self):
        """
        Advance the task by exactly one scheduler step.

        The scheduler either sends a resume value back in, throws an exception
        back in, or starts the coroutine for the first time. Whatever the
        coroutine yields is handed back to ``SmallOS`` for interpretation.
        """
        if not self.getExeStatus():
            return None

        self.isReady = 0
        self._ensure_coroutine()
        if self._done:
            return None

        try:
            if self._pending_throw is not None:
                # Resume by injecting an event such as cancellation or a failed
                # joined child back into the coroutine.
                exc = self._pending_throw
                self._pending_throw = None
                yielded = self._coroutine.throw(exc)
            else:
                send_value = None
                if self._pending_send is not _MISSING:
                    # Sleep/signal/join completions resume the coroutine with a
                    # value, which becomes the result of the awaited call.
                    send_value = self._pending_send
                    self._pending_send = _MISSING
                yielded = self._coroutine.send(send_value)
        except StopIteration as stop:
            self.complete(stop.value)
            return None
        except Exception as exc:
            self.fail(exc)
            return None

        return yielded

    def excecute(self):
        """Compatibility alias for the project's historical misspelling."""
        return self.execute()

    def update(self):
        """Run an optional readiness callback used by legacy task styles."""
        if self.updateFunc:
            if self.updateFunc(self) == 1:
                self.isReady = 1
            return 0
        return -1

    def complete(self, result):
        """Mark the task as successfully finished and store its result."""
        self._done = True
        self._result = result
        self.isReady = 0
        self.isWaiting = 0
        self.isSleep = 0
        self.state.update({"return_status": 0, "result": result}, "system")
        return result

    def fail(self, exc):
        """Mark the task as failed and store its terminal exception."""
        self._done = True
        self._exception = exc
        self.isReady = 0
        self.isWaiting = 0
        self.isSleep = 0
        self.state.update({"return_status": -1, "exception": exc}, "system")
        return exc

    def cancel(self, message="Task cancelled"):
        """
        Force the task into a cancelled terminal state.

        If the coroutine exists we close it first so we do not keep stale frames
        alive after the runtime has decided this task is done.
        """
        if self._done:
            return

        if self._coroutine is not None:
            try:
                self._coroutine.close()
            except RuntimeError:
                pass
        self.fail(TaskCancelledError(message))

    def resume(self, value=_MISSING, exc=None):
        """
        Prepare the task to run again after a wait condition completes.

        ``value`` is sent into the coroutine on the next step. ``exc`` is thrown
        into it instead. The scheduler chooses which of those channels to use
        and is responsible for clearing any wait metadata beforehand.
        """
        self.isReady = 1
        self.isWaiting = 0
        self.isSleep = 0
        self._pending_send = value
        self._pending_throw = exc

    def block(self, reason):
        """Record why the task is no longer runnable."""
        self._blocked_reason = reason
        self.isReady = 0
        self.isWaiting = 1 if reason in ("signal", "join", "join_all") else 0
        self.isSleep = 1 if reason == "sleep" else 0
        self.state.update({"return_status": 1, "blocked_reason": reason}, "system")

    def setID(self, pid):
        """Assign the PID chosen by ``SmallOS`` exactly once."""
        if isinstance(pid, int):
            if self.pid == -1:
                self.pid = pid
            else:
                raise PIDError("PID can only be set once.")
        else:
            raise TypeError("PID must be type Int")
        return

    def getID(self):
        """Return the task PID."""
        return self.pid

    def setOS(self, OS):
        """Attach the task to its owning runtime."""
        self.OS = OS

    def build(self, priority, task, ready=1, name="", parent=None):
        """Compatibility helper used by legacy code paths."""
        return SmallTask(
            priority,
            task,
            isReady=ready,
            name=name,
            parent=parent,
        )

    def spawn(self, routine, priority=None, **kwargs):
        """
        Create and register a child task on the same runtime.

        The returned object is the child task itself so callers can hand it
        directly to ``await task.join(...)`` or ``await task.join_all(...)``.
        """
        if not self.OS:
            raise RuntimeError("Task must belong to an OS before it can spawn children.")

        child = routine
        if not isinstance(child, SmallTask):
            child = SmallTask(priority or self.priority, routine, **kwargs)
        elif priority is not None:
            child.priority = priority

        child.parent = self
        self.OS.fork(child)
        self.children.append(child.getID())
        return child

    def fork(self, new_task):
        """Compatibility wrapper that returns the new child PID."""
        child = self.spawn(new_task)
        return child.getID()

    def join(self, child):
        """Return the awaitable used to wait for one child task."""
        return join_instruction(child)

    def join_all(self, children):
        """Return the awaitable used to wait for several child tasks."""
        return join_all_instruction(children)

    def add_join_waiter(self, waiter):
        """Register a task that is currently waiting on this task."""
        if waiter not in self._join_waiters:
            self._join_waiters.append(waiter)

    def discard_join_waiter(self, waiter):
        """Remove a waiter once it no longer depends on this task."""
        while waiter in self._join_waiters:
            self._join_waiters.remove(waiter)

    def kill(self, flags=None):
        """Request scheduler-managed cancellation of this task."""
        flags = flags or {}
        if not self.OS:
            self.cancel()
            return -1
        return self.OS.cancel_task(self, recursive="-r" in flags)

    def getExeStatus(self):
        """Report whether the scheduler may run this task right now."""
        return bool(self.isReady) and not self.isLocked and not self._done

    def getDelStatus(self):
        """Report whether the task is terminal and removable."""
        return self._done and not self.isWatcher

    def stat(self):
        """Return an expanded debug dump for shell-style inspection."""
        msg = "\nisReady={}\nisWaiting={}\nisSleep={}\n".format(
            self.isReady,
            self.isWaiting,
            self.isSleep,
        )
        msg += "blocked_reason={}\n".format(self._blocked_reason)
        msg += "done={}\n".format(self.done)
        if self.exception is not None:
            msg += "exception={!r}\n".format(self.exception)
        return str(self) + msg

    def __str__(self):
        """Return a compact single-line summary of the task state."""
        name = self.name or "Unamed Process"
        return (
            "PID={}, name={}, priority={}, ExeStatus={}, DelStatus={}, blocked={}, done={}"
        ).format(
            self.pid,
            name,
            self.priority,
            self.getExeStatus(),
            self.getDelStatus(),
            self._blocked_reason,
            self.done,
        )
