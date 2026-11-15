"""Testi di partenza dei messaggi (approvati dal committente il 2026-11-15, vedi docs/TESTI_MESSAGGI.md).

Ogni voce è un modello per (chiave, canale). All'avvio del server i modelli mancanti vengono creati nel
database; quelli modificati dal proprietario dal pannello non vengono mai sovrascritti (vedi messaging.seed).
"""

# Variabili disponibili per i messaggi legati a una prenotazione
BOOKING_VARS = [
    'nome', 'nome_completo', 'data_arrivo', 'data_partenza', 'notti', 'ospiti', 'totale', 'pagato', 'da_saldare',
    'modalita_saldo', 'politica_cancellazione', 'tassa_soggiorno', 'indirizzo', 'link_mappa', 'telefono',
    'ora_checkin', 'ora_checkout', 'come_arrivare', 'parcheggio', 'istruzioni_ingresso', 'wifi',
    'istruzioni_checkout', 'link_recensione', 'ore_risposta', 'link_sito', 'link_pannello', 'firma',
]
CONTACT_VARS = ['nome', 'nome_completo', 'messaggio', 'telefono', 'link_sito', 'link_pannello', 'firma']

EXTRA_VARS = {
    'richiesta_accettata': ['scadenza_pagamento', 'link_pagamento'],
    'richiesta_rifiutata': ['motivo'],
    'prenotazione_cancellata': ['nota_rimborso'],
}

# Pulsanti nelle email: (etichetta, variabile con il link, principale?)
BUTTONS = {
    'richiesta_accettata': [('Paga e conferma', 'link_pagamento', True)],
    'istruzioni_arrivo': [('Apri la mappa', 'link_mappa', False)],
    'grazie_recensione': [('Lascia una recensione', 'link_recensione', True)],
    'gestore_nuova_richiesta': [('Apri nel pannello', 'link_pannello', True)],
    'gestore_nuovo_messaggio': [('Rispondi dal pannello', 'link_pannello', True)],
    'gestore_pagamento_ricevuto': [('Apri nel pannello', 'link_pannello', True)],
}

# Email con l'evento per il calendario (.ics + pulsanti Google / Apple-Outlook)
CALENDAR_KEYS = {'prenotazione_confermata': 'publish', 'prenotazione_modificata': 'publish',
                 'prenotazione_cancellata': 'cancel'}

# Ordine e descrizione in pannello
INFO = {
    'richiesta_ricevuta': 'Quando un ospite invia una richiesta dal sito',
    'richiesta_accettata': 'Quando accetti una richiesta (contiene il link di pagamento)',
    'richiesta_rifiutata': 'Quando rifiuti una richiesta, o annulli una richiesta accettata non ancora pagata',
    'richiesta_scaduta': 'Quando una richiesta si chiude perché non hai risposto in tempo',
    'pagamento_scaduto': "Quando l'ospite non paga entro la scadenza del link",
    'prenotazione_confermata': 'Quando arriva il pagamento o crei una prenotazione manuale',
    'prenotazione_modificata': "Quando cambi date, ospiti o tariffa di una prenotazione",
    'prenotazione_cancellata': 'Quando cancelli una prenotazione (con o senza rimborso)',
    'promemoria_saldo': "Promemoria automatico del saldo, solo per chi ha pagato un acconto",
    'istruzioni_arrivo': "Automatico, qualche giorno prima dell'arrivo",
    'promemoria_checkout': 'Automatico, la sera prima della partenza',
    'grazie_recensione': 'Automatico, il giorno dopo la partenza',
    'contatto_ricevuto': 'Quando qualcuno scrive dal modulo contatti del sito',
    'gestore_nuova_richiesta': 'A te, quando arriva una nuova richiesta',
    'gestore_nuovo_messaggio': 'A te, quando qualcuno scrive dal sito',
    'gestore_pagamento_ricevuto': 'A te, quando un ospite paga e la prenotazione si conferma',
}

