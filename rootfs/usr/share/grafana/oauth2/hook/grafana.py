import logging
import time
import json
import httpx
from pathlib import Path
from string import Template
from psycopg import AsyncConnection
from settings import settings

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {"Content-Type": "application/json"}
DRYCC_GRAFANA_DASHBOARD = Path(__file__).resolve().parent.parent

# Drycc Workspace role to Grafana role mapping
DRYCC_WORKSPACE_ROLE_MAPPING = {"admin": "Editor", "member": "Editor", "viewer": "Viewer"}
# Grafana default main organization ID, renamed to "drycc" by init_org on startup
DRYCC_ORG_ID = 1


# ── Public functions (exported via __init__.py) ──────────────────────────


async def init_org(org_id=DRYCC_ORG_ID, name="drycc"):
    headers = {"Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(_api_url(f"/api/orgs/{org_id}", is_admin=True), headers=headers)
        org = resp.json()
        if org["name"] == name:
            return
        await client.put(
            _api_url(f"/api/orgs/{org_id}", is_admin=True),
            headers=headers,
            json={"name": name},
        )


async def has_changed(token: dict, userinfo: dict) -> bool:
    """Detect drift between the cached userinfo / Grafana org state and the
    live state in passport and the controller.

    Returns True when:
      * any tracked userinfo field differs from passport's response, or
      * the user's controller workspace membership count differs from the
        number of workspace orgs they currently belong to in Grafana, or
      * any workspace's mapped Grafana role differs from the role the user
        currently has on the matching Grafana org.
    Returns False when everything matches.
    """
    access_token = token.get("access_token")
    passport_headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        passport_resp = await client.get(
            settings.passport_userinfo_url, headers=passport_headers)
        passport_resp.raise_for_status()
        passport_userinfo = passport_resp.json()

        for field in ("preferred_username", "email", "is_superuser", "is_staff"):
            if userinfo.get(field) != passport_userinfo.get(field):
                return True

        workspaces = await _get_workspaces(access_token)
        live_roles: dict[str, str] = {}
        for ws in workspaces:
            ws_name = ws["name"]
            members = await _get_workspace_members(ws_name, access_token)
            user_member = next(
                (m for m in members if m["user"] == userinfo["preferred_username"]), None)
            if user_member is None:
                continue
            live_roles[ws_name] = DRYCC_WORKSPACE_ROLE_MAPPING.get(
                user_member["role"], "Viewer")

        grafana_resp = await client.get(
            _api_url("/api/user/orgs"), headers=_api_headers({}, userinfo))
        grafana_resp.raise_for_status()
        grafana_roles = {
            org["name"]: org["role"]
            for org in grafana_resp.json()
            if org["name"] != "drycc"
        }

    return live_roles != grafana_roles


async def sync_user(context: dict, token: dict, userinfo: dict):
    async with httpx.AsyncClient() as client:
        resp = await client.get(_api_url("/api/user"), headers=_api_headers(context, userinfo))
        user = resp.json()
        context["user_id"] = user["id"]
        if userinfo["is_superuser"]:
            await client.put(
                _api_url(f"/api/admin/users/{context['user_id']}/permissions", is_admin=True),
                headers=_api_headers(context, userinfo),
                json={"isGrafanaAdmin": userinfo["is_superuser"]},
            )


async def sync_role(context: dict, token: dict, userinfo: dict):
    """Sync user's Grafana Org memberships based on their workspace memberships."""
    access_token = token.get("access_token")
    context["access_token"] = access_token

    workspace_orgs = await _build_workspace_orgs(userinfo, access_token)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _api_url("/api/user/orgs"), headers=_api_headers(context, userinfo))
        current_orgs = resp.json()
        workspace_org_ids = {info["org_id"] for info in workspace_orgs.values()}
        current_org_map = {org["orgId"]: org["name"] for org in current_orgs}

        await _sync_workspace_org_memberships(
            client, context, userinfo, workspace_orgs, current_org_map)
        await _cleanup_stale_orgs(
            client, context, userinfo, current_orgs, workspace_org_ids)
        await _sync_drycc_org(client, context, userinfo, current_org_map)

    context["workspace_orgs"] = workspace_orgs


