"""Dev-request channel for the autonomous trading agents.

When an LLM trading agent needs something from a human developer (Claude) -- a
new data source, a bug fix, a feature -- it files a "dev request". Each request
is opened as a GitHub issue via the REST API and is also recorded locally in the
LiveDB so the agents can review what they've already asked for.

The module uses ONLY the Python standard library (urllib.request, json, os) so
the agent service does not need the `requests` package. Network failures, a
missing GITHUB_TOKEN, or a non-2xx response all degrade gracefully to a
DB-only fallback; ``file_dev_request`` never raises.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GITHUB_API_URL = "https://api.github.com/repos/{repo}/issues"
DEFAULT_REPO = "emdoc12/algotrader"
DEFAULT_LABELS = ["dev-request", "from-trading-agent"]
GITHUB_API_VERSION = "2022-11-28"
MAX_RETRIES = 3


def _get_db(db=None):
    """Return a LiveDB-like object, importing lazily and defensively.

    The ``daytrader.live.db`` module may be written by a parallel process and
    might not exist yet (or might lack the expected methods). We never let an
    import or construction error propagate.
    """
    if db is not None:
        return db
    try:
        from daytrader.live.db import LiveDB  # lazy import; may not exist yet
    except Exception:
        return None
    try:
        return LiveDB()
    except Exception:
        return None


def _record_in_db(db, title, body, url=None, status="open"):
    """Best-effort write to the DB. Returns True on success, False otherwise."""
    if db is None:
        return False
    add = getattr(db, "add_dev_request", None)
    if not callable(add):
        return False
    try:
        add(title, body, url=url, status=status)
        return True
    except TypeError:
        # Older/different signature -- try positional only.
        try:
            add(title, body, url)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _post_issue(repo, token, payload):
    """POST a new GitHub issue. Returns the parsed JSON response dict.

    Raises urllib.error.URLError / HTTPError on failure so the caller can
    distinguish network errors (retry) from 4xx (do not retry).
    """
    url = GITHUB_API_URL.format(repo=repo)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "algotrader-dev-requests")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def file_dev_request(
    title: str,
    body: str,
    labels: list[str] | None = None,
    db=None,
) -> dict:
    """Create a GitHub issue describing what the trading team needs from the dev.

    Reads GITHUB_TOKEN and GITHUB_REPO (format "owner/repo", default
    "emdoc12/algotrader") from the environment. If a token is present, POSTs a
    new issue using the GitHub REST API (stdlib only). The request is ALWAYS
    also recorded via ``db.add_dev_request(...)`` when a db is available
    (storing the returned issue html_url). On any failure (no token, network
    error, non-2xx) it falls back to recording in the DB only and returns
    ok=False with the error.

    Returns a dict:
        {ok: bool, url: str|None, number: int|None, fallback: bool, error: str|None}
    """
    if labels is None:
        labels = list(DEFAULT_LABELS)

    repo = os.environ.get("GITHUB_REPO") or DEFAULT_REPO
    token = os.environ.get("GITHUB_TOKEN")

    db = _get_db(db)

    result = {"ok": False, "url": None, "number": None, "fallback": True,
              "recorded": False, "error": None}

    if not token:
        # No GitHub mirror, but the request still persists locally and shows on
        # the dashboard's dev-requests page. ``recorded`` is the truthful signal.
        result["recorded"] = _record_in_db(db, title, body, url=None, status="open")
        result["error"] = "no GITHUB_TOKEN set (recorded locally only)"
        return result

    payload = {"title": title, "body": body, "labels": labels}

    last_error = None
    backoff = 1
    for attempt in range(MAX_RETRIES):
        try:
            issue = _post_issue(repo, token, payload)
            html_url = issue.get("html_url")
            number = issue.get("number")
            recorded = _record_in_db(db, title, body, url=html_url, status="open")
            return {
                "ok": True,
                "url": html_url,
                "number": number,
                "fallback": False,
                "recorded": recorded,
                "error": None,
            }
        except urllib.error.HTTPError as exc:
            # 4xx/5xx from GitHub. Do NOT retry client errors; they won't fix
            # themselves. Retry only transient 5xx server errors.
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            last_error = f"HTTP {exc.code}: {exc.reason} {detail}".strip()
            if 500 <= exc.code < 600 and attempt < MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Network error -- retry with exponential backoff (1s, 2s, 4s).
            last_error = f"network error: {exc}"
            if attempt < MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            break
        except Exception as exc:  # noqa: BLE001 -- never raise out of this fn
            last_error = f"unexpected error: {exc}"
            break

    # All attempts failed: fall back to DB-only.
    result["error"] = last_error or "unknown error"
    result["recorded"] = _record_in_db(db, title, body, url=None, status="open")
    return result


def list_open_requests(db=None) -> list[dict]:
    """Return open dev requests from the DB.

    Lets agents see what they've already asked for and avoid filing duplicates.
    Defensive: returns an empty list if the DB is unavailable or raises.
    """
    db = _get_db(db)
    if db is None:
        return []
    getter = getattr(db, "open_dev_requests", None)
    if not callable(getter):
        return []
    try:
        rows = getter()
    except Exception:
        return []
    if rows is None:
        return []
    try:
        return list(rows)
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# resolution sync: GitHub -> desks                                            #
# --------------------------------------------------------------------------- #
_SYNC_EVERY_SEC = float(os.environ.get("DEV_REQUEST_SYNC_MINUTES", "15")) * 60.0
_last_sync = 0.0


def sync_github_resolutions() -> list[dict]:
    """Close local dev requests whose GitHub issue was closed, and broadcast.

    This is the return leg of the automated pipeline: a desk files an issue,
    the auto-fix workflow repairs it and closes the issue — and without this,
    nothing running here would ever notice. The request would sit open on the
    dashboard forever and no desk would be told to retry. Polling the handful
    of open, mirrored requests every ~15 minutes closes that gap for a few
    API calls a day.

    Reuses ``close_dev_request`` from the dashboard so a GitHub-driven close
    broadcasts to every desk's journal exactly like the owner's own
    "Fixed — tell them" button.
    """
    global _last_sync
    now = time.time()
    if now - _last_sync < _SYNC_EVERY_SEC:
        return []
    _last_sync = now
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return []

    # Lazy import: dashboard imports competition which reaches back here.
    from daytrader.live.dashboard import _team_db, close_dev_request
    from daytrader.live.competition import team_names

    actions: list[dict] = []
    actions.extend(_backfill_github_issues(token, team_names, _team_db))
    for team in team_names():
        db = _team_db(team)
        if db is None:
            continue
        try:
            rows = [r for r in db.open_dev_requests() if "/issues/" in str(r.get("url") or "")]
        finally:
            db.close()
        for r in rows:
            info = _fetch_issue_state(str(r["url"]), token)
            if not info or info.get("state") != "closed":
                continue
            status = "wont_fix" if info.get("state_reason") == "not_planned" else "closed"
            resolution = (info.get("resolution")
                          or "Resolved on GitHub — the fix has shipped. Try it again.")
            res = close_dev_request(team, r["id"], status, resolution[:1200])
            actions.append({"team": team, "id": r["id"], "status": status,
                            "notified": len(res.get("notified_desks") or [])})
    return actions


def _fetch_issue_state(html_url: str, token: str) -> dict | None:
    """State + last comment for a mirrored issue. None on any failure."""
    try:
        # html_url: https://github.com/{owner}/{repo}/issues/{n}
        parts = html_url.rstrip("/").split("/")
        n = int(parts[-1])
        repo = "/".join(parts[-4:-2])
        api = f"https://api.github.com/repos/{repo}/issues/{n}"
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/vnd.github+json",
                   "User-Agent": "algotrader-devsync"}
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            issue = json.loads(resp.read().decode("utf-8", "replace"))
        out = {"state": issue.get("state"), "state_reason": issue.get("state_reason"),
               "resolution": None}
        if out["state"] == "closed" and int(issue.get("comments") or 0) > 0:
            req = urllib.request.Request(api + "/comments?per_page=100", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                comments = json.loads(resp.read().decode("utf-8", "replace"))
            if comments:
                out["resolution"] = str(comments[-1].get("body") or "")
        return out
    except Exception:  # noqa: BLE001 - the next sync pass retries
        return None


def _report_bridge_failure(db, exc) -> None:
    """Put a desk→GitHub filing failure where the owner will actually see it:
    the desk's agent log and the dashboard's degraded-providers panel."""
    detail = repr(exc)[:300]
    try:
        import urllib.error as _ue
        if isinstance(exc, _ue.HTTPError):
            try:
                detail = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:250]}"
            except Exception:  # noqa: BLE001
                detail = f"HTTP {exc.code}: {exc.reason}"
    except Exception:  # noqa: BLE001
        pass
    try:
        db.log_agent("runner", "github_issue_post_failed", detail)
    except Exception:  # noqa: BLE001
        pass
    try:
        from daytrader.data.feeds.base import record_named_error
        record_named_error(
            "github_issues", "post_failed", detail,
            hint=("Dev requests are NOT reaching GitHub, so the auto-fix pipeline "
                  "never sees them. An HTTP 403 here with a green github health row "
                  "means the token lacks the ISSUES permission: fine-grained tokens "
                  "need 'Issues: Read and write'; classic tokens need the full "
                  "'repo' scope."))
    except Exception:  # noqa: BLE001
        pass


# At most this many backfilled issues per sync pass. The stranded backlog can be
# large, and each filed issue triggers an auto-fix workflow run — trickling them
# out keeps the fix queue (and its spend) sane; the next pass takes the rest.
_BACKFILL_PER_PASS = int(os.environ.get("DEV_REQUEST_BACKFILL_PER_PASS", "5"))


def _backfill_github_issues(token: str, team_names, _team_db) -> list[dict]:
    """Mirror stranded local-only dev requests to GitHub.

    Requests filed while the GITHUB_TOKEN was broken exist only in the desks'
    local databases — invisible to the auto-fix pipeline, which is exactly the
    backlog the owner keeps having to relay by hand. Now that the token works,
    each open request with no issue URL gets filed and linked, after which the
    normal pipeline (fix → close → broadcast) takes over.
    """
    repo = os.environ.get("GITHUB_REPO") or DEFAULT_REPO
    out: list[dict] = []
    filed = 0
    for team in team_names():
        if filed >= _BACKFILL_PER_PASS:
            break
        db = _team_db(team)
        if db is None:
            continue
        try:
            rows = [r for r in db.open_dev_requests() if not str(r.get("url") or "").strip()]
            for r in rows:
                if filed >= _BACKFILL_PER_PASS:
                    break
                body = (str(r.get("body") or "") +
                        f"\n\n---\n*Backfilled from the {team} desk's dashboard: originally "
                        f"filed {r.get('ts')}, reported {int(r.get('report_count') or 1)}x. "
                        "The GitHub mirror was down when this was first filed.*")
                try:
                    issue = _post_issue(repo, token,
                                        {"title": r.get("title") or "(untitled)",
                                         "body": body, "labels": list(DEFAULT_LABELS)})
                except Exception as exc:  # noqa: BLE001
                    # Next pass retries — but the failure must be VISIBLE. Six
                    # requests sat on the dashboard for hours while this except
                    # silently discarded the reason nothing reached GitHub.
                    _report_bridge_failure(db, exc)
                    return out
                url = issue.get("html_url")
                if url:
                    db.update_dev_request(int(r["id"]), url=url)
                    db.log_agent("runner", "dev_request_backfilled",
                                 f"#{r['id']} -> {url}")
                    out.append({"team": team, "id": r["id"], "backfilled": url})
                    filed += 1
        finally:
            db.close()
    return out