TITLES = {
    'richiesta_ricevuta': 'Richiesta ricevuta', 'richiesta_accettata': 'Richiesta accettata',
    'richiesta_rifiutata': 'Richiesta non accettata', 'richiesta_scaduta': 'Richiesta scaduta senza risposta',
    'pagamento_scaduto': 'Pagamento non completato', 'prenotazione_confermata': 'Prenotazione confermata',
    'prenotazione_modificata': 'Prenotazione modificata', 'prenotazione_cancellata': 'Prenotazione cancellata',
    'promemoria_saldo': 'Promemoria del saldo', 'istruzioni_arrivo': "Istruzioni per l'arrivo",
    'promemoria_checkout': 'Promemoria check-out', 'grazie_recensione': 'Grazie e recensione',
    'contatto_ricevuto': 'Messaggio ricevuto', 'gestore_nuova_richiesta': 'Nuova richiesta (a te)',
    'gestore_nuovo_messaggio': 'Nuovo messaggio dal sito (a te)',
    'gestore_pagamento_ricevuto': 'Pagamento ricevuto (a te)',
}

GESTORE_KEYS = {'gestore_nuova_richiesta', 'gestore_nuovo_messaggio', 'gestore_pagamento_ricevuto'}
CONTACT_KEYS = {'contatto_ricevuto', 'gestore_nuovo_messaggio'}


def allowed_vars(key: str) -> list:
    base = CONTACT_VARS if key in CONTACT_KEYS else BOOKING_VARS
    return base + EXTRA_VARS.get(key, [])


