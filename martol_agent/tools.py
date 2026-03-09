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
                "simulation": {
                    "type": "object",
                    "description": (
                        "Optional structured preview of what this action will do. "
                        "Displayed to human reviewers alongside the approval card. "
                        "Note: previews are agent-declared, not server-verified."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "code_diff",
                                "shell_preview",
                                "api_call",
                                "file_ops",
                                "custom",
                            ],
                            "description": "The kind of preview to render.",
                        },
                        "preview": {
                            "type": "object",
                            "description": (
                                "Type-specific preview data. For code_diff: "
                                "{file, diff, lines_added, lines_removed}. "
                                "For shell_preview: {command, working_dir?, "
                                "predicted_effects?}. For api_call: {method, "
                                "url, headers?, body?}. For file_ops: "
                                "{operations: [{path, op}]}. For custom: "
                                "{markdown}."
                            ),
                        },
                        "impact": {
                            "type": "object",
                            "description": "Estimated impact of the action.",
                            "properties": {
                                "files_modified": {
                                    "type": "integer",
                                    "description": "Number of files affected.",
                                },
                                "services_affected": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 20,
                                    "description": "Services impacted.",
                                },
                                "reversible": {
                                    "type": "boolean",
                                    "description": (
                                        "Whether the action can be undone. "
                                        "false triggers server risk escalation."
                                    ),
                                },
                            },
                        },
                        "risk_factors": {
                            "type": "array",
                            "maxItems": 10,
                            "description": "Risk explanations shown to reviewer.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "factor": {
                                        "type": "string",
                                        "maxLength": 100,
                                        "description": "Short risk label.",
                                    },
                                    "severity": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high"],
                                    },
                                    "detail": {
                                        "type": "string",
                                        "maxLength": 500,
                                        "description": "Explanation of the risk.",
                                    },
                                },
                                "required": ["factor", "severity", "detail"],
                            },
                        },
                    },
                    "required": ["type", "preview"],
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
    {
        "name": "brief_get_active",
        "description": (
            "Fetch the current project brief for this room. The brief contains "
            "project goals, tech stack, conventions, and constraints set by the "
            "room owner or lead. Use this to refresh your understanding of the "
            "project context."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
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
