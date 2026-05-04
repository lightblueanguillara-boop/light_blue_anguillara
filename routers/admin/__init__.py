"""Admin routes — split into focused sub-modules.

Each sub-module exports its own APIRouter; this package combines them all
into a single router that gets mounted under /api by server.py.
"""
from fastapi import APIRouter

from .auth import router as auth_router
from .bookings import router as bookings_router
from .crm import router as crm_router
from .inbox import router as inbox_router
from .operations import router as operations_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(bookings_router)
router.include_router(inbox_router)
router.include_router(crm_router)
router.include_router(operations_router)
