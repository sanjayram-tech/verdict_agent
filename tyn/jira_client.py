"""Thin Jira Cloud REST client.

`JiraReader` only ever issues GET requests; the agent receives a reader and
therefore cannot modify Jira. `JiraWriter` (importer only) adds create calls.
Both retry 429/5xx with backoff (honouring Retry-After) and use timeouts.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterator

import requests

from .config import JiraConfig


class JiraError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Jira HTTP {status}: {message}")
        self.status = status
        self.message = message


RETRY_STATUSES = {429, 500, 502, 503, 504}
# Non-idempotent writes are only retried when Jira certainly did NOT process the request.
# A timeout or 502/504 on a POST may have been applied server-side, so retrying would duplicate data.
SAFE_WRITE_RETRY_STATUSES = {429, 503}


class _Base:
    def __init__(self, cfg: JiraConfig, timeout: float = 15.0, max_retries: int = 3,
                 on_retry: Callable[[str, int, float], None] | None = None):
        self.cfg = cfg
        self.timeout = timeout
        self.max_retries = max_retries
        self.on_retry = on_retry
        self._s = requests.Session()
        self._s.auth = (cfg.email, cfg.api_token)
        self._s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, timeout: float | None = None, **kw: Any) -> Any:
        url = f"{self.cfg.base_url}{path}"
        idempotent = method in ("GET", "HEAD", "PUT", "DELETE")
        retry_statuses = RETRY_STATUSES if idempotent else SAFE_WRITE_RETRY_STATUSES
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._s.request(method, url, timeout=timeout or self.timeout, **kw)
            except requests.RequestException as exc:  # network error / timeout
                if not idempotent:
                    raise JiraError(0, f"network error on {method} {path}: {type(exc).__name__}. The request "
                                       "may have been applied; re-run the command (it skips what exists) and "
                                       "use 'dedupe' if duplicates appear.") from None
                if attempt >= self.max_retries:
                    raise JiraError(0, f"network error: {type(exc).__name__}") from None
                self._sleep(path, attempt, delay)
                delay *= 2
                continue
            if resp.status_code in retry_statuses and attempt < self.max_retries:
                ra = resp.headers.get("Retry-After")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                self._sleep(path, attempt, min(wait, 60.0))
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise JiraError(resp.status_code, _error_text(resp))
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise JiraError(0, "retries exhausted")  # pragma: no cover

    def _sleep(self, path: str, attempt: int, wait: float) -> None:
        if self.on_retry:
            self.on_retry(path, attempt + 1, wait)
        time.sleep(wait)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
        msgs = list(body.get("errorMessages", [])) + [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
        if msgs:
            return "; ".join(msgs)[:500]
    except ValueError:
        pass
    return resp.text[:300]


class JiraReader(_Base):
    """Read-only operations. Used by the agent."""

    def search(self, jql: str, fields: list[str], max_results: int | None = None) -> Iterator[dict[str, Any]]:
        """Enhanced JQL search (GET /rest/api/3/search/jql) with token pagination."""
        token = None
        returned = 0
        while True:
            page = min(100, max_results - returned) if max_results else 100
            params: dict[str, Any] = {"jql": jql, "fields": ",".join(fields), "maxResults": page}
            if token:
                params["nextPageToken"] = token
            data = self._get("/rest/api/3/search/jql", params)
            for issue in data.get("issues", []):
                yield issue
                returned += 1
                if max_results and returned >= max_results:
                    return
            token = data.get("nextPageToken")
            if not token or data.get("isLast", False):
                return

    def get_issue(self, key: str, fields: list[str]) -> dict[str, Any]:
        return self._get(f"/rest/api/3/issue/{key}", {"fields": ",".join(fields)})

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self._get(f"/rest/api/3/issue/{key}/comment", {"startAt": start, "maxResults": 100})
            batch = data.get("comments", [])
            out.extend(batch)
            start += len(batch)
            if not batch or start >= data.get("total", 0):
                return out


class JiraWriter(JiraReader):
    """Adds the write operations the importer needs. Writes get a longer timeout."""

    WRITE_TIMEOUT = 90.0

    def create_issues_bulk(self, issue_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data = self._request("POST", "/rest/api/3/issue/bulk", timeout=self.WRITE_TIMEOUT,
                             json={"issueUpdates": [{"fields": f} for f in issue_fields]})
        if data.get("errors"):
            raise JiraError(400, f"bulk create errors: {data['errors'][:3]}")
        return data.get("issues", [])

    def add_comment(self, key: str, body_adf: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/rest/api/3/issue/{key}/comment", timeout=self.WRITE_TIMEOUT,
                             json={"body": body_adf})

    def delete_issue(self, key: str) -> None:
        """Permanent. Only used by the guarded `dedupe --apply` command."""
        self._request("DELETE", f"/rest/api/3/issue/{key}", timeout=self.WRITE_TIMEOUT,
                      params={"deleteSubtasks": "false"})

    def create_link(self, link_type: str, from_key: str, to_key: str) -> None:
        # "Relates" is symmetric in Jira; the dataset's direction (from -> to) is
        # preserved separately in each issue's description header ("Links to: ...").
        self._request("POST", "/rest/api/3/issueLink", timeout=self.WRITE_TIMEOUT, json={
            "type": {"name": link_type},
            "inwardIssue": {"key": from_key},
            "outwardIssue": {"key": to_key},
        })

    def myself(self) -> dict[str, Any]:
        return self._get("/rest/api/3/myself")

    def project(self, key: str) -> dict[str, Any]:
        return self._get(f"/rest/api/3/project/{key}")
