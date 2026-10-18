# Node: inbox attachments pass-through (2026-10-18)

Piece (4) of the ComfyUI plan: (1) jarvisd inbox attachments, (2) mobile shows
them, (3) the SDK's `JarvisInbox.post(attachments=...)`, (4) the node passes
them through, (5) the `jarvis-cmd-comfyui` package.

## Decision

`services/inbox_backend.py:NodeInboxBackend.post_inbox_item` takes
`attachments: list[Attachment] | None`. With attachments it follows jarvisd's
contract (jarvis-server `docs/decisions/2026-10-17-inbox-attachments.md`):

1. One `POST {cc}/api/v0/node/inbox-attachment` per attachment, in order: raw
   file bytes as the body, `Content-Type: <mime>`, `Content-Length: <size>`,
   node auth (`X-API-Key`). `201` returns `{"id", ...}`.
2. Then the usual `POST {cc}/api/v0/node/inbox-item` with
   `"attachment_ids": [ids in order]`.

Without attachments the request is exactly what it was (same payload, same
`RestClient.post`, no `attachment_ids` key).

### Stream from disk

A `path` attachment is opened `rb` and the file object is the request body;
requests sends it in chunks, so a 25 MB video is never read into memory (and
never held twice). `data` attachments are already in memory and go as-is. The
file is re-opened on each attempt so a retry starts at byte 0. Upload timeout is
(5 s connect, 60 s read) instead of the item POST's 5 s, for big files from a
Pi on Wi-Fi.

### Retries

Up to 3 attempts per upload, backoff 0.5 s then 1 s, on connection errors and
5xx. **Not** retried: any 4xx (the same bytes will be rejected again), 503
(jarvisd has attachments switched off; that will not change in a second), and
read timeouts (same reasoning as `RestClient`'s session: don't amplify a slow
server). The item POST itself is not retried here; uploaded ids stay claimable
for about an hour, so a caller may retry the whole post.

### Tags

| jarvisd says                           | tag                    |
|----------------------------------------|------------------------|
| 413 (file, or the item's total)        | `attachment_too_large` |
| 415 (type not allowed / mislabelled)   | `attachment_type`      |
| 422 on the item (bad/used/foreign id)  | `invalid`              |
| attachment file missing/unreadable     | `invalid`              |
| anything else (400, 503, 5xx x3, down) | `http_error`           |

Every rejection writes one log line, `inbox attachment rejected` with command,
status, mime, size, filename and jarvisd's error body as `reason`; 503 has its
own line (`server has no attachments`).

### Why no multipart

jarvisd's route takes the raw body with the mime in `Content-Type`, one file
per request. That keeps both sides simple (no multipart parser, no boundary
scanning, the server sniffs the first bytes against the declared type), lets
the node stream a file with no encoding step, and makes a retry per file
instead of per bundle.

The upload uses `RestClient._get_session()` and `_build_auth_header()`
directly: `RestClient.post` JSON-encodes the body and hides the status code
that the tags come from.
