from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..core.ssh_keys import SSH_USER, private_key_path, read_public_key

router = APIRouter()


def _require_admin(request: Request):
    u = request.session.get("user") or {}
    if not (isinstance(u, dict) and u.get("role") == "admin"):
        raise HTTPException(403, "admin only")


@router.get("/ssh-key/public")
def get_ssh_public_key(request: Request):
    _require_admin(request)
    return PlainTextResponse(read_public_key() + "\n")


@router.get("/ssh-key/info")
def get_ssh_key_info(request: Request):
    _require_admin(request)
    pub = read_public_key()
    return {
        "public_key": pub,
        "private_key_path": str(private_key_path()),
        "ssh_user": SSH_USER,
    }
