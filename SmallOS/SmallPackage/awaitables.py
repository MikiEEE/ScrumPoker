"""
Awaitable helpers for the native smallOS runtime.

The scheduler does not attempt to understand arbitrary Python awaitables.
Instead, every smallOS primitive returns an object whose ``__await__`` method
emits a ``TaskInstruction``. ``SmallOS`` consumes those instructions and decides
when the task should run again.

This is the key separation of responsibilities in the runtime:
- tasks describe what they want to wait for
- the scheduler decides when that wait is satisfied
"""


class TaskInstruction:
    """
    Minimal message object passed from a coroutine to the scheduler.

    ``operation`` names the requested scheduler action and ``payload`` holds the
    values needed to carry it out. Keeping this object tiny makes the protocol
    easier to debug and more realistic to port to constrained runtimes.
    """

    def __init__(self, operation, **payload):
        self.operation = operation
        self.payload = payload

    def __repr__(self):
        return "TaskInstruction(operation={!r}, payload={!r})".format(
            self.operation,
            self.payload,
        )


class _InstructionAwaitable:
    """
    Tiny awaitable wrapper shared by the public helpers below.

    The wrapped instruction is yielded once. Later, when ``SmallOS`` resumes the
    blocked task, the value sent back in becomes the return value of the
    ``await`` expression in user code.
    """

    def __init__(self, instruction):
        self.instruction = instruction

    def __await__(self):
        """Yield one scheduler instruction and then return the resume value."""
        result = yield self.instruction
        return result


def sleep_instruction(seconds):
    """Create the awaitable used for cooperative sleeping."""
    return _InstructionAwaitable(TaskInstruction("sleep", seconds=seconds))


def wait_signal_instruction(signal):
    """Create the awaitable used for waiting on a task signal."""
    return _InstructionAwaitable(TaskInstruction("wait_signal", signal=signal))


def yield_now_instruction():
    """Create the awaitable used for a voluntary scheduler yield."""
    return _InstructionAwaitable(TaskInstruction("yield_now"))


def join_instruction(target):
    """Create the awaitable used for waiting on a single task."""
    return _InstructionAwaitable(TaskInstruction("join", target=target))


def join_all_instruction(targets):
    """Create the awaitable used for waiting on several tasks at once."""
    return _InstructionAwaitable(TaskInstruction("join_all", targets=list(targets)))


def wait_readable_instruction(io_obj):
    """Create the awaitable used for waiting until an I/O object is readable."""
    return _InstructionAwaitable(TaskInstruction("wait_readable", io_obj=io_obj))


def wait_writable_instruction(io_obj):
    """Create the awaitable used for waiting until an I/O object is writable."""
    return _InstructionAwaitable(TaskInstruction("wait_writable", io_obj=io_obj))
