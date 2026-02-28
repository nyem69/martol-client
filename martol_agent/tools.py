"""Canonical tool definitions for the Martol agent.

These are provider-agnostic. Each provider module converts them to its native
format (Anthropic input_schema, OpenAI function parameters).
"""

TOOLS: list[dict] = [
    {
        "name": "action_submit",
        "description": (
            "Submit a structured action for approval. Use for code changes, "
            "deployments, config modifications, and other actions that need "
            "human review. The action will be reviewed and approved by a human "
            "with sufficient authority before being executed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": [
                        "question_answer",
                        "code_review",
                        "code_write",
                        "code_modify",
                        "code_delete",
                        "deploy",
                        "config_change",
                    ],
                    "description": "The type of action to perform.",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Self-assessed risk level (server may override).",
                },
                "trigger_message_id": {
                    "type": "integer",
                    "description": "The message ID that triggered this action.",
                },
                "description": {
                    "type": "string",
                    "description": "What this action will do (1-2000 chars).",
                },
                "payload": {
                    "type": "object",
                    "description": "Optional structured data for the action.",
                },
            },
            "required": [
                "action_type",
                "risk_level",
                "trigger_message_id",
                "description",
            ],
        },
    },
    {
        "name": "action_status",
        "description": "Check the approval status of a previously submitted action.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "integer",
                    "description": "The action ID returned from action_submit.",
                },
            },
            "required": ["action_id"],
        },
    },
]


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Convert canonical tools to Anthropic format."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert canonical tools to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]
