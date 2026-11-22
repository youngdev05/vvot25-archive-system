"""Small IAM-authenticated client for the YDB Document API."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class DocumentAPIError(RuntimeError):
    def __init__(self, code, status):
        super().__init__(f"Document API: {code} (HTTP {status})")
        self.code = code
        self.status = status


def call(endpoint, token, action, payload):
    request = Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-amz-json-1.0",
            "X-Amz-Target": f"DynamoDB_20120810.{action}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        with error:
            try:
                body = json.load(error)
                code = body.get("__type", body.get("code", "HTTPError"))
                code = str(code).rsplit("#", 1)[-1]
            except (AttributeError, ValueError):
                code = "HTTPError"
        raise DocumentAPIError(code, error.code) from None
