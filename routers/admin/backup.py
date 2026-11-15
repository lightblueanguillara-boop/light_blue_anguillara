"""Backup dei dati dal pannello: download immediato e invio per email."""
from fastapi import APIRouter, Depends, Request, Response

import backup
from auth import get_current_admin
from ratelimit import rate_limit

router = APIRouter()


@router.get("/admin/backup")
async def download_backup(request: Request, admin=Depends(get_current_admin)):
    rate_limit(request, 'backup', 10, 3600)
    data, meta = await backup.build_backup()
    return Response(content=data, media_type='application/gzip', headers={
        'Content-Disposition': f'attachment; filename="{backup.backup_filename()}"',
        'X-Backup-Documents': str(sum(meta['counts'].values())),
    })


@router.post("/admin/backup/email")
async def email_backup_now(request: Request, admin=Depends(get_current_admin)):
    rate_limit(request, 'backup-email', 5, 3600)
    return await backup.email_backup()
