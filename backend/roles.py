ADMIN_ROLES = {"pm", "lead"}


def is_admin_from_roles(roles) -> bool:
    if not roles:
        return False
    return any(str(role).lower() in ADMIN_ROLES for role in roles)