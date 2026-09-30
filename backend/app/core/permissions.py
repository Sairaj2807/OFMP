"""Roles and permissions.

Roles are defined in code (reviewed and versioned with it) rather than in
database rows: the set is small and a permission change is a code change
either way. Authorization is always checked server-side via
require_permission (backend/app/api/deps.py)."""

PERMISSIONS = {
    "market.read",        # live and historical market data, charts, replay
    "chart.read", "chart.write",
    "workspace.read", "workspace.write",
    "alerts.create", "alerts.delete",
    "admin.users",        # list/inspect users, sessions
    "admin.data",         # data quality, ingestion
    "admin.system",       # providers, system health, dangerous operations
}

_USER = {"market.read", "chart.read", "chart.write", "workspace.read", "workspace.write",
         "alerts.create", "alerts.delete"}

ROLE_PERMISSIONS = {
    "user": frozenset(_USER),
    "support": frozenset(_USER | {"admin.users", "admin.data"}),
    "admin": frozenset(_USER | {"admin.users", "admin.data", "admin.system"}),
    "super_admin": frozenset(PERMISSIONS),
}

ROLES = tuple(ROLE_PERMISSIONS)
ORG_ROLES = ("owner", "admin", "member")


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
