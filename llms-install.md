# Installing Axiom Lab MCP Server (`azimo`) for Cline & AI Agents

Axiom Lab provides two zero-friction ways to configure the MCP server. **Prefer Option 1 (Local `stdio` via Python 3.9+)** when local file upload (`azimo_upload_asset` with local file paths) is needed, or **Option 2 (Remote Streamable HTTP)** when zero local installation is preferred.

## Required User Credential

Ask the user for their **Axiom Lab API Key** (`AZIMO_API_KEY`, starts with `vg_...`), which can be created at `https://axiomlab.online/dev`. Do **not** hardcode placeholder keys.

---

## Option 1: Local `stdio` MCP Server (Recommended for Cline)

1. Verify Python 3.9+ is available:
   ```bash
   python3 --version
   ```
2. Install `vidgen-sdk` (`azimo` CLI + `stdio` MCP server, zero external runtime dependencies—uses Python standard library only):
   ```bash
   python3 -m pip install --user "git+https://github.com/Axiom-lab-tech/axiomlab-mcp.git"
   ```
   *(If PEP 668 externally-managed-environment applies on macOS/Linux, create an isolated venv at `~/.axiomlab-mcp-venv` and run `~/.axiomlab-mcp-venv/bin/pip install "git+https://github.com/Axiom-lab-tech/axiomlab-mcp.git"`, then use `~/.axiomlab-mcp-venv/bin/azimo` as `command`.)*
3. Add to `cline_mcp_settings.json`:
   ```json
   {
     "mcpServers": {
       "axiomlab": {
         "command": "azimo",
         "args": ["mcp"],
         "env": {
           "AZIMO_API_KEY": "<USER_PROVIDED_AZIMO_API_KEY>"
         },
         "disabled": false,
         "autoApprove": [
           "azimo_list_workflows",
           "azimo_list_playground",
           "azimo_list_video_types",
           "azimo_list_assets",
           "azimo_get_video",
           "azimo_list_videos",
           "azimo_get_balance"
         ]
       }
     }
   }
   ```

---

## Option 2: Remote Streamable HTTP MCP Server (Zero Local Install)

If the client supports remote `streamableHttp` MCP servers directly:

```json
{
  "mcpServers": {
    "axiomlab": {
      "type": "streamableHttp",
      "url": "https://axiomlab.online/mcp",
      "headers": {
        "Authorization": "Bearer <USER_PROVIDED_AZIMO_API_KEY>"
      },
      "disabled": false
    }
  }
}
```

## Verification

After adding the server, call the read-only tool `azimo_list_workflows` to confirm connectivity. Never call `azimo_create_video` without showing `azimo_create_plan` output and obtaining explicit user confirmation first.
