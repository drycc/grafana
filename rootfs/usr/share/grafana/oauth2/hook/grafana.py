import logging
import os
import time
import json
import httpx
from string import Template
from psycopg import AsyncConnection

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {"Content-Type": "application/json"}
DRYCC_CONTROLLER_URL = os.environ.get('DRYCC_CONTROLLER_URL')
DRYCC_GRAFANA_REFRESH = os.environ.get('DRYCC_GRAFANA_REFRESH', '60s')
DRYCC_GRAFANA_DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../")

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
    created, drycc_token = await _get_or_create_drycc_token(
        userinfo["preferred_username"], token)
    context["drycc_token"] = drycc_token

    workspace_orgs = await _build_workspace_orgs(userinfo, drycc_token)

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

    The email receiver addresses are fully rebuilt each time based on
    all workspace members with alerts=True, ensuring stale entries are removed.
    """
    workspace_orgs = context.get("workspace_orgs", {})
    drycc_token = context.get("drycc_token")

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        alerts = ws_info["alerts"]

        alert_addresses = await _build_alert_addresses(ws_name, drycc_token, userinfo, alerts)
        alertmanager_config = _build_alertmanager_config(alert_addresses)

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
    alerting_path = os.path.join(os.path.dirname(__file__), "..", "alerting")

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        ctx = {**context, "org_id": org_id}

        async with httpx.AsyncClient() as client:
            for filename in os.listdir(alerting_path):
                with open(os.path.join(alerting_path, filename)) as f:
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
    """Create datasources for each workspace org with workspace-specific URLs."""
    workspace_orgs = context.get("workspace_orgs", {})
    datasources_path = os.path.join(os.path.dirname(__file__), "..", "datasources")
    drycc_token = context.get("drycc_token")

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        ctx = {**context, "org_id": org_id}
        headers = _api_headers(ctx, userinfo)

        async with httpx.AsyncClient() as client:
            for filename in os.listdir(datasources_path):
                with open(os.path.join(datasources_path, filename)) as f:
                    template = Template(f.read())
                    datasource = json.loads(template.substitute(
                        controller_url=DRYCC_CONTROLLER_URL,
                        workspace=ws_name,
                        time_interval=DRYCC_GRAFANA_REFRESH,
                        token=drycc_token
                    ))
                    resp = await client.get(
                        _api_url(f"/api/datasources/name/{datasource['name']}"), headers=headers)
                    if resp.status_code == 200:
                        existing = resp.json()
                        datasource["id"] = existing["id"]
                        datasource["version"] = existing["version"]
                        await client.put(
                            _api_url(f"/api/datasources/uid/{datasource['uid']}"),
                            headers=headers, json=datasource)
                    elif resp.status_code == 404:
                        await client.post(
                            _api_url("/api/datasources"), headers=headers, json=datasource)
                    else:
                        raise ValueError(
                            f"grafana returned an unexpected status: {resp.status_code}"
                        )


async def sync_dashboards(context: dict, token: dict, userinfo: dict):
    """Create dashboards for each workspace org."""
    workspace_orgs = context.get("workspace_orgs", {})
    dashboards_path = os.path.join(os.path.dirname(__file__), "..", "dashboards")

    for ws_name, ws_info in workspace_orgs.items():
        org_id = ws_info["org_id"]
        ctx = {**context, "org_id": org_id}

        async with httpx.AsyncClient() as client:
            for filename in os.listdir(dashboards_path):
                with open(os.path.join(dashboards_path, filename)) as f:
                    dashboard = json.load(f)
                    dashboard.update({"id": None, "refresh": DRYCC_GRAFANA_REFRESH})
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
            os.environ.get('GF_SECURITY_ADMIN_USER'),
            os.environ.get('GF_SECURITY_ADMIN_PASSWORD'),
            os.environ.get('GF_SERVER_HTTP_PORT', 3000),
            url_path,
        )
    return "http://localhost:{}{}".format(os.environ.get('GF_SERVER_HTTP_PORT', 3000), url_path)


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


async def _get_workspaces(drycc_token: str) -> list:
    """Call Controller API to get user's workspaces."""
    headers = {"Authorization": f"Token {drycc_token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DRYCC_CONTROLLER_URL}/v2/workspaces", headers=headers)
        resp.raise_for_status()
        return resp.json().get("results", [])


