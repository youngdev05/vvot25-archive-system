"""Create and delete the archives document table from Terraform."""

import os
import sys
import time
from urllib.error import URLError

from function.document_api import DocumentAPIError, call


KEY_SCHEMA = [{"AttributeName": "key", "KeyType": "HASH"}]
ATTRIBUTE_DEFINITIONS = [{"AttributeName": "key", "AttributeType": "S"}]
NOT_FOUND = "ResourceNotFoundException"
THROTTLING_ERRORS = {
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "ThrottlingException",
}


def manage_table(operation, endpoint, token, name):
    table = {"TableName": name}
    deadline = time.monotonic() + 180

    def request(action, payload):
        while True:
            try:
                return call(endpoint, token, action, payload)
            except DocumentAPIError as error:
                if error.status < 500 and error.status != 429 and error.code not in THROTTLING_ERRORS:
                    raise
                if time.monotonic() >= deadline:
                    raise
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
            time.sleep(3)

    if operation == "create":
        try:
            request("CreateTable", {
                **table,
                "BillingMode": "PAY_PER_REQUEST",
                "KeySchema": KEY_SCHEMA,
                "AttributeDefinitions": ATTRIBUTE_DEFINITIONS,
            })
        except DocumentAPIError as error:
            if error.code != "ResourceInUseException":
                raise
    else:
        try:
            request("DeleteTable", table)
        except DocumentAPIError as error:
            if error.code == NOT_FOUND:
                return
            raise

    while time.monotonic() < deadline:
        try:
            description = request("DescribeTable", table)["Table"]
        except DocumentAPIError as error:
            if error.code != NOT_FOUND:
                raise
            if operation == "delete":
                return
        else:
            if operation == "create" and description["TableStatus"] == "ACTIVE":
                if description["KeySchema"] != KEY_SCHEMA or description["AttributeDefinitions"] != ATTRIBUTE_DEFINITIONS:
                    raise ValueError(f"Existing table {name} has an unexpected key schema")
                return
        time.sleep(3)
    raise TimeoutError(f"Document table {name}: {operation} timed out")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"create", "delete"}:
        sys.exit("Usage: python -m scripts.document_table create|delete")
    if not os.environ.get("YC_TOKEN"):
        sys.exit("Set YC_TOKEN to a valid Yandex Cloud IAM token")
    manage_table(sys.argv[1], os.environ["YDB_DOCUMENT_ENDPOINT"], os.environ["YC_TOKEN"], os.environ["ARCHIVES_TABLE"])
    print(f"Document table {os.environ['ARCHIVES_TABLE']}: {sys.argv[1]} complete")
