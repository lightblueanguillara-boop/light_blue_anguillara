# Testi dei messaggi — approvati il 2026-11-15

Stile deciso: **plurale informale** ("noi" verso l'ospite, a cui diamo del tu), firma **Light Blue Anguillara**.
Questi sono i testi **di partenza**: dopo la fase 1 il proprietario potrà cambiarli dal pannello
(Messaggi automatici) e tornare sempre a questo testo originale.

## Come leggere i testi

- Le parole tra graffe, come `{nome}`, vengono sostituite con i dati veri della prenotazione.
- **Le righe con un dato mancante spariscono da sole.** Per esempio `Wi-Fi: {wifi}` non compare se il Wi-Fi
  non è stato compilato nelle Impostazioni. Per questo ogni informazione facoltativa sta su una riga sua.
- Nelle email i link diventano pulsanti o link cliccabili; la grafica (logo, colori, indirizzo e CIN in fondo)
  la aggiunge il sito.
- Su WhatsApp: `*testo*` = **grassetto**, `_testo_` = *corsivo*. Emoji poche e sempre le stesse:
  🌊 saluto · 📅 date · 🕘 orari · 📍 indirizzo · 👉 link.
- Nessun prezzo, orario o regola inventati: arrivano tutti dalle Impostazioni. Quelli che oggi il sito non ha
  diventano **campi nuovi in Impostazioni** (segnati con ★ nella tabella sotto) e finché sono vuoti la riga non compare.

## Dati disponibili nei testi

| Variabile | Esempio | Da dove arriva |
|---|---|---|
| `{nome}` | Giulia | nome dell'ospite (solo il primo) |
| `{nome_completo}` | Giulia Russo | prenotazione |
| `{data_arrivo}` / `{data_partenza}` | venerdì 12 dicembre | prenotazione |
| `{notti}` | 3 notti | prenotazione |
| `{ospiti}` | 2 adulti, 1 bambino | prenotazione |
| `{totale}` / `{pagato}` / `{da_saldare}` | € 360,00 | prenotazione |
| `{scadenza_pagamento}` | 13 dicembre alle 18:30 | richiesta accettata |
| `{link_pagamento}` | link corto al pagamento Stripe | richiesta accettata |
| `{ore_risposta}` | 24 | Impostazioni (già esiste) |
| `{motivo}` | motivo del rifiuto | scritto dal proprietario quando rifiuta |
| `{nota_rimborso}` | frase sul rimborso (importo esatto, oppure nessun rimborso) | cancellazione |
| `{politica_cancellazione}` | regola della tariffa scelta | Impostazioni (già esiste) |
| `{indirizzo}` / `{telefono}` | — | Impostazioni (già esiste) |
| `{link_mappa}` | Google Maps sull'indirizzo | calcolato dall'indirizzo |
| `{tassa_soggiorno}` | — | Impostazioni (già esiste) |
| `{link_sito}` | lightblueanguillara.com | — |
| `{ora_checkin}` / `{ora_checkout}` | 15:00 / 10:00 | ★ Impostazioni |
| `{come_arrivare}` | — | ★ Impostazioni |
| `{istruzioni_ingresso}` | chiavi, codice, chi incontrare | ★ Impostazioni |
| `{parcheggio}` | — | ★ Impostazioni |
| `{wifi}` | nome rete e password | ★ Impostazioni |
| `{istruzioni_checkout}` | dove lasciare le chiavi, rifiuti… | ★ Impostazioni |
| `{modalita_saldo}` | es. in contanti all'arrivo | ★ Impostazioni |
| `{link_recensione}` | link Google / Airbnb | ★ Impostazioni |
| `{messaggio}` | testo scritto dall'ospite | modulo contatti |
| `{link_pannello}` | link alla scheda nel pannello | solo messaggi al proprietario |
| `{firma}` | Light Blue Anguillara | fisso (modificabile) |

---

## A. Messaggi all'ospite

### 1. Richiesta ricevuta — `richiesta_ricevuta`
**Quando:** l'ospite invia una richiesta dal sito. **Canali:** email (WhatsApp no: l'ospite ha appena scritto lui).

