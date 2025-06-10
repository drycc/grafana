from .grafana import (
    init_org, sync_user, sync_role, sync_datasource, sync_alerting, sync_dashboard
)

startup_hooks = [init_org]
login_hooks = [sync_user, sync_role, sync_datasource, sync_alerting, sync_dashboard]
destroy_hooks = []
