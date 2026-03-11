# OTC Log Tank Service (LTS) MCP Server

This MCP server provides a comprehensive interface for interacting with the **Open Telekom Cloud (OTC) Log Tank Service (LTS)**. It allows you to manage log groups, discover log streams, and perform advanced log queries directly from your AI assistant.

## Features

- **Project Management:** List available projects and switch the active context.
- **Discovery:** Browse log groups and streams across your entire project.
- **Advanced Querying:** Search logs using full-text keywords, specific time ranges (ISO 8601 or relative), and pagination support.
- **Analytics:** Retrieve log volume histograms and counts without fetching full log content.
- **Seamless Authentication:** Uses standard `clouds.yaml` configuration.

## Available Tools

### Project Management
- `list_available_projects`: Lists all OTC projects available to the authenticated user.
- `set_active_project`: Switches the active project for all subsequent operations.

### Log Discovery
- `list_log_groups`: Lists all LTS log groups in the current project.
- `list_log_streams_in_group`: Lists all log streams within a specific log group (supports tag filtering).
- `list_all_log_streams`: Performs a cross-group search for log streams by name.

### Log Querying & Analytics
- `query_logs`: Fetches raw log content from a stream using relative time (e.g., `hours_back`), keyword filters, and pagination.
- `query_logs_with_time_range`: Fetches raw logs using explicit ISO 8601 timestamps.
- `count_logs`: Returns the total number of log entries matching a query without downloading content.
- `query_log_histogram`: Returns a time-based histogram (log count per time bucket) for analyzing activity spikes.

## Setup

### Prerequisites

1. **Python & uv:** Ensure you have Python installed. We recommend using `uv` for easy dependency management.
2. **OTC Credentials:** A valid `clouds.yaml` file is required. By default, the server looks for it in:
   - `~/.config/openstack/clouds.yaml`
   - `/etc/openstack/clouds.yaml`
   - The current working directory.
   - Alternatively, set the `OS_CLIENT_CONFIG_FILE` environment variable.

### Configuration

The server uses the `otc` cloud profile by default. You can override this by setting the `OS_CLOUD` environment variable.

## Installation

### For Claude Desktop

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "otc-logtank": {
      "command": "uv",
      "args": [
        "run",
        "/path/to/otc_logtank_mcp_server/otc_logtank_mcp.py"
      ],
      "env": {
        "OS_CLIENT_CONFIG_FILE": "/path/to/your/clouds.yaml",
        "OS_CLOUD": "otc"
      }
    }
  }
}
```

### Direct Execution

You can run the server directly using `uv`:

```bash
export OS_CLIENT_CONFIG_FILE=/path/to/clouds.yaml
uv run otc_logtank_mcp.py
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