**Oggetto:** Abbiamo ricevuto la tua richiesta

```
Ciao {nome},

grazie per aver scelto la nostra casa! Abbiamo ricevuto la tua richiesta di soggiorno:

Arrivo: {data_arrivo}
Partenza: {data_partenza}
Notti: {notti}
Ospiti: {ospiti}
Totale: {totale}

Ti rispondiamo entro {ore_risposta} ore. Se la richiesta viene accettata ti mandiamo un link per pagare in modo sicuro: fino ad allora non ti addebitiamo nulla e le date restano riservate per te.

A presto,
{firma}
```

### 2. Richiesta accettata — `richiesta_accettata`
**Quando:** il proprietario accetta la richiesta. **Canali:** email + WhatsApp. Pulsante nell'email: **Paga e conferma**.

**Oggetto:** Richiesta accettata: completa la prenotazione

```
Ciao {nome},

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
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

abbiamo accettato la tua richiesta!

📅 *{data_arrivo} → {data_partenza}*
Totale: {totale}

Per confermare completa il pagamento entro *{scadenza_pagamento}*:
👉 {link_pagamento}

Trovi lo stesso link anche nella tua email.

_{firma}_
```

### 3. Richiesta non accettata — `richiesta_rifiutata`
**Quando:** il proprietario rifiuta (o annulla una richiesta accettata ma non ancora pagata). **Canali:** email + WhatsApp.

**Oggetto:** La tua richiesta di soggiorno

```
Ciao {nome},

grazie per averci scritto. Purtroppo non possiamo accettare la tua richiesta dal {data_arrivo} al {data_partenza}.

Motivo: {motivo}

Non ti abbiamo addebitato nulla. Se le tue date sono flessibili, dai un'occhiata alle altre disponibilità sul nostro sito: ci farebbe piacere ospitarti.
{link_sito}

A presto,
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

grazie per la tua richiesta. Purtroppo non possiamo accettarla per le date *{data_arrivo} → {data_partenza}*.
Motivo: {motivo}

Non ti abbiamo addebitato nulla. Se hai altre date in mente scrivici pure qui!

_{firma}_
```

### 4. Richiesta scaduta senza risposta — `richiesta_scaduta`
**Quando:** il proprietario non risponde entro il tempo previsto. **Canali:** email.

**Oggetto:** La tua richiesta di soggiorno

```
Ciao {nome},

ci dispiace: non siamo riusciti a rispondere in tempo alla tua richiesta dal {data_arrivo} al {data_partenza}, quindi è stata chiusa automaticamente.

Non ti abbiamo addebitato nulla. Se quelle date ti interessano ancora, scrivici o invia una nuova richiesta dal sito: faremo il possibile per rispondere subito.
{link_sito}

A presto,
{firma}
```

### 5. Pagamento non completato — `pagamento_scaduto`
**Quando:** la richiesta era stata accettata ma il pagamento non è arrivato in tempo. **Canali:** email.

**Oggetto:** Il tempo per il pagamento è scaduto

```
Ciao {nome},

il tempo per completare il pagamento del soggiorno dal {data_arrivo} al {data_partenza} è scaduto e le date sono tornate disponibili.

Non ti abbiamo addebitato nulla. Se vuoi ancora venire da noi, invia una nuova richiesta dal sito o rispondi a questa email.
{link_sito}

A presto,
{firma}
```

### 6. Prenotazione confermata — `prenotazione_confermata`
**Quando:** arriva il pagamento, oppure il proprietario crea una prenotazione manuale. **Canali:** email (con evento per il calendario) + WhatsApp.

**Oggetto:** Prenotazione confermata, ti aspettiamo!

