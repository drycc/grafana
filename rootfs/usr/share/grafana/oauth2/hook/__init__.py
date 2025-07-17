from .grafana import (
    init_org, sync_user, sync_role, sync_datasources, sync_folder, sync_alerting, sync_dashboards
)

startup_hooks = [init_org]
login_hooks = [sync_user, sync_role, sync_datasources, sync_folder, sync_alerting, sync_dashboards]
destroy_hooks = []
