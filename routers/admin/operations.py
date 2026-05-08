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
from pricing import daterange

router = APIRouter()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

@router.get("/admin/analytics")
async def analytics(admin=Depends(get_current_admin)):
    # Recuperiamo tutte le prenotazioni che hanno uno stato rilevante
    bookings = await db.bookings.find(
        {'status': {'$in': ['confirmed', 'pending']}}, {'_id': 0}
    ).to_list(10000)
    
    now = datetime.now(timezone.utc)
    months = []
    
    # Prepariamo la struttura per gli ultimi 12 mesi (per i grafici)
    for i in range(11, -1, -1):
        # Calcolo corretto del primo giorno del mese per evitare errori con mesi di durata diversa
        first_of_current = now.replace(day=1)
        m = (first_of_current - timedelta(days=i*30)).replace(day=1)
        months.append({
            'year': m.year, 
            'month': m.month, 
            'label': m.strftime('%b %Y'),
            'nights': 0, 
            'revenue': 0.0,
        })

    # Usiamo una variabile separata per il totale globale, 
    # così contiamo i soldi anche se la prenotazione è fuori dal range dei 12 mesi
    total_revenue_accumulated = 0.0

    for b in bookings:
        try:
            # Convertiamo le date da stringa a oggetto date
            check_in_dt = datetime.strptime(b['check_in'], '%Y-%m-%d').date()
            check_out_dt = datetime.strptime(b['check_out'], '%Y-%m-%d').date()
            
            nights_total = max(1, (check_out_dt - check_in_dt).days)
            
            # FIX: Convertiamo esplicitamente il prezzo in float (numero decimale)
            price_val = float(b.get('total_price', 0))
            nightly_rate = price_val / nights_total
            
            # Sommiamo al totale generale (quello che vedi nel quadratino in alto a sinistra)
            total_revenue_accumulated += price_val

            # Distribuiamo il valore nei singoli mesi per popolare il grafico
            for d in daterange(b['check_in'], b['check_out']):
                for mm in months:
                    if d.year == mm['year'] and d.month == mm['month']:
                        mm['nights'] += 1
                        mm['revenue'] += round(nightly_rate, 2)
        except Exception as e:
            # Se una prenotazione ha dati corrotti, la saltiamo senza bloccare tutto il server
            print(f"Errore processamento prenotazione: {e}")
            continue

    total_nights = sum(m['nights'] for m in months)
    confirmed_count = sum(1 for b in bookings if b['status'] == 'confirmed')
    pending_count = sum(1 for b in bookings if b['status'] == 'pending')
    messages_new = await db.contact_messages.count_documents({'status': 'new'})
    subs_count = await db.subscribers.count_documents({'consent': True})

    return {
        'monthly': months,
        'totals': {
            'revenue': round(total_revenue_accumulated, 2),
            'nights': total_nights,
            'confirmed_bookings': confirmed_count,
            'pending_bookings': pending_count,
            'new_messages': messages_new,
            'subscribers': subs_count,
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

# --- GESTIONE GALLERIA IMMAGINI (ADMIN) ---

@router.get("/admin/gallery", response_model=List[GalleryImage])
async def list_gallery_images(admin=Depends(get_current_admin)):
    images = await db.gallery.find({}, {'_id': 0}).sort("order", 1).to_list(1000)
    return images

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
        raise HTTPException(status_code=500, detail=f"Errore durante l'upload: {str(e)}")

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
        return {"status": "success", "message": "Immagine eliminata correttamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