```
Ciao {nome},

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
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

la tua prenotazione è confermata, ti aspettiamo!

📅 *{data_arrivo} → {data_partenza}*
🕘 Check-in dalle *{ora_checkin}*
📍 {indirizzo}

Qualche giorno prima dell'arrivo ti scriviamo con tutte le istruzioni.

_{firma}_
```

### 7. Prenotazione modificata — `prenotazione_modificata`
**Quando:** il proprietario cambia date, ospiti o tariffa. **Canali:** email (con evento del calendario aggiornato) + WhatsApp.

**Oggetto:** La tua prenotazione è stata aggiornata

```
Ciao {nome},

abbiamo aggiornato la tua prenotazione. Ecco i dati aggiornati:

Arrivo: {data_arrivo}
Check-in: dalle {ora_checkin}
Partenza: {data_partenza}
Check-out: entro le {ora_checkout}
Ospiti: {ospiti}
Totale: {totale}

Se qualcosa non ti torna rispondi a questa email o chiamaci al {telefono}.

A presto,
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

abbiamo aggiornato la tua prenotazione:

📅 *{data_arrivo} → {data_partenza}*
Ospiti: {ospiti}

Trovi tutti i dettagli nella tua email.

_{firma}_
```

### 8. Prenotazione cancellata — `prenotazione_cancellata`
**Quando:** il proprietario cancella (con o senza rimborso). **Canali:** email (l'evento sparisce dal calendario dell'ospite) + WhatsApp.

**Oggetto:** Prenotazione cancellata

```
Ciao {nome},

la tua prenotazione dal {data_arrivo} al {data_partenza} è stata cancellata.

{nota_rimborso}

Per qualsiasi domanda rispondi a questa email o chiamaci al {telefono}.

Speriamo di ospitarti in un'altra occasione,
{firma}
```

`{nota_rimborso}` la scrive il sito in base al caso:
- rimborso: *"Ti abbiamo rimborsato € 200,00 sul metodo di pagamento che hai usato: di solito compare sull'estratto conto entro 5-10 giorni lavorativi."*
- niente rimborso: *"In base alle condizioni della tariffa scelta non è previsto alcun rimborso."*
- non pagata o pagata fuori dal sito: la riga non compare.

**WhatsApp:**
```
Ciao {nome},

ti confermiamo che la prenotazione *{data_arrivo} → {data_partenza}* è stata cancellata.
{nota_rimborso}

Trovi i dettagli nella tua email.

_{firma}_
```

### 9. Promemoria del saldo — `promemoria_saldo`
**Quando:** solo per chi ha pagato un acconto, **7 giorni prima dell'arrivo alle 10:00** (modificabile). **Canali:** email + WhatsApp.

**Oggetto:** Promemoria del saldo

```
Ciao {nome},

manca una settimana al tuo arrivo! Ti ricordiamo che resta da saldare {da_saldare} per il soggiorno dal {data_arrivo} al {data_partenza}.

Modalità del saldo: {modalita_saldo}

Per qualsiasi domanda rispondi a questa email.

A presto,
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

manca una settimana al tuo arrivo!
Ti ricordiamo il saldo di *{da_saldare}*.
Modalità: {modalita_saldo}

_{firma}_
```

### 10. Istruzioni per l'arrivo — `istruzioni_arrivo`
**Quando:** **2 giorni prima dell'arrivo alle 10:00** (modificabile). **Canali:** email + WhatsApp.

**Oggetto:** Tutto pronto per il tuo arrivo

```
Ciao {nome},

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
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

manca poco al tuo arrivo!

📅 *{data_arrivo}*
🕘 Check-in dalle *{ora_checkin}*
📍 {indirizzo}
👉 {link_mappa}

Parcheggio: {parcheggio}
Come entrare: {istruzioni_ingresso}
Wi-Fi: {wifi}

Se arrivi più tardi del previsto scrivici qui.

_{firma}_
```

