# Tools

## Special Tools & Capabilities

This agent has access to these tools and capabilities:

- **filesystem** — full file operations: `read_file`, `write_file`, `edit_file`, `list_directory`, `glob_files`, `grep_files`.
- **bash** — shell command execution. Used specifically for:
  - **Headless Chrome screenshot rendering**:
    `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=/tmp/preview.png --window-size=1280,800 "file:///path/to/file.html"`
  - **Vision model audit queries**: send base64-encoded screenshots to `http://localhost:4321/v1/chat/completions` with model `vision` to audit the rendered layout, typography, colors, and visual hierarchy.
  - Harmless file/port diagnostics.
- **context** — read shared conventions, planning docs, and meta KV.
- **help** — tool documentation lookup.
- **shared_meta_kv** — read/write shared instance metadata for cross-agent state.

---

*Common tools (time, list_directory, glob_files, inner_soul) are automatically loaded alongside the above.*
