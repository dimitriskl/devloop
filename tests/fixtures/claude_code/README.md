# Recorded Claude CLI output

These files are real `stream-json` output from the installed Claude Code CLI,
captured during the prototype for issue `0003-execute-attempt-via-claude-backend`
and committed so that `tests/test_claude_code_backend.py` is driven from provider
output rather than from hand-written guesses. No test in that module spawns a
provider executable.

| Fixture | What it records |
| --- | --- |
| `bypass-stream.jsonl` | A full attempt with **no** settings isolation, so the operator's own hooks, plugins and MCP servers loaded. This is the negative half of the isolation contrast, and the widest sample of event shapes. |
| `isolated-stream.jsonl` | The same kind of attempt **with** `--setting-sources project,local` and `--strict-mcp-config`, so no hook fired and no plugin or MCP server loaded. |
| `alias-resolution-stream.jsonl` | A run started from a short model alias, showing the concrete pinned model identifier the CLI resolved it to. |
| `permission-bypass.result.json` | The terminal `result` event under `bypassPermissions`: no permission denial. |
| `permission-dontask.result.json` | The terminal `result` event under `dontask`: a Bash denial, reported with `is_error: false` and a `success` subtype. |
| `permission-auto.result.json` | The same denial under `auto`. |
| `permission-acceptedits.result.json` | The same denial under `acceptEdits`. |

## These recordings are sanitised

This repository is published publicly, so the recordings were edited before being
committed. **The placeholders below are ours, not provider output — do not treat
them as evidence of how the CLI behaves, and do not "restore" them.** The
unaltered recordings are kept out of version control under
`docs/prd/claude-execution-backend/spike-fixtures/` for provenance.

Three categories of content were altered, plus two narrower fields noted in
category 3 (`tools` MCP names and plugin `version` values). Everything else is
byte-for-byte as recorded.

### 1. Absolute paths under the operator's home directory

Every absolute path that named the capturing machine's user profile was replaced
with an obviously synthetic path. The affected fields:

- `system/init` &rarr; `cwd` and `memory_paths.auto`, in `bypass-stream.jsonl`,
  `isolated-stream.jsonl` and `alias-resolution-stream.jsonl`.
- `system/init` &rarr; `plugins[].path`, in `bypass-stream.jsonl`.
- `assistant` &rarr; `tool_use` &rarr; `input.file_path` and `input.command`, and
  the matching `user` &rarr; `tool_result` &rarr; `content` and
  `tool_use_result.filePath`, in `bypass-stream.jsonl`.
- `result` &rarr; `permission_denials[].tool_input.command`, in
  `permission-auto.result.json`.

The synthetic replacements are `C:\fixtures\claude-spike\...` for the recorded
working directory and `C:\fixtures\claude-home\...` for the CLI's own home. No
test or translation logic reads these values, so only their shape is synthetic —
they are still Windows absolute paths.

### 2. Hook output text

`bypass-stream.jsonl` carries thirteen hook events (six `hook_started`, six
`hook_response`, one `hook_progress`). Their **number, order,
`subtype`, `hook_name`, `hook_id`, `exit_code` and `outcome` are untouched** —
that a personal hook fires at all when settings isolation is missing is the whole
point of the fixture. Only the payload text was removed, because it was the
capturing operator's own hook configuration:

- On every `system/hook_response` and `system/hook_progress` event, a non-empty
  `output` or `stdout` string was replaced with the literal
  `<hook output removed for publication>`.
- An **empty** `output`/`stdout`/`stderr` string was left empty, because "this
  hook produced nothing" is itself recorded behaviour.

### 3. The `system/init` tooling inventory

`bypass-stream.jsonl` was captured with no settings isolation, so its
`system/init` event enumerated the capturing machine's installed tooling by name
— every plugin and its marketplace and version, every MCP server including one
organisational integration, and every slash command, skill and agent, personal
ones included. All five of those lists were replaced with synthetic
placeholders:

| Field | Entries | Synthetic form |
| --- | --- | --- |
| `plugins` | 21 | `example-plugin-01` … `example-plugin-21`; `source` is `<name>@example-marketplace`; `path` is under `C:\fixtures\claude-home\plugins\cache\example-marketplace\` |
| `mcp_servers` | 7 | `plugin:example-mcp-plugin-1:example-server-1` … `-6`, plus one deliberately non-plugin-scoped `example-org-integration` |
| `slash_commands` | 126 | `example-command-001` … `example-command-126` |
| `skills` | 80 | `example-skill-01` … `example-skill-80` |
| `agents` | 24 | `example-agent-01` … `example-agent-24` |

**Every name in those five lists is ours.** None of them is a real plugin,
marketplace, version, skill, agent, slash command or MCP server, none is
provider output, and there is nothing here to "restore". The `example-` prefix
is the marker.

What was preserved, because these lists are load-bearing evidence:

- The element count of every list, exactly.
- Each element's key set and key order. The six `plugins` entries the CLI
  recorded without a `version` still have no `version` key, and the fifteen that
  had one still do. Every `mcp_servers` entry keeps its recorded `status`
  (`pending`, `needs-auth`, `connected`) — the spread of connection states is
  recorded provider behaviour, not machine configuration.
- That `plugins` and `mcp_servers` are non-empty here while they are empty in
  `isolated-stream.jsonl`, which is the isolation contrast itself.

One further field was altered in a later pass: the three `mcp__…` entries in the
`tools` array named a real installed plugin and its MCP server, so they were
renamed to `mcp__plugin_example-mcp-plugin-N__example_tool_N`. They therefore now
agree with the synthetic `mcp_servers` names. The other 37 `tools` entries are as
recorded. Plugin `version` values were also normalised to `1.0.0`, because an
exact installed version is itself machine-specific; only the presence or absence
of the `version` key is preserved per entry, not its recorded value.

`isolated-stream.jsonl` and `alias-resolution-stream.jsonl` needed no equivalent
edit and were left alone. Both were captured with settings isolation, so their
`plugins` and `mcp_servers` are empty, and their `slash_commands`, `skills` and
`agents` enumerate only the 44, 16 and 5 built-ins the CLI itself ships — no
installed plugin, personal skill or personal agent appears in them. Those lists
are exactly as recorded.

## What the tests rely on, and therefore must not be edited away

If you ever re-record or re-sanitise these files, preserve all of the following.

- Every event `type` and `subtype`, in the recorded order and count.
- The content-block structure of `assistant` and `user` messages
  (`thinking` / `tool_use` / `text` / `tool_result`), the `tool_use` ids, and
  their pairing with the matching `tool_result`.
- The terminal `result` object's `is_error`, `subtype`, `terminal_reason`,
  `api_error_status`, `permission_denials` (including `tool_name` and the key set
  of `tool_input`), `structured_output`, `result`, `usage`, `modelUsage`,
  `num_turns` and `total_cost_usd`.
- On `system/init`: the `model` value, the `permissionMode`, and the presence and
  emptiness-or-not of `plugins`, `mcp_servers`, `output_style` and
  `memory_paths`. The isolation contrast between `bypass-stream.jsonl` and
  `isolated-stream.jsonl` is exactly these being populated in the first and empty
  in the second. The *entries* inside those lists are synthetic (category 3
  above); their count, and whether the list is empty at all, are not.
- The fact and the count of the `hook_started`, `hook_response` and
  `hook_progress` events in `bypass-stream.jsonl`, against none at all in
  `isolated-stream.jsonl`.

Session UUIDs, model thinking signatures, timestamps, token counts and costs are
as recorded; they identify nothing.