async def sync_default(context: dict, token: dict, userinfo: dict):
    """Create default folder and alert configuration for each workspace org.

    Each workspace org gets a webhook contact point pointing back to this
    service's /alerts/webhook endpoint, which fans out to passport messages
    for workspace members with alerts=True.
    """
    workspace_orgs = context.get("workspace_orgs", {})

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]

        alertmanager_config = _build_alertmanager_config(ws_name)

        ctx = {**context, "org_id": org_id}
        async with httpx.AsyncClient() as client:
            await client.post(
                _api_url("/api/folders"),
                headers=_api_headers(ctx, userinfo),
                json={"uid": "drycc", "title": "drycc"},
            )

        await _upsert_alert_configuration(org_id, alertmanager_config)


async def sync_alerting(context: dict, token: dict, userinfo: dict):
    """Create or update alert rules for each workspace org (idempotent).

    Alert rules are always created regardless of the alerts field.
    The alerts field only controls notification channels (handled in sync_default).
    """
    workspace_orgs = context.get("workspace_orgs", {})
    alerting_path = Path(__file__).resolve().parent.parent / "alerting"

    for _, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        ctx = {**context, "org_id": org_id}

        async with httpx.AsyncClient() as client:
            for filepath in alerting_path.glob("*.json"):
                with filepath.open() as f:
                    rule = json.load(f)
                # Use PUT for idempotent upsert (POST would create duplicates)
                resp = await client.put(
                    _api_url(f"/api/v1/provisioning/alert-rules/{rule['uid']}"),
                    headers=_api_headers(ctx, userinfo),
                    json=rule,
                )
                if resp.status_code == 404:
                    # Rule doesn't exist yet, create it
                    await client.post(
                        _api_url("/api/v1/provisioning/alert-rules"),
                        headers=_api_headers(ctx, userinfo),
                        json=rule,
                    )


async def sync_datasources(context: dict, token: dict, userinfo: dict):
    """Create datasources for each workspace org with workspace-specific URLs.

    Datasource creation requires Org Admin permission. Workspace users are
    only Editor/Viewer, so we must use Grafana admin basic auth combined with
    X-Grafana-Org-Id to write into the target workspace org.
    """
    workspace_orgs = context.get("workspace_orgs", {})
    datasources_path = Path(__file__).resolve().parent.parent / "datasources"

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        headers = {
            "Content-Type": "application/json",
            "X-Grafana-Org-Id": str(org_id),
        }

        async with httpx.AsyncClient() as client:
            for filepath in datasources_path.glob("*.json"):
                with filepath.open() as f:
                    template = Template(f.read())
                    datasource = json.loads(template.substitute(
                        controller_url=settings.controller_base_url,
                        time_interval=settings.drycc_grafana_refresh,
                        workspace=ws_name,
                    ))
                    resp = await client.get(
                        _api_url(f"/api/datasources/name/{datasource['name']}", is_admin=True),
                        headers=headers)
                    if resp.status_code == 200:
                        existing = resp.json()
                        datasource["id"] = existing["id"]
                        datasource["version"] = existing["version"]
                        resp = await client.put(
                            _api_url(f"/api/datasources/uid/{datasource['uid']}", is_admin=True),
                            headers=headers, json=datasource)
                        resp.raise_for_status()
                    elif resp.status_code == 404:
                        resp = await client.post(
                            _api_url("/api/datasources", is_admin=True),
                            headers=headers, json=datasource)
                        resp.raise_for_status()
                    else:
                        raise ValueError(
                            f"grafana returned an unexpected status: {resp.status_code}"
                        )


