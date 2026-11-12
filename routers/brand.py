"""Rotte del logo e dell'immagine di anteprima (il manifest dell'app è generato dal frontend)."""

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

import brand
from auth import get_current_admin

router = APIRouter()


@router.get("/brand/{name}")
async def brand_file(name: str, v: str = ''):
    if name not in brand.PUBLIC_FILES:
        raise HTTPException(404, 'File non trovato')
    data, version = await brand.get_file(name)
    media = 'image/x-icon' if name.endswith('.ico') else 'image/jpeg' if name.endswith('.jpg') else 'image/png'
    # con ?v=<versione> il file non cambia mai: cache lunga. Senza versione: cache breve.
    cache = 'public, max-age=31536000, immutable' if v and v == version else 'public, max-age=3600'
    return Response(content=data, media_type=media, headers={'Cache-Control': cache})


async def _upload(kind: str, file: UploadFile):
    data = await file.read(brand.MAX_UPLOAD_BYTES + 1)
    try:
        version = await brand.save_upload(kind, data) if len(data) <= brand.MAX_UPLOAD_BYTES else None
    except brand.BrandImageError as e:
        raise HTTPException(400, str(e))
    if version is None:
        raise HTTPException(413, 'Immagine troppo grande (massimo 8 MB)')
    return {'ok': True, 'version': version}


@router.post("/admin/brand/logo")
async def upload_logo(file: UploadFile = File(...), admin=Depends(get_current_admin)):
    return await _upload('logo', file)


@router.post("/admin/brand/share")
async def upload_share(file: UploadFile = File(...), admin=Depends(get_current_admin)):
    return await _upload('share', file)


@router.delete("/admin/brand/{kind}")
async def reset_brand(kind: str, admin=Depends(get_current_admin)):
    if kind not in ('logo', 'share'):
        raise HTTPException(404, 'Non trovato')
    await brand.reset(kind)
    return {'ok': True}

