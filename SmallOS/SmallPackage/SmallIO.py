"""
Terminal-oriented I/O helpers for smallOS.

The runtime keeps application output and shell/OS output intentionally separate.
Normal task prints go through the app channel. When the shell terminal is open,
those app messages are buffered so shell output can stay readable. Switching
back to app view flushes the buffered output in order.
"""

from collections import deque


class SmallIO:
    """
    Buffered output helper used by ``SmallOS``.

    The class intentionally stays small, but it now exposes a few convenience
    methods for shell tooling:
    - inspect the current terminal mode
    - inspect buffered output
    - clear buffered output
    - flush buffered output
    """

    def __init__(self, buffer_length):
        """Set up the shell/app terminal split plus the app output buffer."""
        self.terminalToggle = False
        self.buffer_length = max(0, int(buffer_length))
        self.appPrintQueue = deque(maxlen=self.buffer_length) if self.buffer_length else deque()
        return

    def _coerce_message(self, *args):
        """Join arbitrary print arguments into one text payload."""
        return "".join(str(arg) for arg in args)

    def _write_direct(self, msg):
        """Write straight to the active kernel when one is attached."""
        if not getattr(self, "kernel", None):
            return False
        self.kernel.write(msg)
        return True

    def print(self, *args):
        """
        Write application output.

        When the shell view is active, app output is buffered instead of being
        shown immediately so shell commands remain readable.
        """
        msg = self._coerce_message(*args)
        if self.terminalToggle:
            if self.buffer_length:
                self.appPrintQueue.append(msg)
            return

        if not self._write_direct(msg) and self.buffer_length:
            self.appPrintQueue.append(msg)
        return

    def sPrint(self, *args, force=False):
        """
        Write shell or OS output.

        Shell output is normally visible only when the shell terminal is active.
        `force=True` is useful for scripted demos and shell prompts that should
        still be emitted while the shell is switching modes.
        """
        msg = self._coerce_message(*args)
        if force or self.terminalToggle:
            self._write_direct(msg)
        return

    def terminalStatus(self):
        """Return a small snapshot of the terminal/buffer state."""
        return {
            "terminal_visible": bool(self.terminalToggle),
            "buffered_messages": len(self.appPrintQueue),
            "buffer_length": self.buffer_length,
        }

    def getBufferedOutput(self):
        """Return a copy of the buffered app output."""
        return list(self.appPrintQueue)

    def clearBufferedOutput(self):
        """Drop every buffered app message and return how many were removed."""
        removed = len(self.appPrintQueue)
        self.appPrintQueue.clear()
        return removed

    def flushBufferedOutput(self):
        """Write all buffered app output immediately and return the flush count."""
        flushed = 0
        while self.appPrintQueue:
            msg = self.appPrintQueue.popleft()
            self._write_direct(msg)
            flushed += 1
        return flushed

    def setTerminalMode(self, enabled):
        """Explicitly switch between shell view and app view."""
        enabled = bool(enabled)
        if self.terminalToggle == enabled:
            return self.terminalStatus()

        self.terminalToggle = enabled
        self._write_direct("*" * 16 + "\n")
        if not self.terminalToggle:
            self.flushBufferedOutput()
        return self.terminalStatus()

    def toggleTerminal(self):
        """Toggle between shell view and app view."""
        return self.setTerminalMode(not self.terminalToggle)
