"""Desktop demo for running smallOS on the Unix kernel."""

from common import build_runtime, default_tasks

from SmallPackage.Kernel import Unix


def main():
    runtime = build_runtime(Unix())
    runtime.fork(default_tasks("Unix"))
    runtime.startOS()


if __name__ == "__main__":
    main()