async def _get_workspace_members(workspace_name: str, drycc_token: str) -> list:
    """Call Controller API to get workspace members."""
    headers = {"Authorization": f"Token {drycc_token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DRYCC_CONTROLLER_URL}/v2/workspaces/{workspace_name}/members",
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


async def _build_workspace_orgs(userinfo: dict, drycc_token: str) -> dict:
    """Build workspace org info by fetching workspaces and their memberships.

    Returns: {workspace_name: {"org_id": int, "role": str, "alerts": bool, "email": str}}
    """
    workspace_orgs = {}
    try:
        workspaces = await _get_workspaces(drycc_token)
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch workspaces for %s: %s", userinfo["preferred_username"], e)
        return workspace_orgs

    for ws in workspaces:
        workspace_name = ws["name"]
        workspace_email = ws.get("email", userinfo["email"])
        try:
            members = await _get_workspace_members(workspace_name, drycc_token)
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch members for workspace %s: %s", workspace_name, e)
            continue
        user_member = next(
            (m for m in members if m["user"] == userinfo["preferred_username"]), None)
        if user_member is None:
            continue

        org_id = await _get_or_create_org(workspace_name)
        workspace_orgs[workspace_name] = {
            "org_id": org_id,
            "role": user_member["role"],
            "alerts": user_member.get("alerts", True),
            "email": workspace_email,
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


async def _build_alert_addresses(
    ws_name: str, drycc_token: str, userinfo: dict, alerts: bool
) -> str:
    """Build comma-separated alert email addresses for a workspace."""
    if drycc_token:
        try:
            members = await _get_workspace_members(ws_name, drycc_token)
            email_list = [m["email"] for m in members if m.get("alerts", True)]
            return ",".join(email_list)
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch members for alert addresses in %s: %s", ws_name, e)
            return userinfo["email"] if alerts else ""
    return userinfo["email"] if alerts else ""


def _build_alertmanager_config(alert_addresses: str) -> str:
    """Build alertmanager JSON config string."""
    if alert_addresses:
        receivers = [{
            "name": "grafana-default-email",
            "grafana_managed_receiver_configs": [{
                "uid": "",
                "name": "email receiver",
                "type": "email",
                "settings": {"addresses": alert_addresses}
            }]
        }]
    else:
        receivers = [{
            "name": "grafana-default-email",
            "grafana_managed_receiver_configs": []
        }]
    return json.dumps({
        "alertmanager_config": {
            "route": {
                "receiver": "grafana-default-email",
                "group_by": ["grafana_folder", "alertname"]
            },
            "receivers": receivers
        }
    })


async def _upsert_alert_configuration(org_id: int, config: str):
    """Insert or update alert configuration for an org using parameterized query."""
    async with await AsyncConnection.connect(os.environ.get("GF_DATABASE_URL")) as conn:
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


async def _get_or_create_drycc_token(username, token: dict):
    async def _check_or_create_drycc_token(drycc_token, token):
        async with httpx.AsyncClient() as client:
            created = False if drycc_token else True
            if drycc_token:
                headers = {"Authorization": f"Token {drycc_token}"}
                resp = await client.get(
                    f"{DRYCC_CONTROLLER_URL}/v2/auth/whoami", headers=headers)
                if resp.status_code in [401, 403]:
                    created = True
            if created:
                headers = {"Authorization": f"Bearer {token['access_token']}"}
                data = (await client.post(
                    f"{DRYCC_CONTROLLER_URL}/v2/auth/token/?alias=grafana-datasource",
                    headers=headers, json=token)).json()
                drycc_token = data["token"]
            return created, drycc_token

    async with await AsyncConnection.connect(os.environ.get("GF_DATABASE_URL")) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT o_auth_id_token FROM user_auth WHERE auth_module=%s AND auth_id=%s",
                ("authproxy", username)
            )
            row = await cursor.fetchone()
            drycc_token = row[0] if row else None
        created, drycc_token = await _check_or_create_drycc_token(drycc_token, token)
        if created:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE user_auth SET o_auth_id_token=%s WHERE auth_module=%s AND auth_id=%s",
                    (drycc_token, "authproxy", username)
                )
                await conn.commit()
        return created, drycc_token
