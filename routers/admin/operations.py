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

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

def clean_date(date_val):
    """Sincronizza i formati data eliminando fusi orari e residui T/Z."""
    if not date_val:
        return None
    try:
        if hasattr(date_val, 'date'):
            return date_val.date()
        # Pulizia stringa: prendiamo solo i primi 10 caratteri (YYYY-MM-DD)
        date_str = str(date_val).split('T')[0].split(' ')[0].strip()
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        return None

@router.get("/admin/analytics")
async def analytics(admin=Depends(get_current_admin)):
    # 1. Recupero TUTTE le prenotazioni
    bookings = await db.bookings.find({}, {'_id': 0}).to_list(10000)
    now = datetime.now(timezone.utc).date()
    
    # 2. Generiamo i 12 mesi per il grafico (mappa anno-mese -> indice lista)
    months = []
    month_map = {}
    
    # Partiamo dal mese corrente e andiamo indietro di 11 mesi
    for i in range(11, -1, -1):
        # Calcolo data del primo giorno del mese relativo
        target_date = now.replace(day=1)
        for _ in range(i):
            last_day_prev_month = target_date - timedelta(days=1)
            target_date = last_day_prev_month.replace(day=1)
        
        key = f"{target_date.year}-{target_date.month}"
        month_map[key] = len(months)
        months.append({
            'year': target_date.year, 
            'month': target_date.month, 
            'label': target_date.strftime('%b %Y'),
            'nights': 0, 
            'revenue': 0.0
        })

    total_revenue_accumulated = 0.0

    # 3. Elaborazione prenotazioni
    for b in bookings:
        status = b.get('status')
        # Consideriamo solo prenotazioni valide (confermate, pendenti o esterne)
        if status not in ['confirmed', 'pending', 'external']:
            continue

        try:
            # Recupero e somma del prezzo totale
            price_val = float(b.get('total_price', 0))
            total_revenue_accumulated += price_val

            # Recupero e pulizia date
            check_in = clean_date(b.get('check_in'))
            check_out = clean_date(b.get('check_out'))
            
            if check_in:
                # Troviamo il mese di appartenenza nel grafico tramite la mappa
                key = f"{check_in.year}-{check_in.month}"
                if key in month_map:
                    idx = month_map[key]
                    # Assegniamo l'intero ricavo al mese del check-in
                    months[idx]['revenue'] = round(months[idx]['revenue'] + price_val, 2)
                    
                    # Calcolo notti per la statistica del grafico
                    if check_out:
                        diff = (check_out - check_in).days
                        months[idx]['nights'] += max(0, diff)
                        
        except Exception as e:
            logging.error(f"Errore calcolo analytics: {e}")
            continue

    return {
        'monthly': months,
        'totals': {
            'revenue': round(total_revenue_accumulated, 2),
            'nights': sum(m['nights'] for m in months),
            'confirmed_bookings': sum(1 for b in bookings if b.get('status') == 'confirmed'),
            'pending_bookings': sum(1 for b in bookings if b.get('status') == 'pending'),
            'external_bookings': sum(1 for b in bookings if b.get('status') == 'external'),
            'new_messages': await db.contact_messages.count_documents({'status': 'new'}),
        },
    }

# --- Altre rotte operative (Settings, iCal, Gallery) ---

@router.get("/admin/settings")
async def get_admin_settings(admin=Depends(get_current_admin)):
    return await get_settings()

@router.put("/admin/settings")
async def update_admin_settings(updates: SettingsUpdate, admin=Depends(get_current_admin)):
    patch = updates.model_dump(exclude_unset=True)
    if not patch: raise HTTPException(400, 'No fields to update')
    await db.settings.update_one({'id': 'global'}, {'$set': patch}, upsert=True)
    return await get_settings()

@router.post("/admin/ical/sync")
async def ical_sync(admin=Depends(get_current_admin)):
    return await ical_sync_run()

@router.get("/admin/gallery", response_model=List[GalleryImage])
async def list_gallery_images(admin=Depends(get_current_admin)):
    return await db.gallery.find({}, {'_id': 0}).sort("order", 1).to_list(1000)

@router.post("/admin/gallery/upload", response_model=GalleryImage)
async def upload_gallery_image(
    file: UploadFile = File(...),
    category: str = Form("gallery"),
    caption: Optional[str] = Form(""),
    admin=Depends(get_current_admin)
):
    try:
        upload_result = cloudinary.uploader.upload(file.file, folder="light_blue_gallery")
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

@router.patch("/admin/gallery/{image_id}", response_model=GalleryImage)
async def update_image_info(image_id: str, updates: GalleryImageUpdate, admin=Depends(get_current_admin)):
    patch = updates.model_dump(exclude_unset=True)
    result = await db.gallery.find_one_and_update(
        {"id": image_id}, {"$set": patch},
        projection={'_id': 0}, return_document=True
    )
    if not result: raise HTTPException(404, "Immagine non trovata")
    return result

@router.delete("/admin/gallery/{image_id}")
async def delete_gallery_image(image_id: str, admin=Depends(get_current_admin)):
    image = await db.gallery.find_one({"id": image_id})
    if not image: raise HTTPException(404, "Immagine non trovata")
    try:
        cloudinary.uploader.destroy(image['public_id'])
        await db.gallery.delete_one({"id": image_id})
        return {"status": "success", "message": "Eliminata correttamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
