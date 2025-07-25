import os
import json
import httpx
from string import Template
from psycopg import AsyncConnection

DEFAULT_HEADERS = {"Content-Type": "application/json"}
DRYCC_CONTROLLER_URL = os.environ.get('DRYCC_CONTROLLER_URL')
DRYCC_GRAFANA_REFRESH = os.environ.get('DRYCC_GRAFANA_REFRESH', '60s')
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


async def sync_default(context: dict, token: dict, userinfo: dict):
    async with httpx.AsyncClient() as client:
        await client.post(
            api_url("/api/folders"),
            headers=api_headers(context, userinfo),
            json={"uid": "drycc", "title": "drycc"},
        )
        await client.post(
            api_url("/api/v1/provisioning/contact-points"),
            headers=api_headers(context, userinfo),
            json={
                "uid": "grafana-default-email",
                "name": "grafana-default-email",
                "type": "email",
                "settings": {
                    "addresses": f"{userinfo["email"]}"
                },
                "disableResolveMessage": False
            },
        )


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


async def sync_datasources(context: dict, token: dict, userinfo: dict):
    headers = api_headers(context, userinfo)
    datasources_path = os.path.join(os.path.dirname(__file__), "..", "datasources")
    created, drycc_token = await _get_or_create_drycc_token(userinfo["preferred_username"], token)
    async with httpx.AsyncClient() as client:
        for filename in os.listdir(datasources_path):
            with open(os.path.join(datasources_path, filename)) as f:
                template = Template(f.read())
                datasource = json.loads(template.substitute(
                    controller_url=DRYCC_CONTROLLER_URL,
                    username=userinfo["preferred_username"],
                    time_interval=DRYCC_GRAFANA_REFRESH,
                    token=drycc_token
                ))
                resp = await client.get(
                    api_url(f"/api/datasources/name/{datasource["name"]}"), headers=headers)
                if resp.status_code == 200:
                    if created:
                        datasource = resp.json()
                        datasource["secureJsonData"] = {
                            "httpHeaderValue1": f"Token {drycc_token}",
                        }
                        await _set_datasource_read_only(False, datasource["id"])
                        await client.put(
                            api_url(f"/api/datasources/uid/{datasource["uid"]}"),
                            headers=headers, json=datasource)
                        await _set_datasource_read_only(True, datasource["id"])
                elif resp.status_code == 404:
                    resp = await client.post(
                        api_url("/api/datasources"), headers=headers, json=datasource)
                    await _set_datasource_read_only(True, resp.json()["datasource"]["id"])
                else:
                    raise ValueError(f"grafana returned an unexpected status: {resp.status_code}")


async def sync_dashboards(context: dict, token: dict, userinfo: dict):
    dashboards_path = os.path.join(os.path.dirname(__file__), "..", "dashboards")
    async with httpx.AsyncClient() as client:
        for filename in os.listdir(dashboards_path):
            with open(os.path.join(dashboards_path, filename)) as f:
                dashboard = json.load(f)
                dashboard.update({"id": None, "refresh": DRYCC_GRAFANA_REFRESH})
                await client.post(
                    api_url("/api/dashboards/db"),
                    headers=api_headers(context, userinfo),
                    json={
                        "dashboard": dashboard,
                        "folderUid": "drycc",
                        "overwrite": True,
                    },
                )


async def _set_datasource_read_only(read_only: bool, id: str):
    async with await AsyncConnection.connect(os.environ.get("GF_DATABASE_URL")) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "UPDATE data_source SET read_only=%s WHERE id=%s",
                (read_only, id)
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
                headers = {"Authorization": f"Bearer {token["access_token"]}"}
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
            drycc_token = (await cursor.fetchone())[0]
        created, drycc_token = await _check_or_create_drycc_token(drycc_token, token)
        if created:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE user_auth SET o_auth_id_token=%s WHERE auth_module=%s AND auth_id=%s",
                    (drycc_token, "authproxy", username)
                )
                await conn.commit()
        return created, drycc_token
