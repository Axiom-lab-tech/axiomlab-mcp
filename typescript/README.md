# Axiom Lab TypeScript SDK (`@vidgen/sdk`)

Zero runtime dependencies, Node.js 22+ or modern server runtimes. Official typed client for **[Axiom Lab](https://axiomlab.online)** — turn PDF pitch decks, slides, UI screenshots, and product photos into QA-checked commercial videos. Keep API keys on a trusted server.

- **Website**: [https://axiomlab.online](https://axiomlab.online)
- **Developer & API Docs**: [https://axiomlab.online/developers](https://axiomlab.online/developers)
- **MCP Integration**: [https://axiomlab.online/developers/cli-mcp](https://axiomlab.online/developers/cli-mcp)
- **Public Video Demonstrations**: [https://axiomlab.online/examples](https://axiomlab.online/examples)

## Build & Test

```sh
npm ci
npm test
npm pack
```

## Quickstart

```ts
import { readFile } from "node:fs/promises";
import { Vidgen } from "@vidgen/sdk";

const api = new Vidgen("https://axiomlab.online", process.env.AZIMO_API_KEY!);
const asset = await api.upload(new Blob([await readFile("deck.pdf")]), "deck.pdf"); // material is required
const plan = await api.plan(
  {
    prompt: "Explain our inventory platform to warehouse managers",
    assets: [asset.id],
    options: { duration_max: 60, workflow: "explainer" },
  },
  "launch-plan-001",
);

if (plan.data.status === "ready") {
  const job = await api.create({ plan_id: plan.id }, "launch-video-001");
  let video = await api.wait(job.id);
  const stop = video.result?.input_required;
  if (stop?.code === "confirm_duration") {
    // material analysis complete: confirm suggested film length before rendering
    await api.confirmDuration(job.id, Number(stop.suggested_duration_sec), "launch-length-001");
    video = await api.wait(job.id);
  }
}

for await (const video of api.videos({ status: "complete" })) {
  console.log(video.id);
}
```

## Semantics & Retries

- `wait()` returns for `needs_input`, `failed`, `rejected`, `cancelled`, or `complete`; inspect the status before using files.
- Every workflow except `profile_card` stops once at `needs_input` with `code: confirm_duration`: answer it with `confirmDuration()` (within `min_duration_sec`–`max_duration_sec`) and wait again.
- Mutating helpers send an idempotency key. Transport errors and `429`/`500`/`502`/`503`/`504` retry only when safe, honoring `Retry-After`.
- `VidgenError` exposes `status`, `code`, `details`, and `requestId`. `paginate()` and list iterators follow opaque cursors.
- `verifyWebhook()` validates the original body bytes with Web Crypto.
