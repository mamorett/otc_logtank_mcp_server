# /// script
# dependencies = [
#     "fastmcp",
#     "requests",
#     "pyyaml",
# ]
# ///

"""
OTC Telekom Cloud MCP Server for LTS (Log Tank Service)
Supports log group management, log stream management, and log querying.
Authentication is based on clouds.yaml (OS_CLIENT_CONFIG_FILE / OS_CLOUD env vars supported).
"""

import os
import time
import datetime
import logging
import json
import yaml
from typing import Any, Dict, List, Optional
from pathlib import Path
import requests
from fastmcp import FastMCP

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("otc-lts-mcp-server")


# ---------------------------------------------------------------------------
# clouds.yaml loader (identical to original, kept verbatim)
# ---------------------------------------------------------------------------

def load_clouds_yaml(cloud_name: str = "otc") -> Dict[str, Any]:
    """
    Load cloud configuration.
    Priority 1: OS_CLIENT_CONFIG_FILE environment variable.
    Priority 2: Standard OpenStack search paths.
    """
    custom_path_env = os.getenv("OS_CLIENT_CONFIG_FILE")
    search_paths = []

    if custom_path_env:
        custom_path = Path(custom_path_env)
        if not custom_path.exists():
            raise FileNotFoundError(
                f"OS_CLIENT_CONFIG_FILE is set to '{custom_path}' but file does not exist."
            )
        search_paths.append(custom_path)
        logger.info(f"Using configuration file from OS_CLIENT_CONFIG_FILE: {custom_path}")
    else:
        search_paths = [
            Path.cwd() / "clouds.yaml",
            Path.home() / ".config" / "openstack" / "clouds.yaml",
            Path("/etc/openstack/clouds.yaml"),
        ]

    config = None
    used_path = None

    for path in search_paths:
        if path.exists():
            try:
                with open(path, "r") as f:
                    data = yaml.safe_load(f)
                    if data and "clouds" in data and cloud_name in data["clouds"]:
                        config = data["clouds"][cloud_name]
                        used_path = path
                        break
            except Exception as e:
                logger.warning(f"Error reading {path}: {e}")
                if custom_path_env:
                    raise ValueError(
                        f"Failed to parse explicitly requested config file {path}: {e}"
                    )

    if not config:
        if custom_path_env:
            raise ValueError(f"Cloud '{cloud_name}' not found in {custom_path_env}")
        else:
            raise FileNotFoundError(
                f"Could not find configuration for cloud '{cloud_name}' in standard locations."
            )

    logger.info(f"Loaded '{cloud_name}' configuration from {used_path}")

    auth = config.get("auth", {})
    return {
        "auth_url": auth.get("auth_url"),
        "username": auth.get("username"),
        "password": auth.get("password"),
        "domain_name": auth.get("user_domain_name") or auth.get("domain_name"),
        "project_name": auth.get("project_name"),
        "region": config.get("region_name", "eu-de"),
    }


# ---------------------------------------------------------------------------
# OTC LTS Client
# ---------------------------------------------------------------------------

