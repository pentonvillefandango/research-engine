from typing import Any

import httpx

EXEMPT = {"/v1/schemas/{name}"}  # application/schema+json: the one documented non-envelope
ENVELOPE_PATHS = ("/health", "/version")
BODY_METHODS = {"post", "put", "patch"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def check_spec(spec: dict[str, Any]) -> list[str]:
    """Return a list of completeness problems in an OpenAPI spec (empty means complete)."""
    problems: list[str] = []
    for path, ops in spec["paths"].items():
        if not (path.startswith("/v1") or path in ENVELOPE_PATHS) or path in EXEMPT:
            continue
        for method, op in ops.items():
            if method not in HTTP_METHODS:
                continue
            where = f"{method.upper()} {path}"
            responses = op.get("responses", {})
            if path.startswith("/v1") and "401" not in responses:
                problems.append(f"{where} does not document 401")
            body = op.get("requestBody")
            if method in BODY_METHODS and body is None:
                problems.append(f"{where} has no requestBody")
            if body is not None:
                content = body.get("content", {}).get("application/json")
                if content is None:
                    problems.append(f"{where} requestBody has no application/json content")
                elif not (content.get("examples") or content.get("example")):
                    problems.append(f"{where} lacks a request example")
            for code, resp in responses.items():
                if not code.startswith("2"):
                    continue
                schema = resp.get("content", {}).get("application/json", {}).get("schema")
                if schema is None:
                    problems.append(f"{where} {code} has no application/json schema")
                    continue
                ref = str(schema.get("$ref", ""))
                name = ref.rsplit("/", 1)[-1]
                if not (name.startswith("Envelope_") and name.endswith("_")):
                    problems.append(f"{where} {code} not enveloped: {schema}")
                elif name == "Envelope_NoneType_":
                    problems.append(f"{where} {code} is an empty envelope: {name}")
    return problems


async def test_openapi_examples_and_envelopes(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert check_spec(spec) == []
    posts = [p for p, ops in spec["paths"].items() if p.startswith("/v1") and "post" in ops]
    assert len(posts) >= 4  # search, fetch, fetch/batch, search_read: the walk saw them
    assert (
        spec["paths"]["/v1/schemas"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"].rsplit("/", 1)[-1]
        == "Envelope_list_str__"
    )
    assert "503" in spec["paths"]["/health"]["get"]["responses"]


def test_checker_catches_bad_routes() -> None:
    ok_schema = {"content": {"application/json": {"schema": {"$ref": "#/c/Envelope_Job_"}}}}
    spec = {
        "paths": {
            "/v1/bare": {"post": {"responses": {"200": {"description": "x"}}}},
            "/v1/noexample": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {}}}},
                    "responses": {"200": ok_schema, "401": {}},
                }
            },
            "/v1/raw": {
                "get": {
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"$ref": "#/c/Job"}}}},
                        "401": {},
                    }
                }
            },
            "/v1/none": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/c/Envelope_NoneType_"}}
                            }
                        },
                        "401": {},
                    }
                }
            },
        }
    }
    problems = "\n".join(check_spec(spec))
    for expected in (
        "POST /v1/bare has no requestBody",
        "POST /v1/bare does not document 401",
        "POST /v1/bare 200 has no application/json schema",
        "POST /v1/noexample lacks a request example",
        "GET /v1/raw 200 not enveloped",
        "GET /v1/none 200 is an empty envelope",
    ):
        assert expected in problems


async def test_schema_endpoint_documented_as_schema_json(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    op = spec["paths"]["/v1/schemas/{name}"]["get"]
    assert "application/schema+json" in op["responses"]["200"]["content"]
    assert "401" in op["responses"] and "404" in op["responses"]
