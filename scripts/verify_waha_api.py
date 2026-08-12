#!/usr/bin/env python
"""Check this app's WAHA calls against the running container's live spec (FR-4).

WAHA's request shapes drift between versions and engines, so rather than trusting
memory, this pulls the OpenAPI document out of the container you are actually
running and asserts that every endpoint and required field we use still exists.

Run after upgrading the WAHA image:

    .venv/bin/python scripts/verify_waha_api.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config  # noqa: E402

# Endpoint -> fields this app sends in the request body.
EXPECTED: dict[tuple[str, str], set[str]] = {
    ("post", "/api/sendText"): {"session", "chatId", "text", "linkPreview"},
    ("post", "/api/sendPoll"): {"session", "chatId", "poll"},
}

# Endpoints we call without a JSON body worth checking.
EXPECTED_PRESENT: list[tuple[str, str]] = [
    ("get", "/api/sessions/{session}"),
    ("post", "/api/sessions/start"),
    ("get", "/api/{session}/groups"),
    ("get", "/api/{session}/channels"),
]

SPEC_PATHS = ["/-json", "/openapi.json", "/api-json"]


def fetch_spec() -> dict:
    """The spec sits behind HTTP Basic auth, not the API key."""
    import os

    user = os.getenv("WAHA_DASHBOARD_USERNAME", "admin")
    password = os.getenv("WAHA_DASHBOARD_PASSWORD", "")
    auth = (user, password) if password else None

    for path in SPEC_PATHS:
        try:
            resp = httpx.get(f"{config.waha_base_url}{path}", auth=auth, timeout=20.0)
        except httpx.HTTPError as exc:
            sys.exit(f"Cannot reach WAHA at {config.waha_base_url}: {exc}")
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return resp.json()
        if resp.status_code == 401:
            sys.exit(
                f"WAHA rejected the dashboard login on {path} (401).\n"
                "Set WAHA_DASHBOARD_USERNAME / WAHA_DASHBOARD_PASSWORD in .env, then\n"
                "docker compose up -d --force-recreate"
            )
    sys.exit(f"Could not find the OpenAPI document. Tried: {', '.join(SPEC_PATHS)}")


def resolve(spec: dict, schema: dict) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return spec.get("components", {}).get("schemas", {}).get(name, {})
    return schema


def main() -> int:
    spec = fetch_spec()
    paths = spec.get("paths", {})
    version = spec.get("info", {}).get("version", "unknown")
    print(f"WAHA spec loaded — API version {version}, {len(paths)} paths\n")

    failures: list[str] = []

    for method, path in EXPECTED_PRESENT:
        if method in paths.get(path, {}):
            print(f"  ok    {method.upper():5} {path}")
        else:
            failures.append(f"missing endpoint: {method.upper()} {path}")
            print(f"  FAIL  {method.upper():5} {path}  — not in spec")

    for (method, path), fields in EXPECTED.items():
        op = paths.get(path, {}).get(method)
        if not op:
            failures.append(f"missing endpoint: {method.upper()} {path}")
            print(f"  FAIL  {method.upper():5} {path}  — not in spec")
            continue

        body = (
            op.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        props = set(resolve(spec, body).get("properties", {}))
        unknown = fields - props
        if unknown:
            failures.append(f"{method.upper()} {path}: unknown field(s) {sorted(unknown)}")
            print(f"  FAIL  {method.upper():5} {path}  — spec has no {sorted(unknown)}")
        else:
            print(f"  ok    {method.upper():5} {path}  — all {len(fields)} fields valid")

    print()
    if failures:
        print(f"{len(failures)} problem(s) found:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("All WAHA calls match the running container's spec.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
