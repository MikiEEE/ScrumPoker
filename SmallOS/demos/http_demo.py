"""SmallOS HTTP example using the native cooperative HTTP client."""

from common import build_runtime

from SmallPackage import SmallHTTPClient, SmallTask, Unix


HTTP_BASE_URL = "http://example.com"


async def http_demo(task):
    client = SmallHTTPClient(task, base_url=HTTP_BASE_URL)
    response = await client.get("/", headers={"Accept": "text/html"})
    preview = response.text().replace("\n", " ")[:120]
    task.OS.print("http status: {} {}\n".format(response.status_code, response.reason))
    task.OS.print("http preview: {}\n".format(preview))
    return response.status_code


def main():
    runtime = build_runtime(Unix())
    runtime.fork([SmallTask(2, http_demo, name="http_demo")])
    runtime.startOS()


if __name__ == "__main__":
    main()
