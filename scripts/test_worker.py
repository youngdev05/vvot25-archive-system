import io
import json
import unittest
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from function import index, source


MESSAGE = {
    "message_id": "ae27ae33-a054-47b2-a5ac-2740d955b184",
    "attributes": {"SentTimestamp": "1760000000456"},
    "body": json.dumps({"name": "данные.txt", "url": "https://example.com/file.txt"}),
}
QUEUE = "yrn:yc:ymq:ru-central1:account:vvot25-tasks"
EVENT = {"messages": [{"details": {"queue_id": QUEUE, "message": MESSAGE}}]}
ENVIRONMENT = {
    "ARCHIVE_BUCKET": "vvot25-test-bucket",
    "YDB_DOCUMENT_ENDPOINT": "https://ydb.example.com/test",
    "ARCHIVES_TABLE": "archives",
}
CONTEXT = SimpleNamespace(token={"access_token": "test-token"})


class WorkerTests(unittest.TestCase):
    def test_uuid7_is_stable_for_redelivery(self):
        key = index.archive_key(QUEUE, MESSAGE)
        identifier = uuid.UUID(key.removesuffix(".zip"))
        self.assertEqual(identifier.version, 7)
        self.assertEqual(identifier.int >> 80, int(MESSAGE["attributes"]["SentTimestamp"]))
        self.assertEqual(index.archive_key(QUEUE, dict(MESSAGE)), key)
        self.assertNotEqual(index.archive_key(QUEUE + "-other", MESSAGE), key)

    def test_handler_stores_zip_before_metadata(self):
        services = Mock()
        services.download.return_value = b"sample bytes"
        with (
            patch.dict(index.os.environ, ENVIRONMENT),
            patch.object(index.source, "download", services.download),
            patch.object(index, "put_archive", services.put_archive),
            patch.object(index.document_api, "call", services.document),
        ):
            result = index.handler(EVENT, CONTEXT)

        self.assertEqual([call[0] for call in services.mock_calls], ["download", "put_archive", "document"])
        bucket, key, blob, token = services.put_archive.call_args.args
        self.assertEqual((bucket, key, token), (ENVIRONMENT["ARCHIVE_BUCKET"], result["key"], "test-token"))
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(archive.namelist(), ["данные.txt"])
            self.assertEqual(archive.read("данные.txt"), b"sample bytes")
        services.document.assert_called_once_with(
            ENVIRONMENT["YDB_DOCUMENT_ENDPOINT"], "test-token", "PutItem",
            {"TableName": "archives", "Item": {
                "name": {"S": "данные.txt"}, "url": {"S": "https://example.com/file.txt"}, "key": {"S": key},
            }},
        )

    def test_download_error_propagates_to_trigger(self):
        with (
            patch.object(index.source, "download", side_effect=OSError("source unavailable")),
            patch.object(index, "put_archive") as upload,
            patch.object(index.document_api, "call") as write,
        ):
            with self.assertRaisesRegex(OSError, "source unavailable"):
                index.handler(EVENT, CONTEXT)
            upload.assert_not_called()
            write.assert_not_called()

    def test_non_public_source_is_rejected(self):
        dns = [(source.socket.AF_INET, source.socket.SOCK_STREAM, source.socket.IPPROTO_TCP, "", ("127.0.0.1", 80))]
        with patch.object(source.socket, "getaddrinfo", return_value=dns), patch.object(source.socket, "socket") as sock:
            with self.assertRaisesRegex(ValueError, "public addresses"):
                source.download("http://example.com/file.txt")
            sock.assert_not_called()

    def test_size_limit_applies_when_length_is_unknown(self):
        response = Mock()
        response.getheader.return_value = None
        response.read1.side_effect = [b"abcd", b"efgh", b""]
        with patch.object(source, "MAX_SOURCE_BYTES", 7):
            with self.assertRaisesRegex(ValueError, "too large"):
                source.read_file(response, float("inf"))


if __name__ == "__main__":
    unittest.main()
