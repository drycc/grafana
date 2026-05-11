import json
import random
import string
import argparse
import uuid
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from hook import state_changed, startup_hooks, login_hooks, destroy_hooks
from settings import settings
from credentials import get_token


def randstr(k=32):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))


parser = argparse.ArgumentParser()
parser.add_argument('--port', default="8000", help='specify alternate port')
parser.add_argument('--bind', default="0.0.0.0", help='specify alternate bind address')
parser.add_argument('--client-id', required=True, help='the OAuth Client ID')
parser.add_argument('--client-secret', required=True, help='the OAuth Client secret')
parser.add_argument('--oidc-issuer-url', required=True, help='OpenID Connect issuer URL')
parser.add_argument('--cookie-name', default="_oauth2_proxy", help='the name of the cookie')
parser.add_argument('--cookie-secret', default=randstr, help='the seed string for secure cookies')
args = parser.parse_args()


http_client = None

SESSION_KEY_PREFIX = "oauth2:session:"
STATE_CHECK_KEY_PREFIX = "oauth2:state_check:"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)

    try:
        await get_token()
    except Exception as e:
        raise RuntimeError(f"Failed to initialize token: {e}")

    for startup_hook in startup_hooks:
        await startup_hook()
    yield
    await http_client.aclose()
    for destroy_hook in destroy_hooks:
        await destroy_hook()


app = FastAPI(lifespan=lifespan)
oauth = OAuth()
oauth.register(
    name='oidc',
    client_id=args.client_id,
    client_secret=args.client_secret,
    client_kwargs={'scope': 'openid profile email'},
    server_metadata_url=args.oidc_issuer_url + "/.well-known/openid-configuration",
)
app.add_middleware(
    SessionMiddleware, secret_key=args.cookie_secret, session_cookie=args.cookie_name
)


@app.get("/oauth2/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/oauth2/sign_in")
async def oauth2_sign_in(request: Request):
    sid = request.session.get("sid")
    redirect_to = request.query_params.get("redirect", "/")
    if sid:
        valkey_client = await settings.get_valkey_client()
        try:
            key = f"{SESSION_KEY_PREFIX}{sid}"
            if await valkey_client.get(key):
                return RedirectResponse(redirect_to)
        finally:
            await valkey_client.aclose()
    redirect_url = request.url_for("oauth2_callback").replace_query_params(redirect=redirect_to)
    return await oauth.oidc.authorize_redirect(request, redirect_url)


@app.get("/oauth2/callback")
async def oauth2_callback(request: Request):
    token = await oauth.oidc.authorize_access_token(request)
    if 'userinfo' not in token:
        userinfo = await oauth.oidc.userinfo(token=token)
    else:
        token.pop("id_token", None)
        userinfo = token.pop("userinfo")

    sid = uuid.uuid4().hex
    valkey_client = await settings.get_valkey_client()
    try:
        key = f"{SESSION_KEY_PREFIX}{sid}"
        payload = json.dumps({"token": token, "user": userinfo})
        expires_in = token.get("expires_in") or settings.session_ttl_fallback_seconds
        await valkey_client.set(key, payload, ex=expires_in)
    finally:
        await valkey_client.aclose()
    request.session['sid'] = sid

    context = {}
    for login_hook in login_hooks:
        await login_hook(context, token, userinfo)
    return RedirectResponse(url='/')


@app.get("/oauth2/userinfo")
async def oauth2_userinfo(request: Request):
    sid = request.session.get("sid")
    if not sid:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    session_key = f"{SESSION_KEY_PREFIX}{sid}"
    state_check_key = f"{STATE_CHECK_KEY_PREFIX}{sid}"
    valkey_client = await settings.get_valkey_client()
    try:
        raw = await valkey_client.get(session_key)
        if not raw:
            request.session.pop("sid", None)
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

        payload = json.loads(raw)
        token, userinfo = payload["token"], payload["user"]

        # SET NX EX guarantees only one request per cooldown window runs the
        # expensive upstream check, even when Grafana fans out concurrent
        # /oauth2/userinfo calls.
        cooldown = settings.state_check_cooldown_seconds
        check_due = cooldown <= 0 or bool(
            await valkey_client.set(state_check_key, "1", ex=cooldown, nx=True)
        )
        if check_due and await state_changed(token, userinfo):
            await valkey_client.delete(session_key)
            request.session.pop("sid", None)
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    finally:
        await valkey_client.aclose()

    headers = {
        "Remote-User": userinfo['preferred_username'],
        "Remote-Name": userinfo['preferred_username'],
        "Remote-Email": userinfo['email'],
    }
    return JSONResponse(content=userinfo, headers=headers)


# ── Proxy routes (zero-overhead version) ───────────────────────────

async def _proxy_request(request: Request, url: str):
    """Minimal proxy: get the token directly from token.py and use it."""
    try:
        token = await get_token()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    headers = dict(request.headers)
    headers.pop("host", None)
    headers["Authorization"] = f"Bearer {token}"
    resp = await http_client.request(
        method=request.method, url=url, headers=headers, params=request.query_params,
        content=await request.body(),
    )
    return StreamingResponse(
        resp.aiter_bytes(), status_code=resp.status_code, headers=dict(resp.headers)
    )


@app.api_route(
    "/proxy/prometheus/{workspace}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def proxy_prometheus(workspace: str, path: str, request: Request):
    url = f"{settings.controller_base_url}/v2/prometheus/{workspace}/{path}"
    return await _proxy_request(request, url)


@app.api_route(
    "/proxy/quickwit/{workspace}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def proxy_quickwit(workspace: str, path: str, request: Request):
    url = f"{settings.controller_base_url}/v2/quickwit/{workspace}/{path}"
    return await _proxy_request(request, url)


# ── Alert webhook (translate Grafana payload → controller schema) ────


@app.post("/alerts/webhook")
async def alerts_webhook(workspace: str, request: Request):
    def _translate_alert(alert: dict) -> dict:
        _SEVERITY_CHOICES = {"info", "warning", "error", "success"}
        _STATUS_TO_SEVERITY = {"firing": "warning", "resolved": "success"}
        labels, annotations = alert.get("labels") or {}, alert.get("annotations") or {}
        title = (annotations.get("summary") or labels.get("alertname") or "Grafana alert")[:255]
        content = annotations.get("description") or title
        severity = (labels.get("severity") if labels.get("severity") in _SEVERITY_CHOICES
                    else _STATUS_TO_SEVERITY.get(alert.get("status", "firing"), "info"))
        message = {"category": "alert", "title": title, "content": content, "severity": severity}
        generator_url = alert.get("generatorURL")
        if generator_url:
            message.update({"action_link": generator_url[:500], "action_text": "View in Grafana"})
        return message

    try:
        token = await get_token()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    grafana_payload = await request.json()
    alerts = (grafana_payload or {}).get("alerts") or []
    translated = [_translate_alert(a) for a in alerts]
    body = json.dumps({"workspace": workspace, "alerts": translated})
    resp = await http_client.post(
        f"{settings.controller_base_url}/v2/alerts",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=body,
    )
    return Response(status_code=resp.status_code)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=args.bind, port=int(args.port))
