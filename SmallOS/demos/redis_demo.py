"""SmallOS Redis example using the native cooperative Redis client."""

from common import build_runtime

from SmallPackage import SmallRedisClient, SmallTask, Unix


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_USE_TLS = False


async def redis_demo(task):
    client = SmallRedisClient(
        task,
        host=REDIS_HOST,
        port=REDIS_PORT,
        use_tls=REDIS_USE_TLS,
    )
    await client.connect()
    pong = await client.ping()
    await client.set("smallos:demo", "hello from smallOS")
    value = await client.get("smallos:demo")
    task.OS.print("redis ping: {}\n".format(pong))
    task.OS.print("redis value: {}\n".format(value))
    client.close()
    return value


def main():
    runtime = build_runtime(Unix())
    runtime.fork([SmallTask(2, redis_demo, name="redis_demo")])
    runtime.startOS()


if __name__ == "__main__":
    main()
