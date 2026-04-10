"""
Signal-facing helpers used by ``SmallTask``.

This module preserves the intent of the original signal API while adapting it
to the new native async runtime. The methods here do not perform scheduling on
their own; they only record signal state and return smallOS-owned awaitables.
``SmallOS`` later interprets those awaitables and moves tasks between queues.
"""

from .awaitables import (
    sleep_instruction,
    wait_readable_instruction,
    wait_signal_instruction,
    wait_writable_instruction,
    yield_now_instruction,
)


class SmallSignals:
    """
    Mix-in that gives tasks signal, sleep, and cooperative-yield helpers.

    Signal numbering notes:
    - Valid signal slots are ``0`` through ``31``.
    - The native async runtime currently reserves none of those slots for
      internal scheduler use.
    - The old generator/``waitOnAsync`` bridge used temporary internal meanings
      for signals ``5``, ``6``, and ``7``. Those meanings no longer apply after
      the native async runtime refactor.
    - In the current runtime, signal meanings are application-defined, so demos
      and app code should use named constants instead of bare integers.
    """

    SIGNAL_CAPACITY = 32
    CORE_SIGNAL_MEANINGS = {}
    LEGACY_SIGNAL_MEANINGS = {
        5: "Legacy waitOnAsync parent-wait signal from the pre-native-async runtime.",
        6: "Legacy waitOnAsync child-complete signal from the pre-native-async runtime.",
        7: "Legacy waitOnAsync all-children-complete signal from the pre-native-async runtime.",
    }

    def __init__(self, OS, kwargs):
        """
        Initialize per-task signal state.

        ``signals`` is a bit-vector so a task can notice a signal that arrived
        before it actually reached ``await wait_signal(...)``.
        """
        self.signals = [0] * self.SIGNAL_CAPACITY
        self.isWaiting = 0
        self.isSleep = 0
        self.wakeSigs = []
        self.sleepTime = 0
        self.timeOfSleep = 0
        self.handlers = None

        super().__init__()

        if kwargs and kwargs.get("handlers", False):
            self.handlers = kwargs["handlers"]

    def getSignals(self):
        """Return the list of currently latched signal numbers."""
        received = []
        for num, sig in enumerate(self.signals):
            if sig:
                received.append(num)
        return received

    @classmethod
    def describeSignal(cls, sig):
        """
        Return the documented meaning of a signal slot.

        This is mainly a convenience for debugging and future shell tooling.
        For the current runtime, most signals are intentionally application-
        defined rather than reserved by the scheduler.
        """
        if sig in cls.CORE_SIGNAL_MEANINGS:
            return cls.CORE_SIGNAL_MEANINGS[sig]
        if sig in cls.LEGACY_SIGNAL_MEANINGS:
            return cls.LEGACY_SIGNAL_MEANINGS[sig] + " Not used by the current runtime."
        return "Application-defined signal slot."

    def sendSignal(self, pid, sig):
        """
        Deliver a signal to another task by PID.

        The owning ``SmallOS`` instance is used as the routing layer so the
        destination task can be resumed immediately if it is actively blocked on
        that signal.
        """
        if not self.OS:
            return -1
        if sig < 0 or sig >= len(self.signals):
            return -1

        task = self.OS.tasks.search(pid)
        if task == -1:
            return -1

        task.acceptSignal(sig)
        return 0

    def acceptSignal(self, sig):
        """
        Latch an incoming signal and notify the scheduler.

        The scheduler callback is what turns a plain signal bit into an actual
        wake-up for tasks awaiting that specific signal.
        """
        if sig < 0 or sig >= len(self.signals):
            return -1

        self.signals[sig] = 1
        if self.OS:
            self.OS.on_signal(self, sig)

        if self.handlers:
            self.handlers(self)
        return 0

    def sleep(self, secs, state_blob=None):
        """
        Return the awaitable used for cooperative sleeping.

        ``state_blob`` is preserved for compatibility with the older API style,
        where suspension helpers could stash task-local state before yielding.
        """
        if state_blob is not None:
            self.state.update(state_blob)
        return sleep_instruction(secs)

    def wait_signal(self, sig, state_blob=None):
        """Return the awaitable used to wait until ``sig`` is delivered."""
        if state_blob is not None:
            self.state.update(state_blob)
        return wait_signal_instruction(sig)

    def sigSuspendV2(self, sig, state_blob=None):
        """Compatibility alias for the older generator-era suspension name."""
        return self.wait_signal(sig, state_blob)

    def yield_now(self):
        """Return the awaitable used for an explicit cooperative yield."""
        return yield_now_instruction()

    def wait_readable(self, io_obj):
        """Return the awaitable used to wait until ``io_obj`` is readable."""
        return wait_readable_instruction(io_obj)

    def wait_writable(self, io_obj):
        """Return the awaitable used to wait until ``io_obj`` is writable."""
        return wait_writable_instruction(io_obj)

    def wake(self):
        """Force the task back onto the ready queue if it belongs to an OS."""
        if self.OS:
            self.OS.resume_task(self)
        return

    def checkSignal(self, sig):
        """
        Consume and clear a previously received signal if it exists.

        This lets a task continue immediately if the signal happened before the
        task reached its wait point.
        """
        if self.signals[sig] == 1:
            self.signals[sig] = 0
            return True
        return False
