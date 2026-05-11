import sys
import logging
import argparse
import asyncio

import httpx
import valkey
import valkey.asyncio
import valkey.exceptions

from settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────

TOKEN_KEY = "datasource:oauth2:token"
INIT_LOCK_KEY = "datasource:oauth2:init_lock"
INIT_LOCK_TTL = 30  # Lock TTL 30 seconds
REFRESH_THRESHOLD = 7 * 24 * 3600  # Refresh when less than 7 days remain (for CronJob)

# ── Function 1: used by main.py (get token, create if missing) ─────────


async def _fetch_and_save_token(valkey_client: valkey.asyncio.Valkey) -> str:
    """
    Fetch a new token from Passport and save it to Valkey with TTL = expires_in.
    Returns the access_token string.
    """
    logger.info("Fetching new token from %s", settings.passport_token_url)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.passport_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.drycc_passport_key,
                "client_secret": settings.drycc_passport_secret,
                "scope": settings.drycc_passport_scopes,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        token_response = resp.json()

    access_token = token_response["access_token"]
    expires_in = token_response.get("expires_in", settings.session_ttl_fallback_seconds)

    await valkey_client.set(TOKEN_KEY, access_token, ex=expires_in)

    logger.info("Token saved (expires_in: %d seconds)", expires_in)
    return access_token


async def introspect_token(token: str) -> bool:
    """
    Check if the token is valid and has the required scopes via introspection.
    """
    if not settings.drycc_passport_scopes:
        return True

    introspect_url = settings.passport_token_url.replace("token/", "introspect/")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                introspect_url,
                auth=(settings.drycc_passport_key, settings.drycc_passport_secret),
                data={"token": token},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("active"):
                    token_scopes = set(data.get("scope", "").split())
                    required_scopes = set(settings.drycc_passport_scopes.split())
                    return required_scopes == token_scopes
    except Exception as e:
        logger.info(f"Error introspecting token: {e}")
    return False


async def get_token() -> str:
    """
    Get the current access token.

    Flow:
    1. Fast path: read Valkey directly and return if valid
    2. Try to acquire a Valkey lock with blocking wait
    3. Double-check token and refresh if necessary

    Returns: access_token string
    Raises: RuntimeError (if token cannot be obtained)
    """
    valkey_client = await settings.get_valkey_client()
    token = await valkey_client.get(TOKEN_KEY)
    if token and await introspect_token(token):
        return token
    try:
        async with valkey_client.lock(INIT_LOCK_KEY, timeout=30.0, blocking_timeout=30.0):
            token = await valkey_client.get(TOKEN_KEY)
            if token and await introspect_token(token):
                return token
            return await _fetch_and_save_token(valkey_client)
    except valkey.exceptions.LockError:
        raise RuntimeError("Timeout waiting for token refresh lock.")


# ── Function 2: CronJob entry point ──────────────────────────────────────

async def main():
    """
    Asynchronous CronJob entry point.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force refresh regardless of current token validity",
    )
    args = parser.parse_args()

    logger.info("Token refresher started (force=%s)", args.force)

    valkey_client = await settings.get_valkey_client()

    try:
        try:
            await valkey_client.ping()
            logger.info("Connected to Valkey")
        except Exception as e:
            logger.error("Failed to connect to Valkey: %s", e)
            sys.exit(1)

        should_refresh = args.force

        if not should_refresh:
            remaining = await valkey_client.ttl(TOKEN_KEY)
            # ttl returns -2 if key missing, -1 if no expiration set
            if remaining < 0:
                logger.warning("No valid token found, fetching new token")
                should_refresh = True
            else:
                remaining_days = remaining / 86400
                logger.info("Current token has %.1f days remaining", remaining_days)
                if remaining > REFRESH_THRESHOLD:
                    logger.info("Token still valid, skip refresh")
                    return
                logger.warning("Token expires in %.1f days, refreshing...", remaining_days)
                should_refresh = True

        if should_refresh:
            try:
                await _fetch_and_save_token(valkey_client)
                logger.info("Token refresh completed successfully")
            except Exception as e:
                logger.error("Token refresh failed: %s", e)
                sys.exit(1)
    finally:
        await valkey_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