class OTCLTSClient:
    """
    OTC Log Tank Service (LTS) client.

    API base: https://lts.{region}.otc.t-systems.com/v2/{project_id}/
    All requests use IAM token authentication (X-Auth-Token header).

    Implemented operations
    ----------------------
    Log Groups
        GET  /v2/{project_id}/groups                    → list_log_groups
    Log Streams
        GET  /v2/{project_id}/groups/{group_id}/streams → list_log_streams_in_group
        GET  /v2/{project_id}/log-streams               → list_all_log_streams  (query params: log_group_name, etc.)
    Log Querying
        POST /v2/{project_id}/groups/{group_id}/streams/{stream_id}/content/query
                                                        → query_logs
        POST /v2/{project_id}/groups/{group_id}/streams/{stream_id}/content/query
             with  ?type=count                          → query_logs_by_time_segment
    """

    def __init__(
        self,
        auth_url: str,
        username: str,
        password: str,
        domain_name: str,
        project_name: str,
        region: str,
    ):
        self.auth_url = auth_url
        self.username = username
        self.password = password
        self.domain_name = domain_name
        self.project_name = project_name
        self.region = region

        self.token: Optional[str] = None
        self.project_id: Optional[str] = None
        self.token_expires: Optional[str] = None

        self._lts_base: str = f"https://lts.{region}.otc.t-systems.com"

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Auth-Token": self.token or "",
        }

    def authenticate(self) -> bool:
        """Authenticate with OTC IAM and obtain a scoped project token."""
        auth_data = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.username,
                            "password": self.password,
                            "domain": {"name": self.domain_name},
                        }
                    },
                },
                "scope": {"project": {"name": self.project_name}},
            }
        }
        try:
            base_url = self.auth_url.rstrip("/")
            resp = requests.post(
                f"{base_url}/auth/tokens",
                json=auth_data,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code == 201:
                self.token = resp.headers["X-Subject-Token"]
                data = resp.json()
                self.project_id = data["token"]["project"]["id"]
                self.token_expires = data["token"]["expires_at"]
                logger.info(f"Authentication successful. Token expires at {self.token_expires}")
                return True
            else:
                logger.error(f"Authentication failed: {resp.status_code} – {resp.text}")
                return False
        except Exception as exc:
            logger.error(f"Authentication error: {exc}")
            return False

    def check_token_validity(self) -> bool:
        """Renew token if missing or expiring within 5 minutes."""
        if not self.token or not self.token_expires:
            return self.authenticate()
        try:
            expiry = datetime.datetime.strptime(self.token_expires, "%Y-%m-%dT%H:%M:%S.%fZ")
            if (expiry - datetime.datetime.utcnow()).total_seconds() < 300:
                return self.authenticate()
            return True
        except Exception as exc:
            logger.error(f"Token validation error: {exc}")
            return self.authenticate()

    def set_project(self, project_name: str) -> bool:
        """Switch to a different project and re-authenticate."""
        self.project_name = project_name
        return self.authenticate()

    def list_projects(self) -> Optional[List[Dict]]:
        """List all projects available to the authenticated user (domain-scoped token)."""
        auth_data = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.username,
                            "password": self.password,
                            "domain": {"name": self.domain_name},
                        }
                    },
                },
                "scope": {"domain": {"name": self.domain_name}},
            }
        }
        try:
            base_url = self.auth_url.rstrip("/")
            resp = requests.post(
                f"{base_url}/auth/tokens",
                json=auth_data,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code != 201:
                logger.error(f"Domain auth failed: {resp.status_code} – {resp.text}")
                return None
            domain_token = resp.headers["X-Subject-Token"]
            projects_resp = requests.get(
                f"{base_url}/auth/projects",
                headers={"Content-Type": "application/json", "X-Auth-Token": domain_token},
                timeout=30,
            )
            if projects_resp.status_code == 200:
                return projects_resp.json().get("projects", [])
            logger.error(f"List projects failed: {projects_resp.status_code} – {projects_resp.text}")
            return None
        except Exception as exc:
            logger.error(f"Error listing projects: {exc}")
            return None

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        if not self.check_token_validity():
            return None
        url = f"{self._lts_base}{path}"
        try:
            resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            logger.error(f"GET {url} failed: {resp.status_code} – {resp.text}")
            return None
        except Exception as exc:
            logger.error(f"GET {url} error: {exc}")
            return None

    def _post(self, path: str, body: Dict, params: Optional[Dict] = None) -> Optional[Dict]:
        if not self.check_token_validity():
            return None
        url = f"{self._lts_base}{path}"
        try:
            resp = requests.post(
                url, headers=self._auth_headers(), json=body, params=params, timeout=60
            )
            if resp.status_code in (200, 201):
                return resp.json()
            logger.error(f"POST {url} failed: {resp.status_code} – {resp.text}")
            return None
        except Exception as exc:
            logger.error(f"POST {url} error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Log Group Management
    # ------------------------------------------------------------------

    def list_log_groups(self) -> Optional[Dict]:
        """
        GET /v2/{project_id}/groups
        Returns all log groups for the current project.
        Response: { "log_groups": [ { "log_group_id", "log_group_name", "ttl_in_days", ... } ] }
        """
        return self._get(f"/v2/{self.project_id}/groups")

    # ------------------------------------------------------------------
    # Log Stream Management
    # ------------------------------------------------------------------

    def list_log_streams_in_group(self, log_group_id: str, tag: Optional[str] = None) -> Optional[Dict]:
        """
        GET /v2/{project_id}/groups/{log_group_id}/streams
        Lists all log streams inside a specific log group.
        Optional ?tag=<tag_name> filter.
        Response: { "log_streams": [ { "log_stream_id", "log_stream_name", "ttl_in_days", ... } ] }
        """
        params = {}
        if tag:
            params["tag"] = tag
        return self._get(
            f"/v2/{self.project_id}/groups/{log_group_id}/streams",
            params=params or None,
        )

    def list_all_log_streams(
        self,
        log_group_name: Optional[str] = None,
        log_stream_name: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        GET /v2/{project_id}/log-streams
        Query log streams across the project, optionally filtered by group/stream name.
        Response: { "log_streams": [ { "log_group_name", "log_stream_id", "log_stream_name", ... } ] }
        """
        params: Dict[str, str] = {}
        if log_group_name:
            params["log_group_name"] = log_group_name
        if log_stream_name:
            params["log_stream_name"] = log_stream_name
        return self._get(f"/v2/{self.project_id}/log-streams", params=params or None)

    # ------------------------------------------------------------------
    # Log Querying
    # ------------------------------------------------------------------

    def query_logs(
        self,
        log_group_id: str,
        log_stream_id: str,
        start_time_ms: int,
        end_time_ms: int,
        keywords: Optional[str] = None,
        limit: int = 100,
        line_num: Optional[str] = None,
        is_count: bool = False,
    ) -> Optional[Dict]:
        """
        POST /v2/{project_id}/groups/{log_group_id}/streams/{log_stream_id}/content/query

        Query raw logs within a time range.

        Body parameters:
          start_time   – epoch milliseconds (required)
          end_time     – epoch milliseconds (required)
          keywords     – full-text search expression (optional)
          limit        – max log entries to return (1-10000, default 100)
          line_num     – pagination cursor from a previous response (optional)
          is_count     – if True, return only the count, not log content

        Response (is_count=False):
          {
            "logs": [ { "line_num", "content", "log_time_ms" } ],
            "is_query_complete": bool
          }
        Response (is_count=True):
          { "count": int }
        """
        body: Dict[str, Any] = {
            "start_time": str(start_time_ms),
            "end_time": str(end_time_ms),
            "limit": limit,
            "is_count": is_count,
        }
        if keywords:
            body["keywords"] = keywords
        if line_num:
            body["line_num"] = line_num

        path = (
            f"/v2/{self.project_id}/groups/{log_group_id}"
            f"/streams/{log_stream_id}/content/query"
        )
        return self._post(path, body)

    def query_logs_by_time_segment(
        self,
        log_group_id: str,
        log_stream_id: str,
        start_time_ms: int,
        end_time_ms: int,
        keywords: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        POST /v2/{project_id}/groups/{log_group_id}/streams/{log_stream_id}/content/query
             ?type=count

        Returns log volume broken down by time segment (histogram) for the
        specified time window.  Useful for spotting spikes without fetching
        every log line.

        Response: { "histogram": [ { "time": epoch_ms, "count": int } ] }
        """
        body: Dict[str, Any] = {
            "start_time": str(start_time_ms),
            "end_time": str(end_time_ms),
        }
        if keywords:
            body["keywords"] = keywords

        path = (
            f"/v2/{self.project_id}/groups/{log_group_id}"
            f"/streams/{log_stream_id}/content/query"
        )
        return self._post(path, body, params={"type": "count"})


# ---------------------------------------------------------------------------
# Global singleton client
# ---------------------------------------------------------------------------

_lts_client: Optional[OTCLTSClient] = None


def get_client() -> OTCLTSClient:
    global _lts_client
    if _lts_client:
        return _lts_client

    cloud_profile = os.getenv("OS_CLOUD", "otc")
    config = load_clouds_yaml(cloud_profile)

    required = ["auth_url", "username", "password", "domain_name", "project_name"]
    missing = [f for f in required if not config.get(f)]
    if missing:
        raise ValueError(
            f"Missing required fields in clouds.yaml for profile '{cloud_profile}': {', '.join(missing)}"
        )

    logger.info(f"Initialising OTC LTS Client – project: {config['project_name']} (profile: {cloud_profile})")
    client = OTCLTSClient(
        auth_url=config["auth_url"],
        username=config["username"],
        password=config["password"],
        domain_name=config["domain_name"],
        project_name=config["project_name"],
        region=config["region"],
    )

    if not client.authenticate():
        raise RuntimeError("Initial authentication failed. Check credentials in clouds.yaml.")

    _lts_client = client
    return _lts_client


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("OTC Log Tank Service (LTS)")


# ── Project management ──────────────────────────────────────────────────────

@mcp.tool()
def list_available_projects() -> str:
    """List all projects available to the authenticated user."""
    try:
        client = get_client()
        projects = client.list_projects()
        if projects is None:
            return "Failed to retrieve projects."
        return json.dumps({"projects": projects}, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def set_active_project(project_name: str) -> str:
    """Switch the active project for all subsequent operations."""
    try:
        client = get_client()
        if client.set_project(project_name):
            return f"Successfully switched to project: {project_name}"
        return f"Failed to switch to project '{project_name}'. Check logs for details."
    except Exception as exc:
        return f"Error: {exc}"


# ── Log Group Management ────────────────────────────────────────────────────

@mcp.tool()
def list_log_groups() -> str:
    """
    List all LTS log groups in the current project.

    Returns a JSON object with a 'log_groups' array. Each element contains
    at least: log_group_id, log_group_name, ttl_in_days, creation_time.
    """
    try:
        client = get_client()
        result = client.list_log_groups()
        if result is None:
            return "Failed to retrieve log groups."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


# ── Log Stream Management ───────────────────────────────────────────────────

@mcp.tool()
def list_log_streams_in_group(log_group_id: str, tag: Optional[str] = None) -> str:
    """
    List all log streams within a specific log group.

    Parameters
    ----------
    log_group_id : str
        The UUID of the log group (obtain from list_log_groups).
    tag : str, optional
        Filter streams by tag name.

    Returns a JSON object with a 'log_streams' array. Each element contains
    at least: log_stream_id, log_stream_name, ttl_in_days, creation_time.
    """
    try:
        client = get_client()
        result = client.list_log_streams_in_group(log_group_id, tag=tag)
        if result is None:
            return "Failed to retrieve log streams."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def list_all_log_streams(
    log_group_name: Optional[str] = None,
    log_stream_name: Optional[str] = None,
) -> str:
    """
    Query log streams across the entire project (cross-group search).

    Parameters
    ----------
    log_group_name : str, optional
        Filter by log group name (partial match supported).
    log_stream_name : str, optional
        Filter by log stream name (partial match supported).

    Returns a JSON object with a 'log_streams' array that includes
    log_group_name alongside the stream details.
    """
    try:
        client = get_client()
        result = client.list_all_log_streams(
            log_group_name=log_group_name,
            log_stream_name=log_stream_name,
        )
        if result is None:
            return "Failed to retrieve log streams."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


# ── Log Querying ────────────────────────────────────────────────────────────

@mcp.tool()
def query_logs(
    log_group_id: str,
    log_stream_id: str,
    hours_back: float = 1.0,
    keywords: Optional[str] = None,
    limit: int = 100,
    line_num: Optional[str] = None,
) -> str:
    """
    Query raw log content from a specific log stream.

    Parameters
    ----------
    log_group_id : str
        UUID of the log group.
    log_stream_id : str
        UUID of the log stream.
    hours_back : float
        How many hours back to search (default 1). Can be fractional, e.g. 0.5 for 30 minutes.
    keywords : str, optional
        Full-text search expression (LTS search syntax).
        Examples: "ERROR", "timeout AND db", "status:500".
        Leave empty to return all logs in the time window.
    limit : int
        Maximum number of log lines to return (1–10 000, default 100).
    line_num : str, optional
        Pagination cursor – pass the 'line_num' value from the last entry
        of a previous response to fetch the next page.

    Returns
    -------
    JSON with:
      - logs: list of log entries, each with 'line_num', 'content', 'log_time_ms'
      - is_query_complete: whether the query finished without being truncated
    """
    try:
        client = get_client()
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - int(hours_back * 3600 * 1000)

        result = client.query_logs(
            log_group_id=log_group_id,
            log_stream_id=log_stream_id,
            start_time_ms=from_ms,
            end_time_ms=now_ms,
            keywords=keywords,
            limit=min(max(1, limit), 10000),
            line_num=line_num,
        )
        if result is None:
            return "Failed to query logs."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def query_logs_with_time_range(
    log_group_id: str,
    log_stream_id: str,
    start_time_iso: str,
    end_time_iso: str,
    keywords: Optional[str] = None,
    limit: int = 100,
    line_num: Optional[str] = None,
) -> str:
    """
    Query raw log content using explicit ISO 8601 timestamps.

    Parameters
    ----------
    log_group_id : str
        UUID of the log group.
    log_stream_id : str
        UUID of the log stream.
    start_time_iso : str
        Start of the time window in ISO 8601 format, e.g. '2025-02-25T08:00:00Z'.
    end_time_iso : str
        End of the time window in ISO 8601 format, e.g. '2025-02-25T09:00:00Z'.
    keywords : str, optional
        Full-text / field search expression (LTS syntax).
    limit : int
        Maximum log lines to return (1–10 000, default 100).
    line_num : str, optional
        Pagination cursor from a previous response.

    Returns
    -------
    JSON with 'logs' list and 'is_query_complete' flag.
    """
    try:
        def _parse_iso(ts: str) -> int:
            ts = ts.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(ts)
            return int(dt.timestamp() * 1000)

        start_ms = _parse_iso(start_time_iso)
        end_ms = _parse_iso(end_time_iso)

        if start_ms >= end_ms:
            return "Error: start_time_iso must be before end_time_iso."

        client = get_client()
        result = client.query_logs(
            log_group_id=log_group_id,
            log_stream_id=log_stream_id,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            keywords=keywords,
            limit=min(max(1, limit), 10000),
            line_num=line_num,
        )
        if result is None:
            return "Failed to query logs."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def count_logs(
    log_group_id: str,
    log_stream_id: str,
    hours_back: float = 1.0,
    keywords: Optional[str] = None,
) -> str:
    """
    Count log entries matching optional keywords without fetching log content.

    Parameters
    ----------
    log_group_id : str
        UUID of the log group.
    log_stream_id : str
        UUID of the log stream.
    hours_back : float
        How many hours back to count (default 1).
    keywords : str, optional
        Search expression to filter before counting.

    Returns
    -------
    JSON with a single 'count' field.
    """
    try:
        client = get_client()
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - int(hours_back * 3600 * 1000)

        result = client.query_logs(
            log_group_id=log_group_id,
            log_stream_id=log_stream_id,
            start_time_ms=from_ms,
            end_time_ms=now_ms,
            keywords=keywords,
            is_count=True,
        )
        if result is None:
            return "Failed to count logs."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def query_log_histogram(
    log_group_id: str,
    log_stream_id: str,
    hours_back: float = 1.0,
    keywords: Optional[str] = None,
) -> str:
    """
    Retrieve a time-based histogram of log volume (log count per time bucket).

    Useful for visualising log activity over time or detecting anomaly spikes
    without transferring the full log content.

    Parameters
    ----------
    log_group_id : str
        UUID of the log group.
    log_stream_id : str
        UUID of the log stream.
    hours_back : float
        Width of the query window in hours (default 1).
    keywords : str, optional
        Optional search expression to filter counted entries.

    Returns
    -------
    JSON with a 'histogram' array where each element has 'time' (epoch ms) and 'count'.
    """
    try:
        client = get_client()
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - int(hours_back * 3600 * 1000)

        result = client.query_logs_by_time_segment(
            log_group_id=log_group_id,
            log_stream_id=log_stream_id,
            start_time_ms=from_ms,
            end_time_ms=now_ms,
            keywords=keywords,
        )
        if result is None:
            return "Failed to retrieve log histogram."
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()