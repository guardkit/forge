# Repository Codex settings

Agent instructions and hooks are shared. The hooks find this checkout through
`git rev-parse --show-toplevel`, including when started in a subdirectory or a
linked worktree. They require Git and Python 3 on PATH.

MCP addresses belong to each machine. Copy `config.example.toml` to
`config.toml` and fill in the local endpoint, or use your user-level
`~/.codex/config.toml`. The project `config.toml` is ignored by Git. HTTP URLs
are literal settings, not shell expressions.

**Existing checkouts:** save your `.codex/config.toml` outside the repository
before pulling the change that removes it from tracking, then restore it at the
same path. It stays active locally and must not be added back to Git. The
working copy used to prepare this change has kept its original file unchanged.
Generated hook state is also ignored and kept locally.

Clarification examples use GuardKit's installed payload resolver (wheel or
editable installation), with the current user's agentecflow library as the
standalone alternative. They do not assume a sibling checkout or a home path.

References: [Codex hooks](https://developers.openai.com/codex/hooks) and
[MCP configuration](https://developers.openai.com/codex/mcp).
