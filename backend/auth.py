import secrets
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from config import settings

router = APIRouter(prefix="/auth")

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"

valid_states = set()


@router.get("/login")
def login():
    state = secrets.token_urlsafe(32)
    valid_states.add(state)

    params = {
        "client_id": settings.discord_client_id,
        "redirect_uri": settings.discord_redirect_uri,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    }
    return RedirectResponse(f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}")