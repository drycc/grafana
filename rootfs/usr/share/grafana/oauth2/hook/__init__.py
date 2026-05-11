from .grafana import (
    has_changed, init_org, sync_user, sync_role, sync_default, sync_datasources, sync_dashboards,
    sync_alerting
)

state_changed = has_changed
startup_hooks = [init_org]
login_hooks = [
    sync_user, sync_role, sync_default, sync_datasources, sync_dashboards, sync_alerting
]
destroy_hooks = []
