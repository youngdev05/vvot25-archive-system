import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from pathlib import PureWindowsPath
from urllib.parse import quote
from urllib.request import Request, urlopen

if __package__:
    from . import document_api, source
else:
    import document_api
    import source


FILENAME = re.compile(r"[^/\\:\x00-\x1f\x7f]*[^./\\:\s\x00-\x1f\x7f][^/\\:\x00-\x1f\x7f]*")


def archive_key(queue_id, message):
    # The queue's immutable timestamp and random message ID keep the UUIDv7
    # key stable across repeated deliveries of the same message.
    timestamp = int(message["attributes"]["SentTimestamp"])
    if not 0 <= timestamp < 1 << 48:
        raise ValueError("Invalid message timestamp")
    identity = f"{queue_id}\0{message['message_id']}".encode()
    entropy = int.from_bytes(hashlib.sha256(identity).digest()[:10], "big")
    rand_a = (entropy >> 62) & 0xFFF
    rand_b = entropy & ((1 << 62) - 1)
    value = (timestamp << 80) | (7 << 76) | (rand_a << 64) | (2 << 62) | rand_b
    return f"{uuid.UUID(int=value)}.zip"


def zip_file(name, data):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return output.getvalue()


def put_archive(bucket, key, archive, token):
    endpoint = f"https://storage.yandexcloud.net/{bucket}/{quote(key, safe='')}"
    request = Request(
        endpoint,
        data=archive,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{key}"',
        },
        method="PUT",
    )
    with urlopen(request, timeout=45):
        pass


def handler(event, context):
    details = event["messages"][0]["details"]
    message = details["message"]
    task = json.loads(message["body"])
    name, url = task["name"], task["url"]
    if not 1 <= len(name) <= 255 or not FILENAME.fullmatch(name):
        raise ValueError("Invalid file name")
    if PureWindowsPath(name).is_reserved():
        raise ValueError("Reserved file name")

    key = archive_key(details["queue_id"], message)
    archive = zip_file(name, source.download(url))
    token = context.token["access_token"]
    put_archive(os.environ["ARCHIVE_BUCKET"], key, archive, token)
    document_api.call(
        os.environ["YDB_DOCUMENT_ENDPOINT"],
        token,
        "PutItem",
        {
            "TableName": os.environ["ARCHIVES_TABLE"],
            "Item": {"name": {"S": name}, "url": {"S": url}, "key": {"S": key}},
        },
    )
    print(f"Saved archive {key}")
    return {"key": key}
