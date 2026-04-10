"""
Shell helpers for smallOS.

The runtime itself is intentionally small and scheduler-focused. This module
adds a light command shell on top so users can inspect task state, send
signals, toggle the shell/app terminal view, and run small debugging snippets
without reaching into runtime internals manually.

The shell is designed around string commands instead of a blocking input loop.
That keeps it useful in three different settings:
- interactive desktop experiments
- scripted demo sessions running alongside other tasks
- future embedded shells that source input from UART, REPL glue, or custom UIs

Parsing and output flow deliberately go through the kernel layer when
available. That keeps platform-sensitive behavior such as shell tokenization
out of the shell module itself and makes it easier to keep the shell portable
to MicroPython targets.
"""

from .SmallSignals import SmallSignals


class ShellCommandError(Exception):
    """Raised when a shell command cannot be parsed or completed."""


class BaseShell:
    """
    Small command shell for runtime inspection and control.

    The shell owns a command registry so new commands can be added without
    modifying the parsing loop. Built-in commands intentionally stay close to
    smallOS concepts such as tasks, signals, buffered output, and terminal
    mode.
    """

    def __init__(self, prompt="smallos> ", allow_python=True):
        self.prompt = prompt
        self.allow_python = allow_python
        self.OS = None
        self.is_running = True
        self.locals = {"shell": self}
        self.commands = {}
        self.aliases = {}
        self._command_help = {}
        self._register_builtin_commands()

    def _register_command(self, name, handler, help_text, aliases=()):
        """Register one command handler plus any short aliases."""
        self.commands[name] = handler
        self._command_help[name] = help_text
        for alias in aliases:
            self.aliases[alias] = name

    def _register_builtin_commands(self):
        """Install the default runtime-inspection command set."""
        self._register_command("help", self.command_help, "Show all commands or detailed help for one command.", aliases=("?",))
        self._register_command("toggle", self.command_toggle, "Toggle between app output and shell output views.", aliases=("sw",))
        self._register_command("ps", self.command_ps, "List every registered task.")
        self._register_command("stat", self.command_stat, "Show OS or task details. Usage: stat [pid]")
        self._register_command("count", self.command_count, "Show the number of registered tasks.")
        self._register_command("kill", self.command_kill, "Cancel a task. Usage: kill <pid> [-r]")
        self._register_command("signal", self.command_signal, "Send a signal to a task. Usage: signal <pid> <sig>", aliases=("send",))
        self._register_command("signals", self.command_signals, "Show pending signals for a task. Usage: signals <pid>")
        self._register_command("children", self.command_children, "List child tasks for a PID. Usage: children <pid>")
        self._register_command("io", self.command_io, "Inspect or manage buffered output. Usage: io [status|show|flush|clear]")
        self._register_command("echo", self.command_echo, "Write text through the shell output channel.")
        self._register_command("python", self.command_python, "Execute Python in the shell context. Usage: python <expr/code>", aliases=("exec",))
        self._register_command("exit", self.command_exit, "Stop the shell session.", aliases=("quit",))

    def setOS(self, OS):
        """Attach the shell to its owning runtime."""
        self.OS = OS
        self.locals["OS"] = OS
        self.locals["kernel"] = self._kernel()
        return self

    def _kernel(self):
        """Return the active runtime kernel when one is available."""
        if self.OS and getattr(self.OS, "kernel", None):
            return self.OS.kernel
        return None

    def write(self, *args, force=False):
        """Write text either through the runtime shell channel or the kernel."""
        message = "".join(str(arg) for arg in args)
        if self.OS and hasattr(self.OS, "sPrint"):
            self.OS.sPrint(message, force=force)
            return

        kernel = self._kernel()
        if kernel and hasattr(kernel, "write"):
            kernel.write(message)
            return

        print(message, end="")
        return

    def _split_command(self, line):
        """Tokenize one command line through the active kernel."""
        kernel = self._kernel()
        if kernel and hasattr(kernel, "shell_split"):
            try:
                return kernel.shell_split(line)
            except ValueError as exc:
                raise ShellCommandError(str(exc))
        return line.split()

    def prompt_user(self, force=False):
        """Emit the shell prompt."""
        self.write(self.prompt, force=force)
        return

    async def run_stdin_loop(
        self,
        task,
        stdin_obj=None,
        poll_interval=0.1,
        show_banner=True,
        banner_text=None,
        prompt_on_start=True,
        force_output=True,
        echo_commands=False,
    ):
        """
        Run an interactive shell loop sourced from stdin-like input.

        This is designed for demos and local tooling where we want web/server
        tasks and shell commands to coexist in one cooperative scheduler.
        """
        if self.OS is None and getattr(task, "OS", None) is not None:
            self.setOS(task.OS)

        kernel = self._kernel()
        if stdin_obj is None:
            stdin_obj = getattr(getattr(kernel, "_sys", None), "stdin", None)

        if stdin_obj is None or not hasattr(stdin_obj, "readline"):
            self.write("shell stdin watcher unavailable on this kernel\n", force=True)
            return "shell stdin unavailable"

        if show_banner:
            text = banner_text
            if text is None:
                text = (
                    "\nInteractive shell enabled.\n"
                    "Type commands like: help, count, ps, stat <pid>, kill <pid>\n"
                )
            self.write(text, force=force_output)

        if prompt_on_start and self.is_running:
            self.prompt_user(force=force_output)

        while self.is_running:
            if hasattr(stdin_obj, "fileno"):
                await task.wait_readable(stdin_obj)
            else:
                await task.sleep(poll_interval)

            line = stdin_obj.readline()
            if line is None:
                await task.sleep(poll_interval)
                continue

            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")

            if line == "":
                # EOF or no complete line available yet.
                await task.sleep(poll_interval)
                continue

            self.run(
                line,
                show_prompt=True,
                echo_command=echo_commands,
                force_output=force_output,
            )

        return "shell session closed"

    def make_task(
        self,
        priority=3,
        name="shell_stdin",
        is_watcher=True,
        stdin_obj=None,
        poll_interval=0.1,
        show_banner=True,
        banner_text=None,
        prompt_on_start=True,
        force_output=True,
        echo_commands=False,
        **task_kwargs
    ):
        """
        Build a ready-to-fork ``SmallTask`` that runs this shell's stdin loop.

        Typical usage:
        - ``shell = BaseShell().setOS(runtime)``
        - ``shell_task = shell.make_task(priority=3)``
        - ``runtime.fork([shell_task, ...])``
        """
        from .SmallTask import SmallTask

        async def _shell_task_routine(task):
            return await self.run_stdin_loop(
                task,
                stdin_obj=stdin_obj,
                poll_interval=poll_interval,
                show_banner=show_banner,
                banner_text=banner_text,
                prompt_on_start=prompt_on_start,
                force_output=force_output,
                echo_commands=echo_commands,
            )

        if "name" not in task_kwargs:
            task_kwargs["name"] = name
        if "isWatcher" not in task_kwargs:
            task_kwargs["isWatcher"] = is_watcher

        return SmallTask(priority, _shell_task_routine, **task_kwargs)

    def run(self, line, show_prompt=True, echo_command=False, force_output=False):
        """
        Parse and execute one shell command line.

        `echo_command` is useful for scripted demos, where it helps show what
        the shell is "typing" while other tasks keep running.
        """
        line = (line or "").strip()
        if not line:
            if show_prompt:
                self.prompt_user(force=force_output)
            return None

        if echo_command:
            self.write(self.prompt, line, "\n", force=force_output)

        try:
            tokens = self._split_command(line)
            response = self.dispatch(tokens)
        except ShellCommandError as exc:
            self.write("error: ", str(exc), "\n", force=True)
            response = None

        if response:
            self.write(response, "\n", force=force_output)
        if show_prompt and self.is_running:
            self.prompt_user(force=force_output)
        return response

    def dispatch(self, tokens):
        """Dispatch one already-tokenized command."""
        if not tokens:
            return None

        name = self.aliases.get(tokens[0], tokens[0])
        handler = self.commands.get(name)
        if handler is None:
            raise ShellCommandError("unknown command {!r}. Try 'help'.".format(tokens[0]))
        return handler(tokens[1:])

    def _require_os(self):
        """Raise a friendly error when the shell is not attached to a runtime."""
        if not self.OS:
            raise ShellCommandError("shell is not attached to a running SmallOS instance")
        return self.OS

    def _parse_pid(self, value):
        """Parse one PID argument."""
        try:
            pid = int(value)
        except (TypeError, ValueError):
            raise ShellCommandError("PID must be an integer")
        return pid

    def _parse_signal(self, value):
        """Parse and validate one signal number."""
        try:
            sig = int(value)
        except (TypeError, ValueError):
            raise ShellCommandError("signal must be an integer")

        if not 0 <= sig < SmallSignals.SIGNAL_CAPACITY:
            raise ShellCommandError(
                "signal must be between 0 and {}".format(SmallSignals.SIGNAL_CAPACITY - 1)
            )
        return sig

    def _get_task(self, pid):
        """Look up one task by PID and raise a shell-friendly error if missing."""
        task = self._require_os().tasks.search(pid)
        if task == -1:
            raise ShellCommandError("no task with PID {}".format(pid))
        return task

    def command_help(self, args):
        """Show top-level help or help for one command."""
        if not args:
            lines = ["Available commands:"]
            for name in sorted(self.commands):
                aliases = sorted(alias for alias, target in self.aliases.items() if target == name)
                alias_text = " (aliases: {})".format(", ".join(aliases)) if aliases else ""
                lines.append("- {}{}: {}".format(name, alias_text, self._command_help[name]))
            return "\n".join(lines)

        name = self.aliases.get(args[0], args[0])
        if name not in self.commands:
            raise ShellCommandError("unknown command {!r}".format(args[0]))
        aliases = sorted(alias for alias, target in self.aliases.items() if target == name)
        alias_text = "\nAliases: {}".format(", ".join(aliases)) if aliases else ""
        return "{}\n{}{}".format(name, self._command_help[name], alias_text)

    def command_toggle(self, args):
        """Toggle shell/app output visibility."""
        status = self._require_os().toggleTerminal()
        if status["terminal_visible"]:
            return "shell view enabled"
        return "application view enabled"

    def command_ps(self, args):
        """List all currently registered tasks."""
        tasks = self._require_os().tasks.list()
        if not tasks:
            return "no tasks registered"
        return "\n".join(str(task) for task in tasks)

    def command_stat(self, args):
        """Show either the OS dump or one task's expanded state."""
        os_ref = self._require_os()
        if not args:
            return str(os_ref)
        task = self._get_task(self._parse_pid(args[0]))
        return task.stat()

    def command_count(self, args):
        """Show task count and watcher count."""
        os_ref = self._require_os()
        return "{} task(s) registered, {} watcher(s)".format(len(os_ref.tasks), os_ref.tasks.numWatchers)

    def command_kill(self, args):
        """Cancel one task, optionally including its descendants."""
        if not args:
            raise ShellCommandError("usage: kill <pid> [-r]")

        pid = self._parse_pid(args[0])
        recursive = "-r" in args[1:]
        task = self._get_task(pid)
        self._require_os().cancel_task(task, recursive=recursive)
        return "cancelled PID {}{}".format(pid, " recursively" if recursive else "")

    def command_signal(self, args):
        """Deliver one signal to a target task."""
        if len(args) != 2:
            raise ShellCommandError("usage: signal <pid> <sig>")

        pid = self._parse_pid(args[0])
        sig = self._parse_signal(args[1])
        task = self._get_task(pid)
        task.acceptSignal(sig)
        return "sent signal {} to PID {} ({})".format(sig, pid, SmallSignals.describeSignal(sig))

    def command_signals(self, args):
        """Show latched signals for a task and their documented meanings."""
        if len(args) != 1:
            raise ShellCommandError("usage: signals <pid>")

        task = self._get_task(self._parse_pid(args[0]))
        signals = task.getSignals()
        if not signals:
            return "PID {} has no pending signals".format(task.getID())

        lines = ["PID {} pending signals:".format(task.getID())]
        for sig in signals:
            lines.append("- {}: {}".format(sig, SmallSignals.describeSignal(sig)))
        return "\n".join(lines)

    def command_children(self, args):
        """List child task summaries for one parent PID."""
        if len(args) != 1:
            raise ShellCommandError("usage: children <pid>")

        task = self._get_task(self._parse_pid(args[0]))
        if not task.children:
            return "PID {} has no children".format(task.getID())

        lines = ["PID {} children:".format(task.getID())]
        for child_id in task.children:
            child = self._require_os().tasks.search(child_id)
            if child == -1:
                lines.append("- {} (already finished)".format(child_id))
            else:
                lines.append("- {}".format(child))
        return "\n".join(lines)

    def command_io(self, args):
        """Inspect or manage the runtime's buffered app output."""
        os_ref = self._require_os()
        action = args[0] if args else "status"

        if action == "status":
            status = os_ref.terminalStatus()
            return (
                "terminal_visible={terminal_visible}, buffered_messages={buffered_messages}, "
                "buffer_length={buffer_length}"
            ).format(**status)

        if action == "show":
            lines = os_ref.getBufferedOutput()
            if not lines:
                return "buffer is empty"
            return "".join(lines)

        if action == "clear":
            removed = os_ref.clearBufferedOutput()
            return "cleared {} buffered message(s)".format(removed)

        if action == "flush":
            flushed = os_ref.flushBufferedOutput()
            return "flushed {} buffered message(s)".format(flushed)

        raise ShellCommandError("usage: io [status|show|flush|clear]")

    def command_echo(self, args):
        """Write arbitrary text through the shell output path."""
        return " ".join(args)

    def command_python(self, args):
        """Evaluate or execute Python in the shell context."""
        if not self.allow_python:
            raise ShellCommandError("python command is disabled for this shell")
        if not args:
            raise ShellCommandError("usage: python <expr/code>")

        source = " ".join(args)
        self.locals.setdefault("OS", self.OS)
        self.locals["kernel"] = self._kernel()

        try:
            result = eval(source, self.locals, self.locals)
        except SyntaxError:
            self.locals["_"] = None
            exec(source, self.locals, self.locals)
            result = self.locals.get("_")

        self.locals["_"] = result
        if result is None:
            return "python executed"
        return repr(result)

    def command_exit(self, args):
        """Stop the shell session without terminating the entire process."""
        self.is_running = False
        return "shell session closed"