### 11. Promemoria check-out — `promemoria_checkout`
**Quando:** **la sera prima della partenza alle 18:00** (modificabile). **Canali:** solo WhatsApp (è un messaggio breve, l'email sarebbe di troppo).

```
Ciao {nome} 🌊

speriamo che il soggiorno ti stia piacendo!
Ti ricordiamo che domani il check-out è entro le *{ora_checkout}*.

{istruzioni_checkout}

Grazie e buon rientro!

_{firma}_
```

### 12. Grazie e recensione — `grazie_recensione`
**Quando:** **il giorno dopo la partenza alle 11:00** (modificabile). **Canali:** email + WhatsApp.

**Oggetto:** Grazie per il tuo soggiorno

```
Ciao {nome},

grazie per aver soggiornato da noi! Speriamo che il soggiorno ti sia piaciuto.

Se hai un minuto, una tua recensione ci aiuterebbe tantissimo:
{link_recensione}

Ti aspettiamo di nuovo sul lago!
{firma}
```

**WhatsApp:**
```
Ciao {nome} 🌊

grazie per aver soggiornato da noi, speriamo che il soggiorno ti sia piaciuto!

Se hai un minuto, una recensione ci aiuterebbe tantissimo:
👉 {link_recensione}

_{firma}_
```

### 13. Messaggio ricevuto — `contatto_ricevuto`
**Quando:** qualcuno scrive dal modulo contatti del sito. **Canali:** email.

**Oggetto:** Abbiamo ricevuto il tuo messaggio

```
Ciao {nome},

grazie per averci scritto! Abbiamo ricevuto il tuo messaggio e ti rispondiamo al più presto.

Il tuo messaggio:
{messaggio}

A presto,
{firma}
```

---

## B. Messaggi al proprietario (solo email; le notifiche sul telefono restano come sono)

Per ognuno, nelle Impostazioni si sceglie: email sì/no (le notifiche sul telefono hanno già i loro interruttori).

### 14. Nuova richiesta — `gestore_nuova_richiesta`
**Oggetto:** Nuova richiesta da {nome_completo}
```
Nuova richiesta di prenotazione dal sito.

Ospite: {nome_completo}
Arrivo: {data_arrivo}
Partenza: {data_partenza}
Ospiti: {ospiti}
Totale: {totale}

Hai {ore_risposta} ore per rispondere, poi la richiesta si chiude da sola:
{link_pannello}
```

### 15. Nuovo messaggio dal sito — `gestore_nuovo_messaggio`
**Oggetto:** Nuovo messaggio da {nome_completo}
```
{nome_completo} ti ha scritto dal sito:

{messaggio}

Rispondi dal pannello:
{link_pannello}
```

### 16. Pagamento ricevuto — `gestore_pagamento_ricevuto`
**Oggetto:** Prenotazione confermata: {nome_completo}
```
{nome_completo} ha pagato {pagato}: la prenotazione è confermata.

Arrivo: {data_arrivo}
Partenza: {data_partenza}
Ospiti: {ospiti}

{link_pannello}
```

Restano scritti nel codice, perché tecnici e non da personalizzare: gli avvisi di problemi (pagamento da verificare,
email non consegnata), il backup e la newsletter (che ha già il suo editor in Marketing).

---

## Da decidere insieme

1. **Nuovi campi in Impostazioni** (★): orari check-in/check-out, come arrivare, come entrare, parcheggio, Wi-Fi,
   istruzioni per il check-out, modalità del saldo, link per le recensioni. Li compila il proprietario; finché sono
   vuoti le righe non compaiono.
2. **Orari dei promemoria** proposti: saldo 7 giorni prima alle 10:00 · istruzioni 2 giorni prima alle 10:00 ·
   check-out la sera prima alle 18:00 · recensione il giorno dopo alle 11:00. Ognuno ha il suo interruttore.
3. **Evento del calendario** nell'email di conferma: dal check-in al check-out con gli orari (se compilati),
   altrimenti a giornata intera.
4. **Emoji del saluto**: 🌊 (lago) al posto del 🌿 della guida.
