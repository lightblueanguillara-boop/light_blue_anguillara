"""iCal helper used by both admin sync endpoint and background scheduler."""
import asyncio
import logging
import uuid
from datetime import datetime, date, timezone

import requests
from icalendar import Calendar

from db import db, get_settings
from models import Booking

async def ical_sync_run():
    s = await get_settings()
    urls = [
        ('airbnb', s.get('ical_airbnb_url', '')),
        ('booking', s.get('ical_booking_url', '')),
    ]
    imported = 0
    new_external_ids = []

    for src, url in urls:
        if not url:
            continue
        try:
            logging.info(f"Inizio sincronizzazione iCal per {src}...")
            r = await asyncio.to_thread(lambda u=url: requests.get(u, timeout=15))
            
            if r.status_code != 200:
                logging.error(f"Errore download iCal {src}: Status {r.status_code}")
                continue

            cal = Calendar.from_ical(r.content)
            for comp in cal.walk():
                if comp.name == 'VEVENT':
                    try:
                        dtstart = comp.get('dtstart')
                        dtend = comp.get('dtend')
                        
                        if not dtstart or not dtend:
                            continue

                        # Estrazione robusta della data
                        start = dtstart.dt
                        end = dtend.dt

                        if isinstance(start, datetime):
                            start = start.date()
                        if isinstance(end, datetime):
                            end = end.date()

                        uid = str(comp.get('uid', uuid.uuid4()))
                        booking_id = f"ext-{src}-{uid}"

                        # LOGICA DI PROTEZIONE PREZZO:
                        # $set aggiorna solo i campi dinamici (date/stato)
                        # $setOnInsert scrive i campi statici SOLO alla creazione del record
                        await db.bookings.update_one(
                            {'id': booking_id}, 
                            {
                                '$set': {
                                    'check_in': start.isoformat(),
                                    'check_out': end.isoformat(),
                                    'status': 'external',
                                    'source': src,
                                },
                                '$setOnInsert': {
                                    'id': booking_id,
                                    'guest_name': f"Ospite {src.capitalize()}",
                                    'guest_email': f"sync-{src}-{imported}@lightblue-anguillara.it",
                                    'total_price': 0,
                                    'deposit_amount': 0,
                                    'payment_status': 'unpaid',
                                    'created_at': datetime.now(timezone.utc).isoformat(),
                                }
                            }, 
                            upsert=True
                        )
                        
                        new_external_ids.append(booking_id)
                        imported += 1
                        
                    except Exception as vevent_error:
                        logging.warning(f"Salto un evento iCal per errore parsing: {vevent_error}")
                        continue

        except Exception as e:
            logging.exception(f'Errore critico durante sync iCal per {src}: {e}')

    # Pulizia: eliminiamo solo le vecchie prenotazioni esterne non più presenti nel file
    if imported > 0 or any(url for _, url in urls):
        await db.bookings.delete_many({
            'source': {'$in': ['airbnb', 'booking']}, 
            'status': 'external',
            'id': {'$nin': new_external_ids}
        })

    # Aggiornamento statistiche nel database
    await db.settings.update_one(
        {'id': 'global'},
        {'$set': {
            'last_ical_sync_at': datetime.now(timezone.utc).isoformat(),
            'last_ical_sync_count': imported,
        }},
    )
    
    logging.info(f"Sincronizzazione completata con successo: {imported} eventi.")
    return {'imported': imported, 'at': datetime.now(timezone.utc).isoformat()}
