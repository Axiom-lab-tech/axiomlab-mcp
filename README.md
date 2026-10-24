# Axiom Lab MCP Server, CLI & SDKs (`azimo`)

Official **Model Context Protocol (MCP) server**, `azimo` command-line client, Python SDK, and TypeScript SDK for **[Axiom Lab](https://axiomlab.online)** — turn PDF pitch decks, PPTX slides, UI screenshots, product photos, and written briefs into QA-checked commercial videos.

- **Website**: [https://axiomlab.online](https://axiomlab.online)
- **Official MCP Registry**: [`io.github.Axiom-lab-tech/axiomlab-mcp`](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.Axiom-lab-tech%2Faxiomlab-mcp/versions/latest)
- **MCP & Agent Guide**: [English](https://axiomlab.online/developers/cli-mcp) · [中文文档](https://axiomlab.online/zh/developers/cli-mcp)
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

## Available MCP Tools (14 Tools, 4 Prompts, 2 Resources)

| Tool | Description |
| --- | --- |
| `azimo_list_workflows` | List supported video workflows (`explainer`, `motion_explainer`, `product_pitch`, `profile_card`, `short_drama`, `creative_video`), durations, and aspect ratios |
| `azimo_list_playground` | List ready-made starting templates (project intro, person card, product film, company intro, short-drama ad, e-commerce ad) with runnable examples |
| `azimo_list_video_types` | Inspect structured fields for `project`, `product`, `personal`, `corporate`, and `name_card` videos |
| `azimo_upload_asset` | Upload source PDFs, PPTX decks, DOCX briefs, PNG/JPG/WEBP photos, MP4/MOV clips, or audio references |
| `azimo_list_assets` | List files uploaded to the current project with their `asset_id`s |
| `azimo_create_plan` | Validate material and generate a **free** structured production plan before any paid rendering starts |
| `azimo_create_video` | Start paid video production from a confirmed `ready` plan (idempotent per `plan_id`) |
| `azimo_get_video` | Retrieve job status, stage, progress, and signed MP4 film + cover (+ SRT) download links |
| `azimo_wait_video` | Long-poll until production reaches a terminal state or pauses at `needs_input` (`confirm_duration`) |
| `azimo_list_videos` | List recent videos in the active project with status |
| `azimo_cancel_video` | Cancel a queued, waiting, or running video job before its next paid step |
| `azimo_approve_budget` | Authorize extra spending when a video stops with an approvable `budget_approval` request |
| `azimo_resume_video` | Confirm the suggested film duration (`duration_sec`, 30–180s) or continue a paused `needs_input` video |
| `azimo_get_balance` | Check total, reserved, and available prepaid HKD wallet balance |

- **MCP Prompts (4)**: `create_product_video`, `create_company_intro`, `create_project_intro`, `create_person_card`
- **MCP Resources (2)**: `azimo://workflows`, `azimo://video-types`

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
