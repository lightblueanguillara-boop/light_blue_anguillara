"""iCal helper used by both admin sync endpoint and background scheduler."""
import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

import requests
from icalendar import Calendar

from db import db, get_settings
from notifications import notify

async def ical_sync_run():
    s = await get_settings()
    urls = [
        ('airbnb', s.get('ical_airbnb_url', '')),
        ('booking', s.get('ical_booking_url', '')),
    ]
    imported = 0
    new_external_ids = []
    synced_sources = []   # piattaforme scaricate con successo
    errors = []
    inserted = []         # prenotazioni esterne comparse per la prima volta (per le notifiche)

    for src, url in urls:
        if not url:
            continue
        try:
            logging.info(f"Inizio sincronizzazione iCal per {src}...")
            r = await asyncio.to_thread(lambda u=url: requests.get(u, timeout=15))
            
            if r.status_code != 200:
                logging.error(f"Errore download iCal {src}: Status {r.status_code}")
                errors.append(f"{src}: HTTP {r.status_code}")
                continue

            cal = Calendar.from_ical(r.content)
            if not any(c.name == 'VEVENT' for c in cal.walk()) and await _has_future_external(src):
                # Calendario arrivato vuoto ma abbiamo ancora prenotazioni future da questa
                # piattaforma: quasi sempre è un errore temporaneo di Airbnb/Booking. Non
                # cancelliamo nulla (meglio una data bloccata in più che una doppia prenotazione):
                # lo trattiamo come un download fallito, così dopo un'ora arriva l'avviso.
                logging.warning(f"Calendario {src} vuoto ma con prenotazioni future: date mantenute")
                errors.append(f"{src}: calendario vuoto, date mantenute per sicurezza")
                continue
            synced_sources.append(src)
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
                        res = await db.bookings.update_one(
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
                        if res.upserted_id is not None:
                            inserted.append(booking_id)
                        imported += 1
                        
                    except Exception as vevent_error:
                        logging.warning(f"Salto un evento iCal per errore parsing: {vevent_error}")
                        continue

        except Exception as e:
            logging.exception(f'Errore critico durante sync iCal per {src}: {e}')
            errors.append(f"{src}: {type(e).__name__}")
            if src in synced_sources:
                synced_sources.remove(src)

    # Pulizia: eliminiamo le prenotazioni esterne non più presenti nel calendario,
    # SOLO per le piattaforme scaricate correttamente. Se un download fallisce
    # (Airbnb/Booking irraggiungibili) le date restano bloccate: meglio una data
    # bloccata in più che una doppia prenotazione.
    removed = []
    if synced_sources:
        stale_query = {'source': {'$in': synced_sources}, 'status': 'external', 'id': {'$nin': new_external_ids}}
        removed = await db.bookings.find(stale_query, {'_id': 0}).to_list(500)
        await db.bookings.delete_many(stale_query)

    await _notify_changes(s, urls, synced_sources, inserted, removed)

    # Aggiornamento statistiche nel database
    await db.settings.update_one(
        {'id': 'global'},
        {'$set': {
            'last_ical_sync_at': datetime.now(timezone.utc).isoformat(),
            'last_ical_sync_count': imported,
            'last_ical_sync_errors': errors,
        }},
    )
    
    logging.info(f"Sincronizzazione iCal completata: {imported} eventi, errori: {errors or 'nessuno'}")
    return {'imported': imported, 'errors': errors, 'at': datetime.now(timezone.utc).isoformat()}


async def _has_future_external(src: str) -> bool:
    """True se ci sono prenotazioni di questa piattaforma non ancora concluse."""
    today = date.today().isoformat()
    return await db.bookings.count_documents(
        {'source': src, 'status': 'external', 'check_out': {'$gt': today}}, limit=1) > 0


MAX_NOTIFY_PER_SYNC = 5
FAIL_NOTIFY_AFTER = timedelta(hours=1)


async def _notify_changes(s: dict, urls, synced_sources, inserted_ids, removed) -> None:
    """Notifiche 6, 7 e 8. La prima sincronizzazione di un calendario non genera avvisi
    (altrimenti arriverebbe una notifica per ogni prenotazione già esistente)."""
    today = date.today().isoformat()
    initialized = set(s.get('ical_initialized') or [])
    newly_initialized = [src for src in synced_sources if src not in initialized]

    for src in synced_sources:
        if src not in initialized:
            continue
        new_docs = await db.bookings.find({'id': {'$in': inserted_ids}, 'source': src, 'check_out': {'$gte': today}},
                                          {'_id': 0}).sort('check_in', 1).to_list(MAX_NOTIFY_PER_SYNC)
        for b in new_docs:
            await notify('external_new', booking=b)
        gone = sorted([b for b in removed if b.get('source') == src and b.get('check_out', '') >= today],
                      key=lambda b: b['check_in'])[:MAX_NOTIFY_PER_SYNC]
        for b in gone:
            await notify('external_removed', booking=b)

    # calendario illeggibile da almeno un'ora
    fail = dict(s.get('ical_fail') or {})
    now = datetime.now(timezone.utc)
    for src, url in urls:
        if not url or src in synced_sources:
            fail.pop(src, None)
            continue
        state = fail.get(src) or {'since': now.isoformat(), 'notified': False}
        if not state.get('notified') and now - datetime.fromisoformat(state['since']) >= FAIL_NOTIFY_AFTER:
            await notify('ical_failing', source=src, tag=src)
            state['notified'] = True
        fail[src] = state

    update = {'ical_fail': fail}
    if newly_initialized:
        update['ical_initialized'] = sorted(initialized | set(newly_initialized))
    await db.settings.update_one({'id': 'global'}, {'$set': update})
