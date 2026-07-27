"""Bounded FastAPI TestClient lifecycle smoke for dependency/CI validation."""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def main() -> None:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/healthz")
        if response.status_code != 200 or response.json() != {"ok": True}:
            raise SystemExit(
                "TestClient smoke returned an unexpected response: "
                f"{response.status_code} {response.text!r}"
            )

    print("TestClient lifecycle smoke: PASS")


if __name__ == "__main__":
    main()
