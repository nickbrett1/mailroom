FROM python:3.12-slim
RUN pip install --no-cache-dir dagster-mcp
ENV DAGSTER_URL=http://mailroom:3003 \
    DAGSTER_READ_ONLY=false \
    DAGSTER_MCP_PORT=8770
EXPOSE 8770
# Serve the dagster-mcp FastMCP server over streamable HTTP (agents / goose /
# Open WebUI). Read-only by default (DAGSTER_READ_ONLY=true) — no run launch,
# terminate, or schedule/sensor start-stop from the MCP.
CMD ["python", "-c", "import os; from dagster_mcp.server import mcp; mcp.run(transport='http', host='0.0.0.0', port=int(os.environ.get('DAGSTER_MCP_PORT', '8770')))"]
