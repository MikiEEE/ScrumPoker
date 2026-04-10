"""
Showcase demo for the native smallOS runtime.

This is the new home for the original root-level `demo.py` examples. It keeps
the same spirit as the earlier demo, but now leans on the higher-level HTTP
client instead of handcrafting the request socket logic inline.
"""

from common import build_runtime

from SmallPackage import SmallHTTPClient, SmallTask, Unix


HTTP_BASE_URL = "http://example.com"
HTTP_PATH = "/"


async def priority_worker(task):
    """Simple child workload used to show priority-aware interleaving."""
    for step in range(3):
        task.OS.print("[{}] step {}\n".format(task.name, step))
        await task.sleep(0.05)
    return task.name


async def join_demo(task):
    """Spawn three workers and collect their results in a fixed order."""
    task.OS.print("join demo starting\n")
    fast = task.spawn(priority_worker, priority=1, name="fast")
    medium = task.spawn(priority_worker, priority=3, name="medium")
    slow = task.spawn(priority_worker, priority=5, name="slow")
    results = await task.join_all([fast, medium, slow])
    task.OS.print("join demo results: {}\n".format(results))
    return results


async def http_request_task(task, base_url=HTTP_BASE_URL, path=HTTP_PATH):
    """Issue one HTTP request through the cooperative client."""
    client = SmallHTTPClient(task, base_url=base_url)
    response = await client.get(path)
    return {
        "status": response.status_code,
        "reason": response.reason,
        "preview": response.text().replace("\n", " ")[:120],
    }


async def http_request_demo(task):
    """Show a network request running while the parent keeps doing work."""
    task.OS.print("http request demo starting\n")
    request = task.spawn(
        http_request_task,
        priority=max(1, task.priority - 1),
        name="http_request_task",
        args=(HTTP_BASE_URL, HTTP_PATH),
    )

    for step in range(3):
        task.OS.print("http request parent doing other work {}\n".format(step))
        await task.sleep(0.05)

    response = await task.join(request)
    task.OS.print("http request status: {} {}\n".format(response["status"], response["reason"]))
    task.OS.print("http request preview: {}\n".format(response["preview"]))
    return response


async def signal_sender(task):
    """Sleep for a while and then wake the parent by sending a signal."""
    await task.sleep(0.1)
    task.OS.print("sender raising signal 3\n")
    task.sendSignal(task.parent.pid, 3)
    return "signal sent"


async def signal_demo(task):
    """Show a task blocking on a signal and then joining the sender."""
    task.OS.print("signal demo waiting\n")
    sender = task.spawn(signal_sender, priority=max(1, task.priority - 1), name="signal_sender")
    signal = await task.wait_signal(3)
    sender_result = await task.join(sender)
    task.OS.print("signal demo resumed on {} with {}\n".format(signal, sender_result))
    return sender_result


async def cooperative_demo(task):
    """Show a task voluntarily yielding without waiting on time or signals."""
    for index in range(5):
        task.OS.print("cooperative tick {}\n".format(index))
        await task.yield_now()
    return "done"


def main():
    runtime = build_runtime(Unix())
    runtime.fork(
        [
            SmallTask(2, http_request_demo, name="http_request_demo"),
            SmallTask(2, join_demo, name="join_demo"),
            SmallTask(4, signal_demo, name="signal_demo"),
            SmallTask(6, cooperative_demo, name="cooperative_demo"),
        ]
    )
    runtime.startOS()


if __name__ == "__main__":
    main()
