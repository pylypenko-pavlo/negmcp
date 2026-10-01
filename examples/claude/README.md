# Claude Code templates

- `mcp.json` — register `negmcp serve` (copy into your project's `.mcp.json`, fix the path).
- `agents/` — subagents for an AI-assisted flow (copy into `.claude/agents/`):
  - `negmcp-colorist` — sets a roll's recipe and makes per-frame fixes (one colourist per roll
    for coherence; fixes can be spread over parallel instances);
  - `negmcp-judge-pro`, `negmcp-judge-editor`, `negmcp-judge-enthusiast` — the tribunal: three
    views on the LOOK only (craft, publishability, appeal), never on composition;
  - `negmcp-engineer` — pipeline mechanics (batch, QA, SoT, reproducibility), not colour.

Replace the placeholders in each file — `<NEGMCP>` (the `negmcp` executable of your venv),
`<PYTHON>` (that venv's python), `<LOOK_DIR>`, `<ROLLS_DIR>`, `<NEG_ROOT>`, `<REVIEW_ROOT>`,
`<WORK_DIR>`, `<REFS_DIR>`, `<REPO>` — with the values `negmcp start` prints. Your taste
lives in `<LOOK_DIR>/house_look.json` → `taste`; the agents read it from there, so the
templates themselves stay taste-free. Write your own house rules (procedure, gotchas) in a
document of your own and point the agents at it.
