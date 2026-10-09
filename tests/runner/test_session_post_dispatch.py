"""Runner dispatch + schema coverage for sys_session_post (set5think fork patch).

sys_session_post is the relationship-agnostic write complement to
sys_session_send: it posts a user message to ANY accessible session via the
server's auth-gated POST /v1/sessions/{id}/events endpoint, with no spawn-tree
parentage check. These tests cover the runner REST helper (success,
not_found, out_of_tree, server error, missing args) and that the tool is
advertised to native harnesses with the right schema.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _session_post_via_rest,
    build_native_relay_tool_schemas,
    execute_tool,
)
from omnigent.spec.types import AgentSpec


@pytest.mark.asyncio
async def test_session_post_delivers_message_to_any_session() -> None:
    """A valid post hits POST .../events and returns posted:true."""
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/sessions/conv_target/events"
        body = json.loads(request.content)
        assert body["type"] == "message"
        assert body["data"]["role"] == "user"
        assert body["data"]["content"] == [{"type": "input_text", "text": "hello there"}]
        return httpx.Response(202, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        out = await _session_post_via_rest(
            json.dumps({"conversation_id": "conv_target", "message": "hello there"}),
            client,
        )
    assert json.loads(out) == {"posted": True, "conversation_id": "conv_target"}
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_session_post_maps_404_to_session_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        out = await _session_post_via_rest(
            json.dumps({"conversation_id": "missing", "message": "x"}), client
        )
    assert json.loads(out) == {"error": "session_not_found", "conversation_id": "missing"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_session_post_maps_auth_denied_to_out_of_tree(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "denied"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        out = await _session_post_via_rest(
            json.dumps({"conversation_id": "forbidden", "message": "x"}), client
        )
    assert json.loads(out) == {"error": "session_out_of_tree", "conversation_id": "forbidden"}


@pytest.mark.asyncio
async def test_session_post_surfaces_other_server_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        out = await _session_post_via_rest(
            json.dumps({"conversation_id": "c", "message": "x"}), client
        )
    parsed = json.loads(out)
    assert parsed["error"] == "sys_session_post returned 500"
    assert parsed["conversation_id"] == "c"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,needle",
    [
        ({"message": "x"}, "conversation_id"),
        ({"conversation_id": "c"}, "message"),
        ({"conversation_id": "", "message": "x"}, "conversation_id"),
        ({"conversation_id": "c", "message": ""}, "message"),
    ],
)
async def test_session_post_requires_both_args(args: dict, needle: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(202)),
        base_url="http://server",
    ) as client:
        out = await _session_post_via_rest(json.dumps(args), client)
    assert needle in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_session_post_requires_server_access() -> None:
    out = await _session_post_via_rest(json.dumps({"conversation_id": "c", "message": "x"}), None)
    assert json.loads(out)["error"] == "sys_session_post requires server access"


@pytest.mark.asyncio
async def test_execute_tool_routes_session_post() -> None:
    """execute_tool dispatches sys_session_post through the REST write path."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(202, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        out = await execute_tool(
            tool_name="sys_session_post",
            arguments=json.dumps({"conversation_id": "conv_other", "message": "ping"}),
            server_client=client,
            conversation_id="conv_me",
            agent_spec=AgentSpec(spec_version=1),
        )
    assert json.loads(out) == {"posted": True, "conversation_id": "conv_other"}
    assert "/v1/sessions/conv_other/events" in seen


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_session_post(spec: AgentSpec | None) -> None:
    schemas = build_native_relay_tool_schemas(spec)
    post = next(s for s in schemas if s["name"] == "sys_session_post")
    assert set(post["parameters"]["required"]) == {"conversation_id", "message"}
    assert post["parameters"]["additionalProperties"] is False
