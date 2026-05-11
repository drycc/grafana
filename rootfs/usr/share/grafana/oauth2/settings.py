import re

import valkey.asyncio
from pydantic import Field, computed_field, RedisDsn, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> int:
    """Parse a duration string like '60s', '5m', '1h', '7d' into seconds.

    A bare integer (no unit) is treated as seconds. Raises ValueError on
    malformed input so misconfiguration fails fast at startup.
    """
    if value is None:
        raise ValueError("duration value must not be None")
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    number, unit = match.groups()
    return int(number) * _DURATION_MULTIPLIERS[unit.lower()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        case_sensitive=True,
    )

    # Drycc configuration
    drycc_valkey_url: RedisDsn = Field(
        default="redis://localhost:6379/0",
        validation_alias="DRYCC_VALKEY_URL",
    )
    drycc_passport_url: HttpUrl = Field(
        default="http://passport.drycc.cc",
        validation_alias="DRYCC_PASSPORT_URL",
    )
    drycc_passport_key: str = Field(default="", validation_alias="DRYCC_PASSPORT_KEY")
    drycc_passport_secret: str = Field(default="", validation_alias="DRYCC_PASSPORT_SECRET")
    drycc_passport_scopes: str = Field(default="", validation_alias="DRYCC_PASSPORT_SCOPES")
    drycc_controller_url: HttpUrl = Field(
        default="http://controller.drycc.cc",
        validation_alias="DRYCC_CONTROLLER_URL",
    )
    drycc_grafana_refresh: str = Field(default="60s", validation_alias="DRYCC_GRAFANA_REFRESH")

    drycc_state_check_cooldown: str = Field(
        default="60s",
        validation_alias="DRYCC_STATE_CHECK_COOLDOWN",
    )
    drycc_session_ttl_fallback: str = Field(
        default="7d",
        validation_alias="DRYCC_SESSION_TTL_FALLBACK",
    )

    # Grafana configuration
    gf_security_admin_user: str = Field(default="admin", validation_alias="GF_SECURITY_ADMIN_USER")
    gf_security_admin_password: str = Field(
        default="admin",
        validation_alias="GF_SECURITY_ADMIN_PASSWORD",
    )
    gf_server_http_port: int = Field(default=3000, validation_alias="GF_SERVER_HTTP_PORT")
    gf_database_url: str = Field(default="", validation_alias="GF_DATABASE_URL")

    @computed_field
    @property
    def passport_token_url(self) -> str:
        return f"{str(self.drycc_passport_url).rstrip('/')}/oauth/token/"

    @computed_field
    @property
    def passport_userinfo_url(self) -> str:
        return f"{str(self.drycc_passport_url).rstrip('/')}/oauth/userinfo/"

    @computed_field
    @property
    def controller_base_url(self) -> str:
        """Controller URL normalized without trailing slash.

        Pydantic's HttpUrl appends a trailing slash on serialization, which
        causes double slashes when callers concatenate paths with a leading
        slash (e.g. f"{url}/v2/..."). Use this property for any path joining.
        """
        return str(self.drycc_controller_url).rstrip('/')

    @computed_field
    @property
    def state_check_cooldown_seconds(self) -> int:
        return _parse_duration(self.drycc_state_check_cooldown)

    @computed_field
    @property
    def session_ttl_fallback_seconds(self) -> int:
        return _parse_duration(self.drycc_session_ttl_fallback)

    async def get_valkey_client(self) -> valkey.asyncio.Valkey:
        return valkey.asyncio.from_url(
            str(self.drycc_valkey_url),
            decode_responses=True,
        )


settings = Settings()
