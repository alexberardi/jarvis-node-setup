# Tool Router Training Endpoint (FastText)

This PRD documents how a client trains the tool router FastText model via the API.

## Endpoint
- `POST /api/v0/tool-router/train`

## Authentication
- Requires `X-API-Key` header (node API key).

## Request Body
### `ToolRouterTrainingRequest`
```json
{
  "available_commands": [
    {
      "command_name": "get_weather",
      "description": "Get current weather or forecast.",
      "parameters": [
        { "name": "city", "type": "string", "required": false }
      ],
      "examples": [
        {
          "voice_command": "What's the weather in Seattle?",
          "expected_parameters": { "city": "Seattle" },
          "is_primary": true
        }
      ],
      "allow_direct_answer": false
    }
  ],
  "extra_training_jsonl": "{\"utterance\":\"Who won the Super Bowl?\",\"tool_name\":\"search_web\"}\n",
  "extra_training": [
    { "utterance": "Score for the Lakers game", "tool_name": "get_sports_scores" }
  ],
  "output_model_path": "/home/alex/jarvis-command-center/temp/tool_classifier.bin",
  "save_training_jsonl": true,
  "epoch": 25,
  "lr": 0.5,
  "word_ngrams": 2
}
```

### Field Notes
- `available_commands` (required): command definitions; training examples are extracted from `examples[].voice_command`.
- `extra_training_jsonl` (optional): **raw JSONL content**, one record per line, with `utterance` and `tool_name`.
- `extra_training` (optional): additional examples provided inline.
- `output_model_path` (optional): where to save the `.bin` model. Default: `repo_root/temp/tool_classifier.bin`.
- `save_training_jsonl` (optional, default `true`): if true, server writes the combined training set to `repo_root/temp/tool_router_training.jsonl`.
- `epoch`, `lr`, `word_ngrams` (optional): passed to fastText training; defaults are `25`, `0.5`, `2`.

## Response
```json
{
  "status": "success",
  "examples": 128,
  "model_path": "/home/alex/jarvis-command-center/temp/tool_classifier.bin",
  "training_jsonl_path": "/home/alex/jarvis-command-center/temp/tool_router_training.jsonl"
}
```

## Training Data Sources (Server-Side)
The server builds the final training set from:
- `temp/test_results.json` (if present)
- `temp/test_command_parsing.py` (if present)
- `available_commands[].examples[]`
- `extra_training_jsonl`
- `extra_training`

## Errors
- `401` if `X-API-Key` is invalid.
- `500` if `fasttext` is not installed or no examples are provided.

## Example cURL
```bash
curl -X POST http://localhost:8002/api/v0/tool-router/train \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NODE_API_KEY" \
  -d '{
    "available_commands": [
      {
        "command_name": "get_weather",
        "description": "Get current weather or forecast.",
        "parameters": [{"name":"city","type":"string","required":false}],
        "examples": [
          {"voice_command":"Weather in Austin","expected_parameters":{"city":"Austin"},"is_primary":true}
        ]
      }
    ],
    "extra_training": [
      {"utterance":"Score for the Lakers game","tool_name":"get_sports_scores"}
    ],
    "output_model_path": "/home/alex/jarvis-command-center/temp/tool_classifier.bin"
  }'
```

## Node Training Script
This repo includes a helper that trains the tool router using this node's
commands plus extra utterances.

- Script: `scripts/train_tool_router.py`
- Extra JSONL: `training/tool_router_extra_utterances.jsonl`
- Test utterances: pulled from `test_command_parsing.py`

Example:
```bash
python3 scripts/train_tool_router.py \
  --output-model-path /home/alex/jarvis-command-center/temp/tool_classifier.bin
```