async def sync_dashboards(context: dict, token: dict, userinfo: dict):
    """Create dashboards for each workspace org."""
    workspace_orgs = context.get("workspace_orgs", {})
    dashboards_path = Path(__file__).resolve().parent.parent / "dashboards"

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        ctx = {**context, "org_id": org_id}

        async with httpx.AsyncClient() as client:
            for filepath in dashboards_path.glob("*.json"):
                with filepath.open() as f:
                    dashboard = json.load(f)
                    dashboard.update({"id": None, "refresh": settings.drycc_grafana_refresh})
                    await client.post(
                        _api_url("/api/dashboards/db"),
                        headers=_api_headers(ctx, userinfo),
                        json={
                            "dashboard": dashboard,
                            "folderUid": "drycc",
                            "overwrite": True,
                        },
                    )


# ── Private functions ────────────────────────────────────────────────────


def _api_url(url_path, is_admin=False):
    if is_admin:
        return "http://{}:{}@localhost:{}{}".format(
            settings.gf_security_admin_user,
            settings.gf_security_admin_password,
            settings.gf_server_http_port,
            url_path
        )
    return "http://localhost:{}{}".format(settings.gf_server_http_port, url_path)


def _api_headers(context: dict, userinfo):
    headers = {"Content-Type": "application/json"}
    headers["Remote-User"] = userinfo["preferred_username"]
    headers["Remote-Name"] = userinfo["preferred_username"]
    headers["Remote-Email"] = userinfo["email"]
    if "org_id" in context:
        headers["X-Grafana-Org-Id"] = str(context["org_id"])
    return headers


def _get_drycc_role(userinfo: dict) -> str | None:
    if userinfo["is_superuser"]:
        return "Editor"
    elif userinfo["is_staff"]:
        return "Viewer"
    return None


async def _get_workspaces(access_token: str) -> list:
    """Call Controller API to get user's workspaces."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.controller_base_url}/v2/workspaces", headers=headers)
        resp.raise_for_status()
        return resp.json().get("results", [])


async def _get_workspace_members(workspace_id: str, access_token: str) -> list:
    """Call Controller API to get workspace members."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{settings.controller_base_url}/v2/workspaces/{workspace_id}/members",
            headers=headers)
        resp.raise_for_status()
        return resp.json().get("results", [])


