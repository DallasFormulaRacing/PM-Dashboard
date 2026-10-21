import secrets
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from config import settings

router = APIRouter(prefix="/auth")

valid_states = set()


@router.get("/login")
def login():
    state = secrets.token_urlsafe(32)
    valid_states.add(state)

    params = {
        "client_id": settings.DFR_CLIENT_ID,
        "redirect_uri": settings.DFR_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile roles",
        "state": state,
    }
    return RedirectResponse(f"{settings.DFR_AUTH_URL}?{urlencode(params)}")