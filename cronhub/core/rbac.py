def effective_role_for_tenant(user: dict, tenant: str) -> str:
    """Resolves a user's role for a specific tenant.

    Global admin (from Keycloak DevOps group or local admin login) always
    wins. Otherwise a per-tenant grant (cronhub_tenant_access) overrides the
    user's default group-derived role for that tenant.
    """
    if not isinstance(user, dict):
        return ""
    global_role = (user.get("role") or "").strip()
    if global_role == "admin":
        return "admin"
    tenant_roles = user.get("tenant_roles") or {}
    return tenant_roles.get(tenant) or global_role
