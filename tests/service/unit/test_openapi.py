import httpx

EXEMPT = {"/v1/schemas/{name}"}  # application/schema+json: the one documented non-envelope
ENVELOPE_PATHS = ("/health", "/version")


async def test_openapi_examples_and_envelopes(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    checked = 0
    for path, ops in spec["paths"].items():
        if not (path.startswith("/v1") or path in ENVELOPE_PATHS) or path in EXEMPT:
            continue
        for method, op in ops.items():
            checked += 1
            where = f"{method.upper()} {path}"
            if path.startswith("/v1"):
                assert "401" in op["responses"], f"{where} does not document 401"
            if method == "post" and path.startswith("/v1"):
                content = op["requestBody"]["content"]["application/json"]
                assert content.get("examples") or content.get("example"), f"{where} lacks example"
            for code, resp in op["responses"].items():
                if code.startswith("2"):
                    ref = str(resp["content"]["application/json"]["schema"])
                    assert "Envelope_" in ref, f"{where} {code} not enveloped: {ref}"
    assert checked >= 8


async def test_schema_endpoint_documented_as_schema_json(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    op = spec["paths"]["/v1/schemas/{name}"]["get"]
    assert "application/schema+json" in op["responses"]["200"]["content"]
    assert "401" in op["responses"] and "404" in op["responses"]
