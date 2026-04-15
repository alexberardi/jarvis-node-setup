"""Build OpenAI tool schemas from discovered commands.

Handles both IJarvisCommand (built-in) commands that have
to_openai_tool_schema() and SDK-only (Pantry) commands that don't.
"""

from typing import Any, Dict, List, Tuple


def build_tool_schemas(
    commands: Dict[str, Any],
    date_context: Any = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build client_tools and available_commands from discovered commands.

    Args:
        commands: Dict of command_name → command instance.
        date_context: Optional date context for schema generation.

    Returns:
        (client_tools, available_commands) tuple.
    """
    client_tools: List[Dict[str, Any]] = []
    available_commands: List[Dict[str, Any]] = []

    for cmd in commands.values():
        try:
            # Built-in commands have these methods via IJarvisCommand
            if hasattr(cmd, "to_openai_tool_schema"):
                client_tools.append(cmd.to_openai_tool_schema(date_context))
                available_commands.append(cmd.get_command_schema(date_context))
            else:
                # SDK-only commands (Pantry) — build schema from interface
                tool, schema = _build_schema_from_sdk_command(cmd)
                client_tools.append(tool)
                available_commands.append(schema)
        except Exception as e:
            # Don't let one bad command break the whole list
            print(f"[tool_schema] Skipping {getattr(cmd, 'command_name', '?')}: {e}", flush=True)

    return client_tools, available_commands


def _build_schema_from_sdk_command(cmd: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build OpenAI tool schema from an SDK IJarvisCommand instance."""
    name = cmd.command_name
    description = cmd.description
    params = cmd.parameters

    # Build JSON Schema for parameters
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for param in params:
        prop = _param_type_to_json_schema(param.param_type if hasattr(param, "param_type") else "string")
        prop["description"] = param.description or param.name
        if hasattr(param, "enum_values") and param.enum_values:
            prop["enum"] = param.enum_values
        properties[param.name] = prop
        if param.required:
            required.append(param.name)

    tool_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        },
    }
    if required:
        tool_schema["function"]["parameters"]["required"] = required

    # Build available_commands entry
    keywords = cmd.keywords if hasattr(cmd, "keywords") else []
    allow_direct = cmd.allow_direct_answer if hasattr(cmd, "allow_direct_answer") else False
    examples = []
    try:
        examples = [
            {"voice_command": ex.voice_command, "expected_parameters": ex.expected_parameters}
            for ex in cmd.generate_prompt_examples()
        ]
    except Exception:
        pass

    command_schema: Dict[str, Any] = {
        "command_name": name,
        "description": description,
        "keywords": keywords,
        "allow_direct_answer": allow_direct,
        "examples": examples,
    }

    return tool_schema, command_schema


def _param_type_to_json_schema(param_type: str) -> Dict[str, Any]:
    """Map IJarvisParameter param_type to a JSON Schema object.

    Handles generic array types like 'array<datetime>' by producing
    {"type": "array", "items": {"type": "string", "format": "date-time"}}.
    """
    # Handle array<inner_type> syntax
    if param_type.startswith("array<") and param_type.endswith(">"):
        inner = param_type[6:-1]
        return {"type": "array", "items": _param_type_to_json_schema(inner)}

    simple: Dict[str, Dict[str, Any]] = {
        "string": {"type": "string"},
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "integer": {"type": "integer"},
        "float": {"type": "number"},
        "number": {"type": "number"},
        "bool": {"type": "boolean"},
        "boolean": {"type": "boolean"},
        "date": {"type": "string", "format": "date"},
        "time": {"type": "string", "format": "time"},
        "datetime": {"type": "string", "format": "date-time"},
        "array": {"type": "array"},
        "enum": {"type": "string"},
    }
    return dict(simple.get(param_type, {"type": "string"}))
