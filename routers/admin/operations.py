"""Operations: analytics, settings, iCal sync, and Image management."""
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import cloudinary
import cloudinary.uploader
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from auth import get_current_admin
from db import db, get_settings
from ical_service import ical_sync_run
from models import SettingsUpdate, GalleryImage, GalleryImageUpdate

router = APIRouter()

# Configurazione Cloudinary (Recuperata da variabili d'ambiente)
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

@router.get("/admin/analytics")
async def analytics(admin=Depends(get_current_admin)):
    # MODIFICA: Filtriamo le prenotazioni per includere solo quelle confermate o esterne.
    # Questo esclude le 'pending' (non pagate) e le 'cancelled'.
    query = {"status": {"$in": ["confirmed", "external"]}}
    bookings = await db.bookings.find(query, {'_id': 0}).to_list(10000)
    
    now = datetime.now(timezone.utc).date()
    
    months = []
    month_map = {}
    
    # 2. Generazione dei 12 mesi (stabile e indipendente dal server)
    base_date = now.replace(day=1)
    for i in range(11, -1, -1):
        # Calcolo mese per mese andando a ritroso
        target_date = (base_date - timedelta(days=i*31)).replace(day=1)
        m_key = target_date.strftime('%Y-%m')
        m_label = target_date.strftime('%b') # Esempio: Jan, Feb...
        
        entry = {"name": m_label, "guadagni": 0, "notti": 0}
        months.append(entry)
        month_map[m_key] = entry

    total_revenue = 0
    total_nights = 0

    # 3. Aggregazione dati
    for b in bookings:
        try:
            # Calcolo notti e ricavo totale
            check_in = datetime.strptime(b['check_in'], '%Y-%m-%d').date()
            check_out = datetime.strptime(b['check_out'], '%Y-%m-%d').date()
            notti = (check_out - check_in).days
            prezzo = float(b.get('total_price', 0))

            total_revenue += prezzo
            total_nights += notti

            # Distribuzione nei grafici mensili (basata sul check-in)
            m_key = check_in.strftime('%Y-%m')
            if m_key in month_map:
                month_map[m_key]["guadagni"] += prezzo
                month_map[m_key]["notti"] += notti
        except Exception as e:
            logging.error(f"Errore processamento analytics per booking {b.get('id')}: {e}")
            continue

    return {
        "total_revenue": round(total_revenue, 2),
        "total_nights": total_nights,
        "bookings_count": len(bookings),
        "chart_data": months
    }

@router.get("/admin/settings")
async def fetch_settings(admin=Depends(get_current_admin)):
    return await get_settings()

@router.put("/admin/settings")
async def update_settings(updates: SettingsUpdate, admin=Depends(get_current_admin)):
    patch = updates.model_dump(exclude_unset=True)
    await db.settings.update_one({}, {"$set": patch}, upsert=True)
    return {"ok": True}

@router.post("/admin/ical/sync")
async def sync_ical_now(admin=Depends(get_current_admin)):
    imported = await ical_sync_run()
    return {"ok": True, "imported": imported}

# --- GALLERY MANAGEMENT ---

@router.get("/gallery", response_model=List[GalleryImage])
async def get_gallery():
    return await db.gallery.find({}, {'_id': 0}).sort('order', 1).to_list(1000)

@router.post("/admin/gallery", response_model=GalleryImage)
async def upload_gallery_image(
    file: UploadFile = File(...),
    caption: str = Form(\"\"),
    category: str = Form(\"general\"),
    admin=Depends(get_current_admin)
):
    try:
        upload_result = cloudinary.uploader.upload(file.file, folder=\"light_blue_gallery\")
        new_image = GalleryImage(
            url=upload_result['secure_url'],
            public_id=upload_result['public_id'],
            caption=caption,
            category=category,
            order=0
        )
        await db.gallery.insert_one(new_image.model_dump())
        return new_image
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch(\"/admin/gallery/{image_id}\", response_model=GalleryImage)
async def update_image_info(image_id: str, updates: GalleryImageUpdate, admin=Depends(get_current_admin)):
    patch = updates.model_dump(exclude_unset=True)
    result = await db.gallery.find_one_and_update(
        {\"id\": image_id}, {\"$set\": patch},
        projection={'_id': 0}, return_document=True
    )
    if not result: raise HTTPException(404, \"Immagine non trovata\")
    return result

@router.delete(\"/admin/gallery/{image_id}\")
async def delete_gallery_image(image_id: str, admin=Depends(get_current_admin)):
    image = await db.gallery.find_one({\"id\": image_id})
    if not image: raise HTTPException(404, \"Immagine non trovata\")
    try:
        cloudinary.uploader.destroy(image['public_id'])
        await db.gallery.delete_one({\"id\": image_id})
        return {\"ok\": True}
    except Exception as e:
        raise HTTPException(500, str(e))