SEED = [
    # ------------------------------------------------------------------ 1
    dict(key='richiesta_ricevuta', channel='email', subject='Abbiamo ricevuto la tua richiesta', body="""Ciao {nome},

grazie per aver scelto la nostra casa! Abbiamo ricevuto la tua richiesta di soggiorno:

Arrivo: {data_arrivo}
Partenza: {data_partenza}
Notti: {notti}
Ospiti: {ospiti}
Totale: {totale}

Ti rispondiamo entro {ore_risposta} ore. Se la richiesta viene accettata ti mandiamo un link per pagare in modo sicuro: fino ad allora non ti addebitiamo nulla e le date restano riservate per te.

A presto,
{firma}"""),
    # ------------------------------------------------------------------ 2
    dict(key='richiesta_accettata', channel='email', subject='Richiesta accettata: completa la prenotazione', body="""Ciao {nome},

buone notizie: abbiamo accettato la tua richiesta!

Per confermare la prenotazione completa il pagamento entro {scadenza_pagamento}:
{link_pagamento}

Arrivo: {data_arrivo}
Partenza: {data_partenza}
Ospiti: {ospiti}
Totale: {totale}

Se il pagamento non arriva entro la scadenza le date tornano disponibili per altri ospiti.

{politica_cancellazione}

A presto,
{firma}"""),
    dict(key='richiesta_accettata', channel='whatsapp', body="""Ciao {nome} 🌊

abbiamo accettato la tua richiesta!

📅 *{data_arrivo} → {data_partenza}*
Totale: {totale}

Per confermare completa il pagamento entro *{scadenza_pagamento}*:
👉 {link_pagamento}

Trovi lo stesso link anche nella tua email.

_{firma}_"""),
    # ------------------------------------------------------------------ 3
    dict(key='richiesta_rifiutata', channel='email', subject='La tua richiesta di soggiorno', body="""Ciao {nome},

grazie per averci scritto. Purtroppo non possiamo accettare la tua richiesta dal {data_arrivo} al {data_partenza}.

Motivo: {motivo}

Non ti abbiamo addebitato nulla. Se le tue date sono flessibili, dai un'occhiata alle altre disponibilità sul nostro sito: ci farebbe piacere ospitarti.
{link_sito}

A presto,
{firma}"""),
    dict(key='richiesta_rifiutata', channel='whatsapp', body="""Ciao {nome} 🌊

grazie per la tua richiesta. Purtroppo non possiamo accettarla per le date *{data_arrivo} → {data_partenza}*.
Motivo: {motivo}

Non ti abbiamo addebitato nulla. Se hai altre date in mente scrivici pure qui!

_{firma}_"""),
    # ------------------------------------------------------------------ 4
    dict(key='richiesta_scaduta', channel='email', subject='La tua richiesta di soggiorno', body="""Ciao {nome},

ci dispiace: non siamo riusciti a rispondere in tempo alla tua richiesta dal {data_arrivo} al {data_partenza}, quindi è stata chiusa automaticamente.

Non ti abbiamo addebitato nulla. Se quelle date ti interessano ancora, scrivici o invia una nuova richiesta dal sito: faremo il possibile per rispondere subito.
{link_sito}

A presto,
{firma}"""),
    # ------------------------------------------------------------------ 5
    dict(key='pagamento_scaduto', channel='email', subject='Il tempo per il pagamento è scaduto', body="""Ciao {nome},

il tempo per completare il pagamento del soggiorno dal {data_arrivo} al {data_partenza} è scaduto e le date sono tornate disponibili.

Non ti abbiamo addebitato nulla. Se vuoi ancora venire da noi, invia una nuova richiesta dal sito o rispondi a questa email.
{link_sito}

A presto,
{firma}"""),
    # ------------------------------------------------------------------ 6
    dict(key='prenotazione_confermata', channel='email', subject='Prenotazione confermata, ti aspettiamo!', body="""Ciao {nome},

la tua prenotazione è confermata: ti aspettiamo!

Arrivo: {data_arrivo}
Check-in: dalle {ora_checkin}
Partenza: {data_partenza}
Check-out: entro le {ora_checkout}
Ospiti: {ospiti}
Totale: {totale}
Pagato: {pagato}
Da saldare: {da_saldare}
Modalità del saldo: {modalita_saldo}

Indirizzo: {indirizzo}
{link_mappa}

Qualche giorno prima dell'arrivo ti mandiamo le istruzioni per raggiungerci ed entrare in casa.

{politica_cancellazione}
{tassa_soggiorno}

Per qualsiasi domanda rispondi pure a questa email o chiamaci al {telefono}.

A presto,
{firma}"""),
    dict(key='prenotazione_confermata', channel='whatsapp', body="""Ciao {nome} 🌊

la tua prenotazione è confermata, ti aspettiamo!

📅 *{data_arrivo} → {data_partenza}*
🕘 Check-in dalle *{ora_checkin}*
📍 {indirizzo}

Qualche giorno prima dell'arrivo ti scriviamo con tutte le istruzioni.

_{firma}_"""),
    # ------------------------------------------------------------------ 7
    dict(key='prenotazione_modificata', channel='email', subject='La tua prenotazione è stata aggiornata', body="""Ciao {nome},

abbiamo aggiornato la tua prenotazione. Ecco i dati aggiornati:

Arrivo: {data_arrivo}
Check-in: dalle {ora_checkin}
Partenza: {data_partenza}
Check-out: entro le {ora_checkout}
Ospiti: {ospiti}
Totale: {totale}

Se qualcosa non ti torna rispondi a questa email o chiamaci al {telefono}.

A presto,
{firma}"""),
    dict(key='prenotazione_modificata', channel='whatsapp', body="""Ciao {nome} 🌊

abbiamo aggiornato la tua prenotazione:

📅 *{data_arrivo} → {data_partenza}*
Ospiti: {ospiti}

Trovi tutti i dettagli nella tua email.

_{firma}_"""),
    # ------------------------------------------------------------------ 8
    dict(key='prenotazione_cancellata', channel='email', subject='Prenotazione cancellata', body="""Ciao {nome},

la tua prenotazione dal {data_arrivo} al {data_partenza} è stata cancellata.

{nota_rimborso}

Per qualsiasi domanda rispondi a questa email o chiamaci al {telefono}.

Speriamo di ospitarti in un'altra occasione,
{firma}"""),
    dict(key='prenotazione_cancellata', channel='whatsapp', body="""Ciao {nome},

ti confermiamo che la prenotazione *{data_arrivo} → {data_partenza}* è stata cancellata.
{nota_rimborso}

Trovi i dettagli nella tua email.

_{firma}_"""),
    # ------------------------------------------------------------------ 9
    dict(key='promemoria_saldo', channel='email', subject='Promemoria del saldo', body="""Ciao {nome},

manca una settimana al tuo arrivo! Ti ricordiamo che resta da saldare {da_saldare} per il soggiorno dal {data_arrivo} al {data_partenza}.

Modalità del saldo: {modalita_saldo}

Per qualsiasi domanda rispondi a questa email.

A presto,
{firma}"""),
    dict(key='promemoria_saldo', channel='whatsapp', body="""Ciao {nome} 🌊

manca una settimana al tuo arrivo!
Ti ricordiamo il saldo di *{da_saldare}*.
Modalità: {modalita_saldo}

_{firma}_"""),
    # ------------------------------------------------------------------ 10
    dict(key='istruzioni_arrivo', channel='email', subject='Tutto pronto per il tuo arrivo', body="""Ciao {nome},

manca poco al tuo soggiorno! Ecco tutto quello che ti serve.

Arrivo: {data_arrivo}
Check-in: dalle {ora_checkin}
Partenza: {data_partenza}
Check-out: entro le {ora_checkout}

Indirizzo: {indirizzo}
{link_mappa}
Come arrivare: {come_arrivare}
Parcheggio: {parcheggio}
Come entrare: {istruzioni_ingresso}
Wi-Fi: {wifi}

{tassa_soggiorno}

Se pensi di arrivare più tardi del previsto avvisaci al {telefono}.

Buon viaggio,
{firma}"""),
    dict(key='istruzioni_arrivo', channel='whatsapp', body="""Ciao {nome} 🌊

manca poco al tuo arrivo!

📅 *{data_arrivo}*
🕘 Check-in dalle *{ora_checkin}*
📍 {indirizzo}
👉 {link_mappa}

Parcheggio: {parcheggio}
Come entrare: {istruzioni_ingresso}
Wi-Fi: {wifi}

Se arrivi più tardi del previsto scrivici qui.

_{firma}_"""),
    # ------------------------------------------------------------------ 11
    dict(key='promemoria_checkout', channel='whatsapp', body="""Ciao {nome} 🌊

speriamo che il soggiorno ti stia piacendo!
Ti ricordiamo che domani il check-out è entro le *{ora_checkout}*.

{istruzioni_checkout}

Grazie e buon rientro!

_{firma}_"""),
    # ------------------------------------------------------------------ 12
    dict(key='grazie_recensione', channel='email', subject='Grazie per il tuo soggiorno', body="""Ciao {nome},

grazie per aver soggiornato da noi! Speriamo che il soggiorno ti sia piaciuto.

Se hai un minuto, una tua recensione ci aiuterebbe tantissimo:
{link_recensione}

Ti aspettiamo di nuovo sul lago!
{firma}"""),
    dict(key='grazie_recensione', channel='whatsapp', body="""Ciao {nome} 🌊

grazie per aver soggiornato da noi, speriamo che il soggiorno ti sia piaciuto!

Se hai un minuto, una recensione ci aiuterebbe tantissimo:
👉 {link_recensione}

_{firma}_"""),
    # ------------------------------------------------------------------ 13
    dict(key='contatto_ricevuto', channel='email', subject='Abbiamo ricevuto il tuo messaggio', body="""Ciao {nome},

grazie per averci scritto! Abbiamo ricevuto il tuo messaggio e ti rispondiamo al più presto.

Il tuo messaggio:
{messaggio}

A presto,
{firma}"""),
    # ------------------------------------------------------------------ 14-16 (al proprietario)
    dict(key='gestore_nuova_richiesta', channel='email', subject='Nuova richiesta da {nome_completo}', body="""Nuova richiesta di prenotazione dal sito.

Ospite: {nome_completo}
Arrivo: {data_arrivo}
Partenza: {data_partenza}
Ospiti: {ospiti}
Totale: {totale}

Hai {ore_risposta} ore per rispondere, poi la richiesta si chiude da sola:
{link_pannello}"""),
    dict(key='gestore_nuovo_messaggio', channel='email', subject='Nuovo messaggio da {nome_completo}', body="""{nome_completo} ti ha scritto dal sito:

{messaggio}

Rispondi dal pannello:
{link_pannello}"""),
    dict(key='gestore_pagamento_ricevuto', channel='email', subject='Prenotazione confermata: {nome_completo}', body="""{nome_completo} ha pagato {pagato}: la prenotazione è confermata.

Arrivo: {data_arrivo}
Partenza: {data_partenza}
Ospiti: {ospiti}

{link_pannello}"""),
]

KEYS = list(dict.fromkeys(t['key'] for t in SEED))
