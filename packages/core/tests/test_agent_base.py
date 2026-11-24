"""Contract tests for the shared JSON agent request helper."""

import pytest
from wenyi_core.agents.base import Agent
from wenyi_core.llm.json_parser import JsonParseError
from wenyi_core.llm.providers.fake import FakeClient

from tests.pipeline_fixtures import fake_agent_config


def test_ask_json_preserves_prompts_and_request_options():
    client = FakeClient(lambda *_: '{"polished": ["edited"]}')
    agent = Agent(client, fake_agent_config())
    system = "Keep the text unchanged.\n"
    user = "  Original: 甲\n"

    result = agent._ask_json(
        system, user, operation="polish.body", key="polished", item_type=str, max_tokens=512
    )

    assert result == ["edited"]
    [call] = client.calls
    assert call["messages"] == [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    assert call["operation"] == "polish.body"
    assert call["json_mode"] is True
    assert call["max_tokens"] == 512


def test_ask_json_only_falls_back_on_parse_error_if_default_is_supplied():
    agent = Agent(FakeClient(lambda *_: "not JSON"), fake_agent_config())

    with pytest.raises(JsonParseError):
        agent._ask_json("system", "user", operation="analysis.style")

    assert agent._ask_json("system", "user", operation="analysis.style", default=None) is None
