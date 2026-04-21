# Structured Outputs — The Skill Output Contract Pattern

This is a pattern for multi-agent systems, not an Open Brane feature. But it matters here because every adapter writes events with structured payloads, and agents that *produce* content for Open Brane should do the same.

## The drift problem

Your skill emits markdown. A downstream script builds a document from that markdown. A human gives feedback: "this paragraph is too stiff." You update the skill prompt. Next run is better. But the downstream script *also* writes some markdown, and nobody updated that. The feedback gets lost. The human is annoyed. You debug for an hour and find two markdown generators that should have been one.

Root cause: **markdown is the wrong output contract between an LLM and code.**

Markdown is formatting, not semantics. An LLM writing markdown hallucinates structure — an `##` becomes a `###` next run. A bullet becomes a numbered list. An em-dash replaces a colon. Your parser breaks on Thursday because Wednesday's output had a different shape.

Markdown is great for humans reading the finished thing. It's terrible as a wire format between agents and downstream code.

## The fix: JSON output with a schema

Skills emit JSON that validates against a published schema. A single deterministic renderer converts that JSON into whatever surface you need (doc, markdown, HTML, PDF, Slack message, dashboard row).

One source. Many views. Zero drift.

### Example: a meeting-recap skill

**Schema** (`schemas/meeting-recap.json`):

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["version", "meeting"],
  "properties": {
    "version": {"type": "string", "const": "1.0"},
    "meeting": {
      "type": "object",
      "required": ["title", "date", "attendees", "summary", "decisions", "action_items"],
      "properties": {
        "title": {"type": "string"},
        "date": {"type": "string", "format": "date-time"},
        "attendees": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["name"],
            "properties": {
              "name": {"type": "string"},
              "email": {"type": "string"},
              "role": {"type": "string"}
            }
          }
        },
        "summary": {"type": "string", "maxLength": 600},
        "decisions": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["decision", "why"],
            "properties": {
              "decision": {"type": "string"},
              "why": {"type": "string"},
              "owner": {"type": "string"}
            }
          }
        },
        "action_items": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["owner", "task", "due"],
            "properties": {
              "owner": {"type": "string"},
              "task": {"type": "string"},
              "due": {"type": "string", "format": "date"}
            }
          }
        }
      }
    }
  }
}
```

**Skill system prompt ends with:**

```
Your output MUST be a single JSON object matching schemas/meeting-recap.json.
No markdown. No prose. No explanation before or after the JSON.

Every required field must be present. If you cannot produce a field with
high confidence, use null — do not fabricate.

The next agent in the pipeline will render this JSON deterministically. Any
markdown or prose outside the JSON object breaks the render.
```

**Renderer** (`render/meeting-recap-to-gdoc.py`):

```python
import json, sys
from googleapiclient.discovery import build

def render(data: dict, gdoc_id: str, creds):
    service = build("docs", "v1", credentials=creds)
    requests = []
    m = data["meeting"]

    requests.append({"insertText": {"location": {"index": 1},
        "text": f"{m['title']}\n{m['date']}\n\n"}})

    requests.append({"insertText": {"endOfSegmentLocation": {},
        "text": f"Summary\n{m['summary']}\n\n"}})

    if m["decisions"]:
        requests.append({"insertText": {"endOfSegmentLocation": {}, "text": "Decisions\n"}})
        for d in m["decisions"]:
            line = f"- {d['decision']} - {d['why']}"
            if d.get("owner"):
                line += f" (owner: {d['owner']})"
            requests.append({"insertText": {"endOfSegmentLocation": {}, "text": line + "\n"}})

    if m["action_items"]:
        requests.append({"insertText": {"endOfSegmentLocation": {}, "text": "\nAction Items\n"}})
        for a in m["action_items"]:
            requests.append({"insertText": {"endOfSegmentLocation": {},
                "text": f"[ ] [{a['owner']}] {a['task']} - due {a['due']}\n"}})

    service.documents().batchUpdate(documentId=gdoc_id, body={"requests": requests}).execute()

if __name__ == "__main__":
    render(json.load(sys.stdin), sys.argv[1], load_creds())
```

You run the renderer once. You trust it. When someone gives feedback on *style*, you update the skill prompt. When someone gives feedback on *format*, you update the renderer. The two concerns never tangle again.

## Why this works

| Contract | Concerns separated |
|---|---|
| Skill → JSON → renderer → output | Style (LLM) vs format (code). LLM feedback -> prompt. Format feedback -> renderer. |
| Skill → markdown → output | Style and format mixed in the LLM output. Feedback goes to "the markdown" which is two files. |

Downstream benefits:

- **Validation.** Feed every skill output through `jsonschema.validate()`. Malformed outputs never reach the renderer.
- **Multi-surface rendering.** Same JSON -> Google Doc. Same JSON -> Obsidian page. Same JSON -> Notion block. Same JSON -> a dashboard tile. Each renderer is 50-150 lines.
- **Editability.** Build a UI that edits JSON fields. One click patches the event. All surfaces re-render.
- **Diffability.** `diff` two runs of the same skill by comparing their JSON payloads. Much cleaner than diffing markdown.
- **Replay.** The JSON is stored as an event payload. You can re-render any historical run by piping it through the current renderer.

## Why Open Brane cares

`record_event.py` takes JSON on stdin. Every adapter already writes structured payloads. Skills that produce deliverables should too: `record_event(source="skill", type="meeting-recap", payload={meeting: {...}})` captures the exact JSON the skill produced. You can re-render any old event. You can diff two versions. You can compose across events — aggregate "all meeting recaps where decisions list contains 'launch'" via a SQL query.

If your skills emit markdown and your events store a blob of that markdown, you've lost most of this. Go structured.

## Checklist to convert one skill

1. Write a JSON schema for its output. Cover every field you've ever given feedback about.
2. Rewrite the skill's output contract: "produce JSON matching schema X, no prose."
3. Add a validator step before downstream consumers see the output.
4. Write a renderer (50-150 lines) that consumes the JSON and produces the target.
5. Delete every markdown-emitting branch the skill previously had.
6. Store the JSON as an event payload. Render on demand.

Start with the skill that's caused the most review pain. Every subsequent migration takes about an hour after you've done the first one.

## Related patterns

- **Structured output modes.** If your model provider exposes `response_format: {"type": "json_schema", ...}` (OpenAI) or equivalent tool-use forcing (Anthropic), use it. Cuts the "malformed JSON" failure mode to near zero.
- **Tool-use as output contract.** Define a single `produce_recap` tool. The model is forced to call it. The tool arguments *are* the structured output. You never parse free-form text.
- **Pydantic models as schema.** Generate JSON Schema from Pydantic classes. Validate incoming payloads with `.model_validate()`. Keep schema + validator in one place.
