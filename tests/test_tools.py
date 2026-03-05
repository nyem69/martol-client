"""Tests for martol_agent.tools — tool schema definitions and converters."""

from martol_agent.tools import TOOLS, to_anthropic_tools, to_openai_tools


class TestToolsSchema:
    """Validate the canonical tool definitions."""

    def test_tools_count(self):
        assert len(TOOLS) == 2

    def test_tools_have_required_fields(self):
        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool

    def test_action_submit_name(self):
        assert TOOLS[0]["name"] == "action_submit"

    def test_action_status_name(self):
        assert TOOLS[1]["name"] == "action_status"

    def test_action_submit_required_fields(self):
        required = TOOLS[0]["parameters"]["required"]
        assert "action_type" in required
        assert "risk_level" in required
        assert "trigger_message_id" in required
        assert "description" in required

    def test_action_status_required_fields(self):
        required = TOOLS[1]["parameters"]["required"]
        assert "action_id" in required

    def test_action_submit_has_properties(self):
        props = TOOLS[0]["parameters"]["properties"]
        assert "action_type" in props
        assert "risk_level" in props
        assert "payload" in props


class TestToAnthropicTools:
    """Test conversion to Anthropic format."""

    def test_renames_parameters_to_input_schema(self):
        result = to_anthropic_tools(TOOLS)
        for tool in result:
            assert "input_schema" in tool
            assert "parameters" not in tool

    def test_preserves_name_and_description(self):
        result = to_anthropic_tools(TOOLS)
        assert result[0]["name"] == "action_submit"
        assert "structured action" in result[0]["description"]


class TestToOpenAITools:
    """Test conversion to OpenAI function-calling format."""

    def test_wraps_in_function_type(self):
        result = to_openai_tools(TOOLS)
        for tool in result:
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_preserves_name_and_description(self):
        result = to_openai_tools(TOOLS)
        assert result[0]["function"]["name"] == "action_submit"
