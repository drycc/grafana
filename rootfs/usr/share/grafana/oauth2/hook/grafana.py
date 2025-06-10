import os
import json
import httpx

DEFAULT_HEADERS = {"Content-Type": "application/json"}
DRYCC_GRAFANA_REFRESH = os.environ.get('DRYCC_GRAFANA_REFRESH', '60s')
DRYCC_CONTROLLER_API_URL = "http://{}:{}".format(
    os.environ.get('DRYCC_CONTROLLER_API_SERVICE_HOST'),
    os.environ.get('DRYCC_CONTROLLER_API_SERVICE_PORT'),
)
DRYCC_GRAFANA_DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../")


def api_url(url_path, is_admin=False):
    if is_admin:
        return "http://{}:{}@localhost:{}{}".format(
            os.environ.get('GF_SECURITY_ADMIN_USER'),
            os.environ.get('GF_SECURITY_ADMIN_PASSWORD'),
            os.environ.get('GF_SERVER_HTTP_PORT', 3000),
            url_path,
        )
    return "http://localhost:{}{}".format(os.environ.get('GF_SERVER_HTTP_PORT', 3000), url_path)


def api_headers(context: dict, userinfo):
    headers = {"Content-Type": "application/json"}
    headers["Remote-User"] = userinfo["preferred_username"]
    headers["Remote-Name"] = userinfo["preferred_username"]
    headers["Remote-Email"] = userinfo["email"]
    if "org_id" in context:
        headers["X-Grafana-Org-Id"] = str(context["org_id"])
    return headers


async def init_org(org_id=1, name="drycc"):
    headers = {"Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(api_url(f"/api/orgs/{org_id}", is_admin=True), headers=headers)
        org = resp.json()
        if org["name"] == name:
            return
        await client.put(
            api_url(f"/api/orgs/{org_id}", is_admin=True),
            headers=headers,
            json={"name": name},
        )


async def sync_user(context: dict, token: dict, userinfo: dict):
    async with httpx.AsyncClient() as client:
        resp = await client.get(api_url("/api/user"), headers=api_headers(context, userinfo))
        user = resp.json()
        context["user_id"] = user["id"]
        if userinfo["is_superuser"]:
            await client.put(
                api_url(f"/api/admin/users/{context["user_id"]}/permissions", is_admin=True),
                headers=api_headers(context, userinfo),
                json={"isGrafanaAdmin": userinfo["is_superuser"]},
            )


async def sync_role(context: dict, token: dict, userinfo: dict):
    async with httpx.AsyncClient() as client:
        resp = await client.get(api_url("/api/user/orgs"), headers=api_headers(context, userinfo))
        orgs = resp.json()
        has_admin_org = False
        for org in orgs:
            if org["orgId"] == 1:
                has_admin_org = True
            else:
                context["org_id"] = org["orgId"]
        if has_admin_org and not (userinfo["is_superuser"] or userinfo["is_staff"]):
            await client.delete(
                api_url(f"/api/orgs/1/users/{context["user_id"]}", is_admin=True),
                headers=api_headers(context, userinfo),
            )
        elif not has_admin_org and (userinfo["is_superuser"] or userinfo["is_staff"]):
            await client.post(
                api_url("/api/orgs/1/users", is_admin=True),
                headers=api_headers(context, userinfo),
                json={"loginOrEmail": userinfo["preferred_username"], "role": "Admin"},
            )
    await init_org(org_id=context["org_id"], name=userinfo["preferred_username"])


async def sync_alerting(context: dict, token: dict, userinfo: dict):
    alerting_path = os.path.join(os.path.dirname(__file__), "..", "alerting")
    async with httpx.AsyncClient() as client:
        for filename in os.listdir(alerting_path):
            with open(os.path.join(alerting_path, filename)) as f:
                await client.post(
                    api_url("/api/v1/provisioning/alert-rules"),
                    headers=api_headers(context, userinfo),
                    json=json.load(f),
                )


async def sync_datasource(context: dict, token: dict, userinfo: dict):
    username = userinfo["preferred_username"]
    prometheus_url = f"{DRYCC_CONTROLLER_API_URL}/v2/prometheus/{username}"
    datasource_name = "Prometheus on Drycc"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            api_url("/api/datasources/name/{datasource_name}"),
            headers=api_headers(context, userinfo))
        if resp.status_code == 200:
            datasource = resp.json()
            drycc_token_uuid = datasource.get("jsonData", {}).get("tokenId", None)
            drycc_token = await _get_or_create_drycc_token(drycc_token_uuid, token)
            if "token" in drycc_token:
                datasource["jsonData"]["tokenId"] = drycc_token["uuid"]
                datasource["secureJsonData"] = {
                    "httpHeaderValue1": f"Token {drycc_token["token"]}",
                }
                await client.put(
                    api_url(f"/api/datasources/uid/{datasource["uid"]}"),
                    headers=api_headers(context, userinfo),
                    json=datasource,
                )
            return
        drycc_token = await _get_or_create_drycc_token(None, token)
        await client.post(
            api_url("/api/datasources"),
            headers=api_headers(context, userinfo),
            json={
                "name": datasource_name,
                "type": "prometheus",
                "url": prometheus_url,
                "access": "proxy",
                "basicAuth": False,
                "jsonData": {
                    "tokenId": drycc_token["uuid"],
                    "httpHeaderName1": "Authorization",
                    "httpMethod": "GET",
                    "timeInterval": DRYCC_GRAFANA_REFRESH,
                },
                "secureJsonData": {
                    "httpHeaderValue1": f"Token {drycc_token["token"]}",
                },
            },
        )


async def sync_dashboard(context: dict, token: dict, userinfo: dict):
    dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard")
    async with httpx.AsyncClient() as client:
        for filename in os.listdir(dashboard_path):
            with open(os.path.join(dashboard_path, filename)) as f:
                dashboard = json.load(f)
                dashboard.update({"id": None, "refresh": DRYCC_GRAFANA_REFRESH})
                await client.post(
                    api_url("/api/dashboards/db"),
                    headers=api_headers(context, userinfo),
                    json={
                        "dashboard": dashboard,
                        "overwrite": True,
                    },
                )


async def _get_or_create_drycc_token(drycc_token_uuid, token: dict):
    headers = {"Authorization": f"Bearer {token["access_token"]}"}
    async with httpx.AsyncClient() as client:
        if drycc_token_uuid:
            resp = await client.get(
                f"{DRYCC_CONTROLLER_API_URL}/v2/tokens/{drycc_token_uuid}", headers=headers)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code != 404:
                raise ValueError(
                    f"the Drycc controller returned an unsupported status code {resp.status_code}"
                )
        resp = await client.post(
            f"{DRYCC_CONTROLLER_API_URL}/v2/auth/token/?alias=grafana-datasource",
            headers=headers,
            json=token,
        )
        return resp.json()
