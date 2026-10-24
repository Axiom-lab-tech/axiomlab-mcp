# Axiom Lab MCP Server, CLI & SDKs (`azimo`)

Official **Model Context Protocol (MCP) server**, `azimo` command-line client, Python SDK, and TypeScript SDK for **[Axiom Lab](https://axiomlab.online)** — turn PDF pitch decks, PPTX slides, UI screenshots, product photos, and written briefs into QA-checked commercial videos.

- **Website**: [https://axiomlab.online](https://axiomlab.online)
- **MCP & Agent Guide**: [https://axiomlab.online/developers/cli-mcp](https://axiomlab.online/developers/cli-mcp)
- **REST API Reference**: [https://axiomlab.online/developers/api](https://axiomlab.online/developers/api)
- **Claude Skill (`SKILL.md`)**: [`skills/azimo/SKILL.md`](skills/azimo/SKILL.md) · [Online Guide](https://axiomlab.online/developers/skills)
- **Full Playable Examples**: [https://axiomlab.online/examples](https://axiomlab.online/examples)
- **Free Pitch & Startup Calculators**: [https://axiomlab.online/tools](https://axiomlab.online/tools)

---

## Two Ways to Connect via MCP

### Option 1: Remote Streamable HTTP MCP (Zero Local Install)

Point Claude Code, Cursor, or any Streamable HTTP MCP client directly at `https://axiomlab.online/mcp` using an API key from [https://axiomlab.online/dev](https://axiomlab.online/dev):

```bash
claude mcp add --transport http azimo https://axiomlab.online/mcp \
  --header "Authorization: Bearer $AZIMO_API_KEY"
```

Or in `~/.cursor/mcp.json` / `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "azimo": {
      "url": "https://axiomlab.online/mcp",
      "headers": {
        "Authorization": "Bearer ${env:AZIMO_API_KEY}"
      }
    }
  }
}
```

### Option 2: Local `stdio` MCP Server (`azimo mcp`)

Run the local `stdio` MCP server directly on macOS, Linux, or Windows (enables direct local file path uploads up to 60 MB per file without base64 inline limits):

```bash
pip install "git+https://github.com/Axiom-lab-tech/axiomlab-mcp.git"
```

Configure in `claude_desktop_config.json` or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "azimo": {
      "command": "azimo",
      "args": ["mcp"],
      "env": {
        "AZIMO_API_KEY": "vg_..."
      }
    }
  }
}
```

Or launch ephemeral via `uvx`:

```bash
uvx --from "git+https://github.com/Axiom-lab-tech/axiomlab-mcp.git" azimo mcp
```

---

## Available MCP Tools

| Tool | Description |
| --- | --- |
| `azimo_list_workflows` | List supported video workflows (`explainer`, `motion_explainer`, `product_pitch`, `profile_card`, etc.) and duration bounds |
| `azimo_list_video_types` | Inspect structured fields for `project`, `product`, `personal`, `corporate`, and `name_card` videos |
| `azimo_upload_asset` | Upload source PDFs, PPTX decks, DOCX briefs, PNG/JPG/WEBP product photos, or reference MP4/MOV clips |
| `azimo_create_plan` | Validate material and generate a **free** structured production plan before any paid rendering starts |
| `azimo_get_plan` | Fetch an existing production plan and inspect requirements |
| `azimo_create_video` | Start video production from a confirmed `ready` plan (idempotent per plan) |
| `azimo_wait_video` | Poll until production finishes or pauses at `needs_input` (`confirm_duration`) after reading uploaded material |
| `azimo_resume_video` | Confirm the suggested film duration (in seconds) or approve a paused step |
| `azimo_get_video` | Retrieve job status, QA results, and signed MP4 film + cover download links |
| `azimo_list_videos` | Paginate finished or in-progress videos in the active project |
| `azimo_cancel_video` | Cancel a queued or paused job (cancelled and failed jobs are never charged) |
| `azimo_get_balance` | Check available and reserved prepaid HKD wallet balance |

**Safety by design**: Plans are always free. Production and billing only start after explicit confirmation, and `viewer`-role API keys can inspect plans and progress without permission to spend balance.

---

## Python SDK

```python
import os
from vidgen_sdk import Vidgen

with Vidgen("https://axiomlab.online", os.environ["AZIMO_API_KEY"]) as api:
    asset = api.upload_asset("deck.pdf")
    plan = api.create_plan(
        {
            "prompt": "Explain in 60 seconds how our inventory platform cuts warehouse stockouts",
            "assets": [asset["id"]],
            "options": {"workflow": "auto", "duration_max": 60},
        },
        idempotency_key="launch-plan-001",
    )
    if plan["data"]["status"] == "ready":
        job = api.create_video(plan_id=plan["id"], idempotency_key="launch-video-001")
        final = api.wait(job["id"])
        stop = (final.get("result") or {}).get("input_required") or {}
        if stop.get("code") == "confirm_duration":
            api.confirm_duration(job["id"], stop["suggested_duration_sec"], idempotency_key="launch-length-001")
            final = api.wait(job["id"])
```

## TypeScript SDK (`typescript/`)

See [`typescript/README.md`](typescript/README.md) for the zero-dependency Node.js 22+ / Web Crypto TypeScript client (`@vidgen/sdk`).

## License

[MIT](LICENSE) © [Axiom Lab](https://axiomlab.online)
