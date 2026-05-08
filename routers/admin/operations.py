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

def clean_date(date_val):
    """Trasforma date ISO o stringhe sporche in oggetti date puri."""
    if not date_val:
        return None
    if isinstance(date_val, datetime):
        return date_val.date()
    # Se è una stringa ISO (con la T), prendiamo solo la prima parte
    if "T" in str(date_val):
        return datetime.fromisoformat(str(date_val).replace("Z", "+00:00")).date()
    # Se è una stringa semplice YYYY-MM-DD
    return datetime.strptime(str(date_val)[:10], '%Y-%m-%d').date()

@router.get("/admin/analytics")
async def analytics(admin=Depends(get_current_admin)):
    bookings = await db.bookings.find(
        {'status': {'$in': ['confirmed', 'pending']}}, {'_id': 0}
    ).to_list(10000)
    
    now = datetime.now(timezone.utc).date()
    months = []
    
    # 1. Generiamo i 12 mesi per il grafico
    first_of_this_month = now.replace(day=1)
    for i in range(11, -1, -1):
        m_date = (first_of_this_month - timedelta(days=i*31)).replace(day=1)
        months.append({
            'year': m_date.year, 
            'month': m_date.month, 
            'label': m_date.strftime('%b %Y'),
            'nights': 0, 
            'revenue': 0.0,
        })

    total_revenue_accumulated = 0.0
    total_nights_counter = 0

    # 2. Elaborazione prenotazioni
    for b in bookings:
        try:
            # Pulizia sicura delle date
            check_in = clean_date(b.get('check_in'))
            check_out = clean_date(b.get('check_out'))
            
            if not check_in or not check_out:
                continue

            price_val = float(b.get('total_price', 0))
            total_revenue_accumulated += price_val

            # Calcolo notti
            delta = check_out - check_in
            n_nights = max(1, delta.days)
            nightly_rate = price_val / n_nights

            # 3. Distribuzione nei mesi
            # Cicliamo su ogni notte della prenotazione
            for n in range(n_nights):
                current_day = check_in + timedelta(days=n)
                for mm in months:
                    if current_day.year == mm['year'] and current_day.month == mm['month']:
                        mm['nights'] += 1
                        mm['revenue'] += round(nightly_rate, 2)
                        total_nights_counter += 1
                        
        except Exception as e:
            print(f"DEBUG ANALYTICS ERROR on booking: {e}")
            continue

    return {
        'monthly': months,
        'totals': {
            'revenue': round(total_revenue_accumulated, 2),
            'nights': total_nights_counter,
            'confirmed_bookings': sum(1 for b in bookings if b.get('status') == 'confirmed'),
            'pending_bookings': sum(1 for b in bookings if b.get('status') == 'pending'),
            'new_messages': await db.contact_messages.count_documents({'status': 'new'}),
        },
    }

# --- Resto del file (Settings, Gallery, etc.) rimasto invariato ---

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
