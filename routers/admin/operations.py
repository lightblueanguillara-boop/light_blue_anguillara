"""Operations: analytics, settings, iCal sync, and Image management."""
import os
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

@router.get("/admin/analytics")
async def analytics(admin=Depends(get_current_admin)):
    # 1. Recupero prenotazioni
    bookings = await db.bookings.find(
        {'status': {'$in': ['confirmed', 'pending']}}, {'_id': 0}
    ).to_list(10000)
    
    now = datetime.now(timezone.utc).date()
    months = []
    
    # 2. Genero gli ultimi 12 mesi (fino al mese corrente)
    first_of_this_month = now.replace(day=1)
    for i in range(11, -1, -1):
        # Sottraiamo i mesi in modo più preciso
        m_date = (first_of_this_month - timedelta(days=i*31)).replace(day=1)
        months.append({
            'year': m_date.year, 
            'month': m_date.month, 
            'label': m_date.strftime('%b %Y'),
            'nights': 0, 
            'revenue': 0.0,
        })

    total_revenue_accumulated = 0.0

    # 3. Elaborazione
    for b in bookings:
        try:
            check_in = datetime.strptime(b['check_in'], '%Y-%m-%d').date()
            check_out = datetime.strptime(b['check_out'], '%Y-%m-%d').date()
            
            price_val = float(b.get('total_price', 0))
            total_revenue_accumulated += price_val

            # Calcoliamo la durata
            delta = check_out - check_in
            nights_total = max(1, delta.days)
            nightly_rate = price_val / nights_total

            # Invece di usare daterange esterno, usiamo un ciclo interno sicuro
            for n in range(nights_total):
                current_day = check_in + timedelta(days=n)
                for mm in months:
                    if current_day.year == mm['year'] and current_day.month == mm['month']:
                        mm['nights'] += 1
                        mm['revenue'] += round(nightly_rate, 2)
                        
        except Exception as e:
            print(f"DEBUG ANALYTICS ERROR: {e}")
            continue

    # Calcolo totali per la risposta
    total_nights_calculated = sum(m['nights'] for m in months)
    confirmed_count = sum(1 for b in bookings if b['status'] == 'confirmed')
    pending_count = sum(1 for b in bookings if b['status'] == 'pending')
    messages_new = await db.contact_messages.count_documents({'status': 'new'})

    return {
        'monthly': months,
        'totals': {
            'revenue': round(total_revenue_accumulated, 2),
            'nights': total_nights_calculated,
            'confirmed_bookings': confirmed_count,
            'pending_bookings': pending_count,
            'new_messages': messages_new,
        },
    }

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
        return {"status": "success", "message": "Eliminata"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
