import random
import string
import argparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware

from hook import startup_hooks, login_hooks, destroy_hooks


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    for startup_hook in startup_hooks:
        await startup_hook()
    yield
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
    if "user" in request.session:
        return RedirectResponse(request.query_params.get("redirect", "/"))
    redirect_url = request.url_for("oauth2_callback")
    redirect_url.replace_query_params(redirect=request.query_params.get("redirect", "/"))
    return await oauth.oidc.authorize_redirect(request, redirect_url)


@app.get("/oauth2/callback")
async def oauth2_callback(request: Request):
    token = await oauth.oidc.authorize_access_token(request)
    if 'userinfo' not in token:
        userinfo = await oauth.oidc.userinfo(token=token)
    else:
        token.pop("id_token", None)
        userinfo = token.pop("userinfo")
    request.session['user'] = userinfo
    context = {}
    for login_hook in login_hooks:
        await login_hook(context, token, userinfo)
    return RedirectResponse(url='/')


@app.get("/oauth2/userinfo")
async def oauth2_userinfo(request: Request):
    if "user" not in request.session:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    userinfo = request.session['user']
    headers = {
        "Remote-User": userinfo['preferred_username'],
        "Remote-Name": userinfo['preferred_username'],
        "Remote-Email": userinfo['email'],
    }
    return JSONResponse(content=userinfo, headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=args.bind, port=int(args.port))
