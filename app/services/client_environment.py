"""Display-only browser descriptions, never authentication or trust signals."""
import json
import re
from datetime import timezone

from sqlalchemy import func, select

from app.models import now_utc


BROWSERS = {"Chrome", "Safari", "Firefox", "Edge", "Samsung Internet", "Opera", "Chromium", "Unknown"}
OPERATING_SYSTEMS = {"iOS", "iPadOS", "Android", "macOS", "Windows", "Linux", "ChromeOS", "Unknown"}
DEVICES = {"desktop", "mobile", "tablet", "unknown"}
SESSION_DESCRIPTION_LIMIT = 20


def environment(browser="Unknown", os="Unknown", device="unknown"):
    browser = browser if isinstance(browser, str) and browser in BROWSERS else "Unknown"
    os = os if isinstance(os, str) and os in OPERATING_SYSTEMS else "Unknown"
    device = device if isinstance(device, str) and device in DEVICES else "unknown"
    labels = [value for value in (browser, os) if value != "Unknown"]
    if device in {"mobile", "tablet"}:
        labels.append({"mobile": "모바일", "tablet": "태블릿"}[device])
    return {"browser": browser, "os": os, "device": device,
        "label": " · ".join(labels) or "환경 정보 없음"}


def classify_user_agent(value):
    # Only fixed labels leave this parser. Raw UA text, claimed hardware and
    # version strings are never included in a display label.
    ua = value[:2048].lower() if isinstance(value, str) else ""
    browser = "Unknown"
    for marker, name in [
        (r"(?:edgios|edga|edg|edge)/", "Edge"),
        (r"samsungbrowser/", "Samsung Internet"),
        (r"(?:opr|opios)/", "Opera"),
        (r"(?:fxios|firefox)/", "Firefox"),
        (r"(?:crios|chrome)/", "Chrome"),
        (r"chromium/", "Chromium"),
        (r"version/[\d.]+.*safari/", "Safari"),
    ]:
        if re.search(marker, ua):
            browser = name
            break
    if "ipad" in ua:
        os, device = "iPadOS", "tablet"
    elif "iphone" in ua or "ipod" in ua:
        os, device = "iOS", "mobile"
    elif "android" in ua:
        os, device = "Android", "mobile" if "mobile" in ua else "tablet"
    elif "windows" in ua:
        os, device = "Windows", "mobile" if "phone" in ua else "desktop"
    elif "cros" in ua:
        os, device = "ChromeOS", "desktop"
    elif "macintosh" in ua or "mac os x" in ua:
        # Some tablet desktop modes advertise a Mac UA. Do not infer a
        # physical desktop or a particular tablet from conflicting markers.
        os, device = "macOS", "unknown" if "mobile" in ua else "desktop"
    elif "linux" in ua or "x11" in ua:
        os, device = "Linux", "desktop"
    else:
        os, device = "Unknown", "mobile" if "mobile" in ua else "unknown"
    return environment(browser, os, device)


def request_environment(request):
    return classify_user_agent(request.headers.get("user-agent"))


def decode_environment(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        value = None
    if not isinstance(value, dict):
        return environment()
    return environment(value.get("browser"), value.get("os"), value.get("device"))


def encode_environment(value):
    return json.dumps(decode_environment(value), ensure_ascii=False) if value is not None else None


def _iso(value):
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def describe_session(row, scope):
    result = {"scope": scope, "issued_at": _iso(row.issued_at), "last_seen_at": _iso(row.last_seen_at),
        "expires_at": _iso(row.expires_at if scope == "participant" else row.refresh_expires_at),
        "environment": decode_environment(row.client_environment)}
    if scope == "participant":
        result["contest_id"] = row.contest_id
    return result


def latest_descriptions(rows):
    return sorted(rows, key=lambda item: item["last_seen_at"] or item["issued_at"], reverse=True)[:SESSION_DESCRIPTION_LIMIT]


def active_user_sessions(db, email):
    """Service-master user detail only; never use for contest-scoped views."""
    from app.orm_models import GeneralSessionRow, StaffAccountRow, StaffSessionRow, TeamMemberRow, TeamSessionRow
    rows = []
    for model, scope, email_column, join in [
        (GeneralSessionRow, "general", GeneralSessionRow.email, None),
        (StaffSessionRow, "staff", StaffAccountRow.email, StaffAccountRow),
        (TeamSessionRow, "participant", TeamMemberRow.email, TeamMemberRow),
    ]:
        query = select(model)
        if join is not None:
            query = query.join(join)
        expiry = model.expires_at if scope == "participant" else model.refresh_expires_at
        query = query.where(func.lower(email_column) == email.lower(), model.revoked_at.is_(None), expiry > now_utc())
        for row in db.scalars(query.order_by(func.coalesce(model.last_seen_at, model.issued_at).desc()).limit(SESSION_DESCRIPTION_LIMIT)):
            rows.append(describe_session(row, scope))
    return latest_descriptions(rows)


def operator_participant_sessions(db, contest_id):
    """Only team-session rows for this contest, bounded per participant."""
    from app.orm_models import TeamSessionRow
    ranked = select(TeamSessionRow.team_session_id, func.row_number().over(
        partition_by=TeamSessionRow.team_member_id,
        order_by=func.coalesce(TeamSessionRow.last_seen_at, TeamSessionRow.issued_at).desc()).label("position"))
    ranked = ranked.where(TeamSessionRow.contest_id == contest_id,
        TeamSessionRow.revoked_at.is_(None), TeamSessionRow.expires_at > now_utc()).subquery()
    rows = db.scalars(select(TeamSessionRow).join(ranked, ranked.c.team_session_id == TeamSessionRow.team_session_id)
        .where(ranked.c.position <= SESSION_DESCRIPTION_LIMIT)
        .order_by(func.coalesce(TeamSessionRow.last_seen_at, TeamSessionRow.issued_at).desc()))
    result = {}
    for row in rows:
        result.setdefault(row.team_member_id, []).append(describe_session(row, "participant"))
    return result
