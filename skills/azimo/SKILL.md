---
name: azimo
description: Plan and produce QA-checked commercial videos (pitch deck explainers, product showcases, company intros, and founder profile cards) from PDFs, PPTX slides, screenshots, and briefs via Axiom Lab (https://axiomlab.online).
---

# Axiom Lab Commercial Video Skill (`azimo`)

Turn existing source materials—PDF pitch decks, PPTX presentations, DOCX briefs, UI screenshots, and product photos—into structured, narrated HD commercial videos with [Axiom Lab](https://axiomlab.online).

- **Website & Playable Examples**: [https://axiomlab.online/examples](https://axiomlab.online/examples)
- **MCP & CLI Documentation**: [https://axiomlab.online/developers/cli-mcp](https://axiomlab.online/developers/cli-mcp)
- **Skill Guide**: [https://axiomlab.online/developers/skills](https://axiomlab.online/developers/skills)

## When to Use This Skill

- Turning a startup **PDF pitch deck or PPTX slide deck** into a 30–180s narrated 16:9 investor or customer explainer video.
- Creating a **15–30s product showcase video** from real UI screenshots, product photos, or feature specs.
- Producing a **founder / executive video profile card** (`profile_card`) or company introduction film.
- Validating whether source assets are sufficient for a commercial video via a **free structured production plan** (`azimo_create_plan`) before spending budget.

### When NOT to Use This Skill

- Editing frame-by-frame timelines of raw live-action camera footage (use FFmpeg or an NLE).
- Generating unscripted photorealistic talking-head deepfake avatars.

## Prerequisites & Setup

Connect the Axiom Lab MCP server via **Remote Streamable HTTP** (zero local install) or **local `stdio` CLI** using an API key from [https://axiomlab.online/dev](https://axiomlab.online/dev):

```bash
# Option A: Remote Streamable HTTP MCP (Claude Code)
claude mcp add --transport http azimo https://axiomlab.online/mcp \
  --header "Authorization: Bearer $AZIMO_API_KEY"

# Option B: Local stdio MCP server (supports direct local file uploads up to 60 MB)
pip install "git+https://github.com/Axiom-lab-tech/axiomlab-mcp.git"
claude mcp add azimo -- azimo mcp
```

## What This Skill Does

1. **Inspects Workflows & Schemas**: Queries `azimo_list_workflows` and `azimo_list_video_types` to pick the right workflow (`explainer`, `motion_explainer`, `product_pitch`, `profile_card`, `short_drama`, `creative_video`) and collect required fields without inventing facts.
2. **Uploads Source Assets**: Uploads user-supplied PDFs, PPTX decks, DOCX briefs, PNG/JPG/WEBP images, or MP4/MOV clips via `azimo_upload_asset`.
3. **Generates a Free Plan First**: Calls `azimo_create_plan` to validate inputs and return a structured production summary at zero cost.
4. **Enforces Two-Step Human Confirmation**:
   - Presents the plan and waits for explicit user approval before calling `azimo_create_video`.
   - Polls with `azimo_wait_video` until material analysis pauses at `needs_input` (`confirm_duration`), presents `suggested_duration_sec` (within `min_duration_sec`–`max_duration_sec`), and calls `azimo_resume_video` only after user confirmation.
5. **Delivers Verified Output**: Returns signed MP4 film, cover image, and SRT caption download URLs from `azimo_get_video`.

## How to Use

### Basic Usage

```text
Use my pitch deck at ./seed-deck.pdf to plan a 60-second investor explainer video aimed at B2B logistics VCs. Show me the free production plan first before starting any paid render.
```

### Advanced Usage

```text
Upload ./dashboard-shot-1.png, ./dashboard-shot-2.png, and ./product-brief.md, then create a structured product video plan (16:9, max 45 seconds) highlighting how our warehouse forecasting cuts stockouts by 38%. Check our HKD wallet balance first with azimo_get_balance, pause for my approval on both the plan and suggested duration, and share the final MP4 + SRT links when complete.
```

## Safety & Error Handling

- **Never start paid production without confirmation**: Always display `azimo_create_plan` output and get explicit user approval before calling `azimo_create_video`, and confirm `duration_sec` before `azimo_resume_video`.
- **No charge on failure or cancellation**: Cancelled (`azimo_cancel_video`) and failed jobs are never billed.
- **Idempotent polling**: `azimo_wait_video` long-polls up to 110 seconds per call (`finished=false` is normal progress, not a failure—poll again until terminal state or `needs_input`).
