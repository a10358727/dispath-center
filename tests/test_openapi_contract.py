import hashlib
import json
from pathlib import Path

from app.main import app


SNAPSHOT_PATH = Path(__file__).with_name("openapi_snapshot.sha256")
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def test_openapi_contract_matches_the_pre_extraction_snapshot():
    schema = app.openapi()
    canonical = json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert len(schema["paths"]) == 254
    assert sum(
        method in HTTP_METHODS
        for path_item in schema["paths"].values()
        for method in path_item
    ) == 268
    assert len(schema["components"]["schemas"]) == 126
    assert hashlib.sha256(canonical).hexdigest() == SNAPSHOT_PATH.read_text(
        encoding="utf-8"
    ).strip()
