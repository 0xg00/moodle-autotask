# Related assignment resources

Some Moodle submission activities contain only delivery instructions. Their
exercise statements live in separate pages or linked documents. An operator can
import the original source text, provenance and image transcriptions into an
explicit context file before the normal scheduler/approval/agent flow runs.
Do not include prewritten solutions. This is an explicit import, not automatic
discovery of related resources or external authenticated browsing.

Set `MOODLE_AUTOTASK_ASSIGNMENT_CONTEXT_FILE` to the same absolute JSON file for
the scheduler and worker (including any one-off controller commands):

```json
{
  "schema": "moodle-assignment-context-v1",
  "siteUrl": "https://moodle.example.edu",
  "assignments": [{
    "assignmentId": 123,
    "baseRevision": "moodle-assignment-v1:<64 lowercase hex characters>",
    "text": "Original source URL, retrieval date and SHA-256; source text and image transcription.",
    "submissionFormat": "archive"
  }]
}
```

`baseRevision` is the snapshot digest read without the context setting. Each
entry is tied to the exact site, assignment and upstream revision. The text and
format produce a new effective revision, invalidating any previous approval.
An upstream edit requires refreshing the imported sources and base revision.
Configured unreadable, oversized, linked or malformed files fail closed.
Use a root-owned regular file readable by the controller but not group/world
writable. The agent receives only the bound assignment snapshot, never this
file or Moodle credentials. URLs in the text are provenance, not fetch targets.

The central circuit already produces a verified ZIP of executor artifacts and
sends it with its report to Telegram. `submissionFormat: "archive"` disables
the existing Markdown-only Moodle submission offer and upload; it does not
automatically submit that ZIP. ZIP submission needs a separate exact-artifact
approval boundary. Existing Markdown assignments use `"markdown"`; assignments
without imported context retain their existing behavior and revision digests.