async def _get_or_create_org(name: str) -> int:
    """Find or create a Grafana Org by name. Returns org_id."""
    headers = {"Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        # Try to find existing org by name
        resp = await client.get(
            _api_url(f"/api/orgs/name/{name}", is_admin=True), headers=headers)
        if resp.status_code == 200:
            return resp.json()["id"]
        # Create new org (handle race condition: another request may have created it)
        resp = await client.post(
            _api_url("/api/orgs", is_admin=True), headers=headers,
            json={"name": name})
        if resp.status_code == 409:
            # Org already exists (race condition), fetch it
            resp = await client.get(
                _api_url(f"/api/orgs/name/{name}", is_admin=True), headers=headers)
            return resp.json()["id"]
        resp.raise_for_status()
        return resp.json()["orgId"]


async def _build_workspace_orgs(userinfo: dict, access_token: str) -> dict:
    """Build workspace org info by fetching workspaces and their memberships.

    Returns: {workspace_id: {"org_id": int, "role": str}}
    """
    workspace_orgs = {}
    try:
        workspaces = await _get_workspaces(access_token)
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch workspaces for %s: %s", userinfo["preferred_username"], e)
        return workspace_orgs

    for ws in workspaces:
        workspace_id = ws["id"]
        try:
            members = await _get_workspace_members(workspace_id, access_token)
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch members for workspace %s: %s", workspace_id, e)
            continue
        user_member = next(
            (m for m in members if m["user"] == userinfo["preferred_username"]), None)
        if user_member is None:
            continue

        org_id = await _get_or_create_org(workspace_id)
        workspace_orgs[workspace_id] = {
            "org_id": org_id,
            "role": user_member["role"],
        }
    return workspace_orgs


async def _sync_workspace_org_memberships(
    client: httpx.AsyncClient, context: dict, userinfo: dict,
    workspace_orgs: dict, current_org_map: dict,
):
    """Add/update user's membership in each workspace org."""
    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        grafana_role = DRYCC_WORKSPACE_ROLE_MAPPING.get(ws_info["role"], "Viewer")

        if org_id in current_org_map:
            await client.patch(
                _api_url(f"/api/orgs/{org_id}/users/{context['user_id']}", is_admin=True),
                headers=_api_headers(context, userinfo),
                json={"role": grafana_role},
            )
        else:
            await client.post(
                _api_url(f"/api/orgs/{org_id}/users", is_admin=True),
                headers=_api_headers(context, userinfo),
                json={
                    "loginOrEmail": userinfo["preferred_username"],
                    "role": grafana_role,
                },
            )


async def _cleanup_stale_orgs(
    client: httpx.AsyncClient, context: dict, userinfo: dict,
    current_orgs: list, workspace_org_ids: set,
):
    """Remove user from orgs they no longer belong to (excluding drycc org)."""
    for org in current_orgs:
        org_id = org["orgId"]
        if org_id == DRYCC_ORG_ID:
            continue
        if org_id not in workspace_org_ids:
            await client.delete(
                _api_url(f"/api/orgs/{org_id}/users/{context['user_id']}", is_admin=True),
                headers=_api_headers(context, userinfo),
            )


async def _sync_drycc_org(
    client: httpx.AsyncClient, context: dict, userinfo: dict,
    current_org_map: dict,
):
    """Handle drycc org membership for superusers/staff."""
    drycc_role = _get_drycc_role(userinfo)
    has_drycc_org = DRYCC_ORG_ID in current_org_map
    if drycc_role:
        if has_drycc_org:
            await client.patch(
                _api_url(f"/api/orgs/{DRYCC_ORG_ID}/users/{context['user_id']}", is_admin=True),
                headers=_api_headers(context, userinfo),
                json={"role": drycc_role},
            )
        else:
            await client.post(
                _api_url(f"/api/orgs/{DRYCC_ORG_ID}/users", is_admin=True),
                headers=_api_headers(context, userinfo),
                json={"loginOrEmail": userinfo["preferred_username"], "role": drycc_role},
            )
    elif has_drycc_org:
        await client.delete(
            _api_url(f"/api/orgs/{DRYCC_ORG_ID}/users/{context['user_id']}", is_admin=True),
            headers=_api_headers(context, userinfo),
        )


def _build_alertmanager_config(ws_name: str) -> str:
    receivers = [{
        "name": "controller-alerts",
        "grafana_managed_receiver_configs": [{
            "uid": "",
            "name": "controller alerts webhook",
            "type": "webhook",
            "settings": {
                "url": f"http://localhost:4000/alerts/webhook?workspace={ws_name}",
                "httpMethod": "POST",
            },
        }],
    }]
    return json.dumps({
        "alertmanager_config": {
            "route": {
                "receiver": "controller-alerts",
                "group_by": ["grafana_folder", "alertname"]
            },
            "receivers": receivers
        }
    })


async def _upsert_alert_configuration(org_id: int, config: str):
    """Insert or update alert configuration for an org using parameterized query."""
    async with await AsyncConnection.connect(settings.gf_database_url) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO alert_configuration (
                    alertmanager_configuration, configuration_version, created_at, "default", org_id
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (org_id) DO UPDATE
                SET alertmanager_configuration = EXCLUDED.alertmanager_configuration,
                    configuration_version = EXCLUDED.configuration_version,
                    created_at = EXCLUDED.created_at
                """,
                (config, "v1", int(time.time()), True, org_id),
            )
            await conn.commit()
