# Node Adapter Training API (PRD)

## Summary
This PRD describes how nodes interact with JCC to trigger adapter training and receive adapter updates. Nodes send command schemas, JCC queues a training job on llm-proxy, and nodes receive an MQTT notification with a signed adapter URL.

## Goals
- Provide a clear, stable API contract for nodes to request adapter training.
- Ensure nodes can map their local command changes to a training request.
- Keep node-side integration minimal and consistent across deployments.

## Non-Goals
- Node-side adapter training.
- Long-term adapter storage on the node.

## Preconditions
- Node has a valid API key for JCC.
- Node can publish command schemas generated from `IJarvisCommand.get_command_schema()`.
- Node has an MQTT client to receive notifications.

## Endpoints

### 1) Train Adapter
`POST /api/v0/adapters/train`

Auth:
- Node API key (same as existing JCC endpoints).

Request body:
```json
{
  "base_model_id": "llm-proxy-model-name",
  "available_commands": [
    {
      "command_name": "get_weather",
      "description": "Current weather or up-to-5-day forecast.",
      "parameters": [
        {"name": "city", "type": "string", "required": false},
        {"name": "resolved_datetimes", "type": "array<datetime>", "required": true}
      ],
      "examples": [
        {
          "voice_command": "What's the weather in Miami?",
          "expected_parameters": {"city": "Miami", "resolved_datetimes": ["2026-01-18T05:00:00Z"]},
          "is_primary": true
        }
      ],
      "keywords": ["weather", "forecast"]
    }
  ],
  "dataset_hash": "optional-sha256",
  "params": {
    "rank": 16,
    "epochs": 2,
    "batch_size": 4,
    "max_seq_len": 2048
  },
  "priority": "normal"
}
```

Response:
```json
{
  "status": "queued",
  "job_id": "uuid",
  "dataset_hash": "sha256",
  "llm_proxy_response": {
    "accepted": true,
    "job_id": "uuid",
    "deduped": false
  }
}
```

Notes:
- `dataset_hash` is optional; JCC computes it if omitted.
- `params` are optional; llm-proxy defaults apply if omitted.

### 2) Job Status (optional)
`GET /api/v0/adapters/jobs/{job_id}`

Response:
```json
{
  "status": "queued|running|complete|failed",
  "progress": 0.5,
  "artifact_url": "https://...",
  "artifact_metadata": {
    "node_id": "node-123",
    "base_model_id": "llm-proxy-model-name",
    "dataset_hash": "sha256",
    "adapter_format": "peft_lora"
  }
}
```

## MQTT Notification
When training completes, JCC publishes an MQTT event to the node:
```json
{
  "node_id": "node-123",
  "base_model_id": "llm-proxy-model-name",
  "adapter_url": "https://...",
  "dataset_hash": "abc123",
  "created_at": "2026-01-18T04:00:00Z"
}
```

Node behavior:
- Download adapter from `adapter_url`.
- Load adapter for the node’s base model.
- Replace any prior adapter for that node.

## Error Handling
Schema validation errors return **400** with details:
```json
{
  "error": "invalid_schema",
  "details": [
    {"path": "commands[0].examples[0]", "code": "missing_field", "message": "voice_command required"}
  ]
}
```

## Limits
- Max 2000 examples per training request.
- Signed adapter URLs are reusable until TTL.

## Best Practices for Nodes
- Train on every command change (add/update/delete).
- Include at least 5–15 examples per command.
- Provide at most one primary example per command.
