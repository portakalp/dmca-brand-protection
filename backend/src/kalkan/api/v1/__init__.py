"""The customer API under ``/api/v1`` (architecture.md §17).

Every state-changing request needs the ``X-Kalkan-CSRF: 1`` header; the webhooks
(``/api/webhooks/*``) and the evidence pages (``/e/*``) live outside this router and are
protected by basic auth, signatures or tokens instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from kalkan.api.v1 import account, auth, overview, threats, whitelist
from kalkan.api.v1.deps import csrf_header

router = APIRouter(prefix="/api/v1", dependencies=[Depends(csrf_header)])
for module in (auth, overview, threats, whitelist, account):
    router.include_router(module.router)
