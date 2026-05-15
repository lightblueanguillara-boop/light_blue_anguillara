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
    # Recupero TUTTE le prenotazioni
    bookings = await db.bookings.find({}, {'_id': 0}).to_list(10000)
    now = datetime.now(timezone.utc).date()

    months = []
    month_map = {}

    # Generazione dei 12 mesi
    base_date = now.replace(day=1)

    for i in range(11, -1, -1):
        target_date = (base_date - timedelta(days=i * 31)).replace(day=1)

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

    # SOLO prenotazioni confermate e completamente pagate
    valid_bookings = []

    for b in bookings:
        status = b.get('status')
        payment_status = b.get('payment_status')

        if status != 'confirmed':
            continue

        if payment_status != 'fully_paid':
            continue

        valid_bookings.append(b)

    # Elaborazione dati analytics
    for b in valid_bookings:
        try:
            # Prezzo totale
            price_val = float(b.get('total_price', 0))
            total_revenue_accumulated += price_val

            # Parsing sicuro date
            raw_in = b.get('check_in')

            if not raw_in:
                continue

            date_str_in = str(raw_in)[:10]
            dt_in = datetime.strptime(date_str_in, '%Y-%m-%d').date()

            # Mappa mese
            key = f"{dt_in.year}-{dt_in.month}"

            if key in month_map:
                idx = month_map[key]

                # Revenue mensile
                months[idx]['revenue'] = round(
                    months[idx]['revenue'] + price_val,
                    2
                )

                # Notti prenotate
                raw_out = b.get('check_out')

                if raw_out:
                    date_str_out = str(raw_out)[:10]
                    dt_out = datetime.strptime(
                        date_str_out,
                        '%Y-%m-%d'
                    ).date()

                    diff = (dt_out - dt_in).days

                    months[idx]['nights'] += max(0, diff)

        except Exception as e:
            logging.error(f"Errore calcolo booking: {e}")
            continue

    return {
        'monthly': months,
        'totals': {
            'revenue': round(total_revenue_accumulated, 2),

            'nights': sum(
                m['nights'] for m in months
            ),

            'confirmed_bookings': len(valid_bookings),

            'pending_bookings': sum(
                1 for b in bookings
                if b.get('status') == 'pending'
            ),

            'external_bookings': sum(
                1 for b in bookings
                if b.get('status') == 'external'
            ),

            'new_messages': await db.contact_messages.count_documents({
                'status': 'new'
            }),
        },
    }

# --- Gestione Settings ---
@router.get("/admin/settings")
async def get_admin_settings(admin=Depends(get_current_admin)):
    return await get_settings()

@router.put("/admin/settings")
async def update_admin_settings(
    updates: SettingsUpdate,
    admin=Depends(get_current_admin)
):
    patch = updates.model_dump(exclude_unset=True)

    if not patch:
        raise HTTPException(400, 'No fields to update')

    await db.settings.update_one(
        {'id': 'global'},
        {'$set': patch},
        upsert=True
    )

    return await get_settings()

# --- Sincronizzazione iCal ---
@router.post("/admin/ical/sync")
async def ical_sync(admin=Depends(get_current_admin)):
    return await ical_sync_run()

# --- Gestione Galleria Immagini ---
@router.get("/admin/gallery", response_model=List[GalleryImage])
async def list_gallery_images(admin=Depends(get_current_admin)):
    return await db.gallery.find(
        {},
        {'_id': 0}
    ).sort("order", 1).to_list(1000)

@router.post("/admin/gallery/upload", response_model=GalleryImage)
async def upload_gallery_image(
    file: UploadFile = File(...),
    category: str = Form("gallery"),
    caption: Optional[str] = Form(""),
    admin=Depends(get_current_admin)
):
    try:
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="light_blue_gallery"
        )

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
async def update_image_info(
    image_id: str,
    updates: GalleryImageUpdate,
    admin=Depends(get_current_admin)
):
    patch = updates.model_dump(exclude_unset=True)

    result = await db.gallery.find_one_and_update(
        {"id": image_id},
        {"$set": patch},
        projection={'_id': 0},
        return_document=True
    )

    if not result:
        raise HTTPException(404, "Immagine non trovata")

    return result

@router.delete("/admin/gallery/{image_id}")
async def delete_gallery_image(
    image_id: str,
    admin=Depends(get_current_admin)
):
    image = await db.gallery.find_one({"id": image_id})

    if not image:
        raise HTTPException(404, "Immagine non trovata")

    try:
        cloudinary.uploader.destroy(image['public_id'])

        await db.gallery.delete_one({"id": image_id})

        return {
            "status": "success",
            "message": "Eliminata correttamente"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
