"""Test the public routes of a deployed archive system."""

import argparse
import io
import json
import time
import uuid
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def http(url, body=None):
    request = Request(url)
    if body is not None:
        request = Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.headers, error.read()


def expect(url, status, body=None):
    received, headers, data = http(url, body)
    method = "POST" if body is not None else "GET"
    if received != status:
        detail = data[:400].decode(errors="replace")
        raise RuntimeError(f"{method} {url}: expected {status}, got {received}\n{detail}")
    print(f"{method} {url} -> {status}", flush=True)
    return headers, data


def test_api(base, source_url, enqueue_failure):
    _, source_bytes = expect(source_url, 200)
    suffix = uuid.uuid4().hex[:10]
    task = {"name": f"smoke-{suffix}.txt", "url": source_url}
    task_url = f"{base}/tasks"

    expect(task_url, 400, b'{"name":"missing-url.txt"}')
    expect(task_url, 400, b'{"name":')
    expect(task_url, 400, b'{"name":"file.txt","url":"not-a-url"}')
    expect(f"{base}/archives/missing-{suffix}.zip", 404)
    expect(f"{base}/download/missing-{suffix}.zip", 404)
    expect(task_url, 202, json.dumps(task).encode())

    if enqueue_failure:
        failure = {"name": f"failure-{suffix}.txt", "url": "https://archive-test.invalid/file.txt"}
        expect(task_url, 202, json.dumps(failure).encode())
        print(f"Look for {failure['name']} in the dead-letter queue after the visibility timeout.")

    deadline = time.monotonic() + 120
    while True:
        _, data = expect(f"{base}/archives", 200)
        records = json.loads(data)
        if not isinstance(records, list):
            raise RuntimeError("GET /archives did not return a JSON array")
        record = next((row for row in records if row.get("name") == task["name"]), None)
        if record is not None:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Archive for {task['name']} did not appear in 120 seconds")
        time.sleep(4)

    key = record["key"]
    expected = {**task, "key": key}
    archive_id = uuid.UUID(key.removesuffix(".zip"))
    if key != f"{archive_id}.zip" or archive_id.version != 7 or record != expected:
        raise RuntimeError(f"Unexpected archive metadata: {record}")
    _, data = expect(f"{base}/archives/{key}", 200)
    if json.loads(data) != expected:
        raise RuntimeError("Archive details differ from the list entry")

    headers, data = expect(f"{base}/download/{key}", 200)
    if headers.get_content_type() != "application/zip":
        raise RuntimeError("The download response does not have Content-Type application/zip")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.namelist() != [task["name"]] or archive.read(task["name"]) != source_bytes:
            raise RuntimeError("The downloaded ZIP does not match the source file")
    print(f"Verified UUIDv7, metadata and ZIP contents: {key}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_url", help="Service URL from terraform output -raw api_url")
    parser.add_argument("--source-url", default="https://www.rfc-editor.org/rfc/rfc9562.txt")
    parser.add_argument("--enqueue-failure", action="store_true", help="Also enqueue a job with an unreachable source")
    args = parser.parse_args()
    test_api(args.api_url.rstrip("/"), args.source_url, args.enqueue_failure)
