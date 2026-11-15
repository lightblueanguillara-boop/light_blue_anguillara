# Guida per Claude Code: sistema di email e WhatsApp da replicare

> **A chi è rivolta.** A Claude Code che lavora sul sito di una **casa vacanze**. Il committente vuole che email e
> WhatsApp funzionino come nel suo altro progetto (sito per professioniste con appuntamenti, FastAPI + MongoDB + React).
> Qui trovi la **logica di funzionamento e di utilizzo**, già collaudata, da adattare al tuo progetto.
>
> **Cosa NON copiare:** i testi dei messaggi. Quelli dell'altro sito parlano di visite e pazienti e qui non c'entrano.
> I modelli dei messaggi della casa vacanze li scrivi tu, partendo dagli eventi del tuo sito (richiesta di
> prenotazione, conferma, caparra, istruzioni per l'arrivo, promemoria, check-out, ecc.) e facendoli approvare al
> committente. **Non inventare** prezzi, orari, regole della casa o indirizzi: dove manca un dato metti
> `[DA COMPLETARE]` e chiedilo.
>
> **Come procedere:** prima leggi il codice del tuo progetto e confrontalo con questa guida. Poi presenta al committente
> un piano a fasi (cosa c'è già, cosa manca, in che ordine lo fai) e **aspetta il via** prima di cominciare. Il
> committente vuole riepiloghi in italiano semplice e il via esplicito tra una fase e l'altra.

---

## 1. L'idea in breve

1. **Un solo punto d'ingresso per ogni messaggio all'ospite**: `invia(chiave, email, telefono, variabili, prenotazione_id)`.
   Con una chiamata parte l'email e, se esiste un modello WhatsApp con la stessa chiave, il WhatsApp va in coda.
2. **Modelli nel database**, modificabili dalla dashboard: uno per evento e per canale (email / whatsapp), con le
   `{variabili}`.
3. **Email con Resend** (semplice API REST, niente SDK). **Senza chiave non si perde niente**: ogni email finisce
   comunque in un registro (`email_outbox`) con lo stato e il motivo.
4. **WhatsApp «al tocco»**, senza API di Meta e senza costi. Il sito prepara il messaggio già scritto e lo mette in
   una **coda in dashboard**. Il gestore tocca «Apri», WhatsApp si apre con il testo pronto, preme invio dal **suo**
   numero e poi segna «Inviato».
5. **Link sempre corti** in ogni messaggio (`<sito>/l/<codice>`), mai il link lungo con il token.
6. **Invii automatici** (promemoria ecc.) fatti da **job pianificati e idempotenti**, lanciati da un cron ogni 5 minuti:
   ogni messaggio parte **una volta sola**, anche se il cron gira due volte insieme.
7. **Mai bloccante**: un errore di email o WhatsApp non deve mai far fallire una prenotazione. Si registra e si va avanti.

---

## 2. Variabili d'ambiente (mai chiavi nel codice)

| Variabile | A cosa serve |
|---|---|
| `RESEND_API_KEY` | chiave di Resend. Se manca, l'invio è **spento con avviso chiaro in dashboard** e le email restano nel registro |
| `EMAIL_FROM_ADDRESS` | mittente, su un dominio verificato in Resend (es. `prenotazioni@dominio.it`) |
| `EMAIL_FROM_NAME_DEFAULT` | nome del mittente se non è impostato in dashboard |
| `EMAIL_SOLO_A` | *(solo prove)* elenco di indirizzi ammessi, separati da virgole. Tutti gli altri non partono (motivo `solo_prove`). Ignora il `+`: `nome+ospite1@…` vale come `nome@…` |
| `EMAIL_LIMITE_GIORNO` | *(solo prove)* massimo di email vere al giorno (motivo `limite`) |
| `RESEND_WEBHOOK_SECRET` | chiave `whsec_…` del webhook di Resend, per sapere se l'email è stata consegnata o è tornata indietro |
| `PUBLIC_SITE_URL` | base dei link corti e dei link assoluti nelle email |
| `CRON_SECRET` | protegge l'endpoint dei job |

Regola del committente: **le chiavi si chiedono solo quando una prova reale le richiede**, preferendo quelle di test.
Vanno solo nel file `.env`: mai nel codice, nei test, nei commit o in chat. Account e carte dei servizi (Resend,
dominio, hosting) sono **intestati al cliente finale**, non al committente.

---

## 3. Modelli dei messaggi

Collezione `modelli_messaggio`, un documento per **(chiave, canale)**:

```python
class ModelloMessaggio:
    chiave: str               # es. "conferma_prenotazione", "promemoria_arrivo"
    canale: "email" | "whatsapp"
    quando: str | None        # descrizione leggibile in dashboard: "Quando confermi una prenotazione"
    oggetto: str | None       # solo email
    corpo: str                # testo con {variabili}
    destinatario: "ospite" | "gestore"
    attivo: bool = True
    oggetto_originale / corpo_originale   # testo di partenza, per «Ripristina il testo originale»
```

- **Seed idempotente** da un file (nell'altro sito un `.md` / `.json` con tutti i testi): crea i modelli mancanti,
  non sovrascrive quelli modificati dal gestore.
- **Stessa chiave sui due canali** = la stessa comunicazione via email e via WhatsApp. `invia()` cerca il modello
  WhatsApp con la stessa chiave: se c'è ed è attivo, mette il messaggio in coda.
- **Dashboard → Messaggi automatici**: elenco dei modelli, modifica di oggetto e corpo, interruttore attivo/spento,
  «Ripristina il testo originale», **anteprima con dati d'esempio** e **«Invia una prova a me»** (l'email arriva
  all'indirizzo del gestore; per WhatsApp si apre un link verso il proprio numero). Accettare solo le variabili
  documentate per quel modello e mostrare l'elenco delle variabili accanto al campo.

### Il render (importante, copialo così)

`{variabile}` → valore. Le **righe con variabili vuote si tolgono da sole**, così lo stesso modello va bene in casi
diversi (es. «Codice del portone: {codice}» sparisce se il codice non c'è):

```python
_VAR = re.compile(r"\{([a-z_]+)\}")
_ETICHETTA = re.compile(r"^[^{}:\n]{1,40}:\s*(\{[a-z_]+\}\s*)+$")   # «Etichetta: {var}»

def render(template: str, variables: dict) -> str:
    out = []
    for line in template.split("\n"):
        names = _VAR.findall(line)
        vuote = names and all(not variables.get(n) for n in names)
        if vuote and not _VAR.sub("", line).strip(" .,:;-–_*~"):
            continue      # riga fatta solo di variabili vuote (anche dentro _corsivo_ o *grassetto*)
        if vuote and _ETICHETTA.match(line):
            continue      # «Etichetta: {variabile}» con la variabile vuota
        out.append(_VAR.sub(lambda m: str(variables.get(m.group(1)) or ""), line))
    return re.sub(r"[ \t]{2,}", " ", "\n".join(out)).strip()
```

Le variabili si preparano in **una funzione unica per ogni oggetto** (es. `variabili_prenotazione(db, pren)` →
`nome`, `data_arrivo`, `data_partenza`, `ospiti`, `alloggio`, `totale`, `link_prenotazione`, `link_mappa`…). Così ogni
modello e ogni job usa gli stessi nomi.

---

## 4. Email

### 4.1 Invio (`services/email.py`)

```python
async def send_email(db, to, subject, text, chiave_modello=None, extra=None, *, prenotazione_id=None) -> EmailOutbox:
    profilo = await profilo_del_sito(db)                     # nome, logo, recapiti: dal DB, mai dal codice
    html, testo = componi(profilo, subject, text, extra)    # layout curato + versione solo testo
    html, testo = await link_corti.accorcia_html(db, html), await link_corti.accorcia_testo(db, testo)
    record = EmailOutbox(a=to, reply_to=profilo.email_risposte, mittente_nome=..., oggetto=subject,
                         testo=testo, html=html, chiave_modello=chiave_modello,
                         allegati=[a.nome for a in extra.allegati], prenotazione_id=prenotazione_id)
    return await _consegna(db, record, extra.allegati)
```

`_consegna`:
1. Controlla `EMAIL_SOLO_A` → altrimenti `motivo="solo_prove"`.
2. Controlla `EMAIL_LIMITE_GIORNO` (conta le `inviata` da mezzanotte, fuso italiano) → `motivo="limite"`.
3. Se mancano `RESEND_API_KEY` o `EMAIL_FROM_ADDRESS` → `motivo="non_attivo"`.
4. Altrimenti `POST https://api.resend.com/emails` con `httpx` (timeout 15 s), header
   `Authorization: Bearer <chiave>` e payload `from` (`"Nome <indirizzo>"`), `to`, `subject`, `text`, `html`,
   `reply_to`, `attachments` (`filename`, `content` in base64, `content_type`).
   Risposta < 300 → `stato="inviata"`, `inviata_il`, `provider_id = resp.json()["id"]`. Altrimenti `stato="errore"` e
   `errore="Resend <codice>"` (mai il contenuto dell'email nei log).
5. **Inserisce sempre** il record in `email_outbox`, qualunque sia l'esito.

```python
class EmailOutbox:
    a, reply_to, mittente_nome, oggetto, testo, html, chiave_modello
    allegati: list[str]                  # solo i nomi dei file
    stato: "non_inviata" | "inviata" | "errore"
    motivo: "non_attivo" | "solo_prove" | "limite" | "errore" | None
    errore: str | None
    provider_id: str | None              # id di Resend, per il webhook
    consegna: None | "consegnata" | "in_ritardo" | "tornata_indietro" | "segnalata_spam"
    consegna_il, consegna_dettaglio
    prenotazione_id / richiesta_id       # a cosa si riferisce: serve all'icona di stato in dashboard
```

`send_template_email(db, chiave, to, variabili, extra, **collegamenti)` carica il modello email attivo, fa il render di
oggetto e corpo e chiama `send_email`. Se il modello manca o è spento, scrive un warning e non manda niente.

### 4.2 Aspetto (`services/email_layout.py`)

- HTML semplice con **stili in linea** (i programmi di posta ignorano i fogli di stile), tabella centrata larga al
  massimo 560 px: logo **PNG** (gli SVG non si vedono), nome del sito, carta bianca con il testo, piè di pagina con i
  dati presi dal profilo (nome, P.IVA, telefono, email). **Colori e caratteri dal design system del sito**, non
  scritti a mano.
- Il corpo del modello è testo semplice: i paragrafi si separano con una riga vuota e gli a capo diventano `<br>`.
  **Prima si fa l'escape HTML, poi i link diventano cliccabili.**
- Riga di anteprima nascosta (preheader) = prima riga del testo.
- `Extra` = `pulsanti` (etichetta, url, primario/secondario) + `allegati` (nome, bytes, mime). Nella versione solo testo
  i pulsanti diventano «Etichetta: url».

### 4.3 Evento del calendario (`services/calendario_ics.py`)

Per **conferma e modifica della prenotazione** si allega `prenotazione.ics` e si aggiungono i pulsanti «Aggiungi a
Google Calendar» / «Apple / Outlook». Regole RFC 5545 da rispettare:
- `UID` stabile (`<id>@<dominio>`), così chi riceve una modifica **aggiorna** l'evento invece di duplicarlo;
  `SEQUENCE` più alta a ogni modifica; `METHOD:CANCEL` + `STATUS:CANCELLED` per l'annullamento.
- Date in UTC (`%Y%m%dT%H%M%SZ`), escape di `\ ; ,` e degli a capo, righe piegate a 75 ottetti, fine riga `\r\n`.
- Link Google: `https://calendar.google.com/calendar/render?action=TEMPLATE&text=…&dates=inizio/fine&location=…&details=…`.
- Per una casa vacanze l'evento può andare dall'ora del check-in a quella del check-out, ma decidilo con il committente.

### 4.4 Esito della consegna (webhook di Resend)

`POST /api/resend/webhook`:
- Firma **Svix**: HMAC-SHA256 di `"{svix-id}.{svix-timestamp}.{corpo}"` con la chiave decodificata in base64 (togli
  `whsec_`), confrontata con ognuna delle firme `v1,…` dell'header `svix-signature` usando `hmac.compare_digest`.
  Tolleranza di 5 minuti sul timestamp. Senza `RESEND_WEBHOOK_SECRET` risponde 503 e la dashboard lo segnala.
- Eventi: `email.delivered` → consegnata, `email.delivery_delayed` → in_ritardo, `email.bounced` → tornata_indietro (si
  salva solo il tipo di rimbalzo), `email.complained` → segnalata_spam. Cerca per `provider_id`.
- **Un esito non torna indietro** (gli eventi arrivano in disordine): «in ritardo» non sovrascrive «consegnata»;
  «tornata indietro» e «spam» sì. Si fa con un filtro sull'`update_one`:
  ```python
  SOVRASCRIVIBILI = {"consegnata": {None, "in_ritardo"}, "in_ritardo": {None, "in_ritardo"},
                     "tornata_indietro": {None, "in_ritardo", "consegnata"}, "segnalata_spam": {None, "in_ritardo", "consegnata"}}
  ```
- **Mai** tracciamento di aperture e clic (niente `email.opened` / `email.clicked`).
- Configurazione nel pannello di Resend: Webhooks → `https://<dominio>/api/resend/webhook`, i quattro eventi sopra,
  e la Signing secret nella variabile del backend.

### 4.5 Stato dell'email in dashboard

Accanto a ogni prenotazione, richiesta e voce WhatsApp si vede l'**ultima email di quel tipo** (una per chiave): «Email
inviata alle 17:42», «Email consegnata alle…», «Non inviata: servizio email non attivo / solo prove / limite / errore»,
«Tornata indietro: controlla l'indirizzo» (in rosso), «In ritardo, il servizio riprova da solo».
**«Rimanda l'email»** (`POST /admin/email/{id}/rimanda`) rispedisce la stessa email come **voce nuova** del registro,
ricreando l'eventuale `.ics`. È rifiutata se esiste già un'email più recente dello stesso tipo o se l'email parla di
date ormai cambiate o passate (la prenotazione non è più confermata e futura, o è stata modificata dopo).

Pagina **Diagnostica**: elenco di `email_outbox` (destinatario, oggetto, stato, motivo, esito, allegati), per vedere
tutto anche senza Resend.

---

## 5. WhatsApp «al tocco»

### 5.1 Perché così

Niente API di Meta: costerebbe, richiederebbe un account Business verificato e modelli approvati. Il messaggio parte
dal numero del gestore, con il testo già pronto. Predisponi comunque un'interfaccia `MessageSender` con l'adattatore
`AlTocco` attivo e dei segnaposto spenti (`meta_business`, `sms_gateway`) che ripiegano sulla coda, così un giorno si
può passare all'invio automatico senza toccare il resto.

### 5.2 La coda (`coda_whatsapp`)

```python
class MessaggioWhatsApp:
    tipo: str               # chiave del modello
    telefono: str           # solo cifre, internazionale (es. 393331234567)
    testo: str              # già con il render fatto e i link corti
    nome: str | None        # a chi va, per l'elenco
    prenotazione_id, ospite_id
    per_data: str | None    # YYYY-MM-DD di riferimento (es. arrivo di domani), per l'ordinamento
    stato: "da_inviare" | "inviato" | "annullato"
    inviato_il: datetime | None
```

```python
async def accoda(db, chiave, telefono, variabili, *, prenotazione_id=None, ospite_id=None, per_data=None):
    try:
        numero = cifre(telefono); tpl = await get_template(db, chiave, "whatsapp")
        if not numero or not tpl: return None
        testo = await link_corti.accorcia_testo(db, render(tpl.corpo, variabili))
        return await adattatore.accoda(db, MessaggioWhatsApp(...))
    except Exception:
        logger.exception("WhatsApp %s non messo in coda", chiave); return None   # MAI bloccante
```

- **Niente doppioni**: se per la stessa prenotazione c'è già un messaggio dello stesso `tipo` ancora `da_inviare`, se ne
  aggiorna il testo invece di crearne un altro.
- **Messaggi superati tolti da soli**: quando una prenotazione viene spostata o annullata,
  `annulla_per_prenotazione(id, tipi)` mette `annullato` i messaggi ancora da inviare che non hanno più senso (es. il
  promemoria del vecchio giorno).
- `cifre()`: toglie tutto tranne le cifre, `00` → `+`, aggiunge `+39` se manca il prefisso, valido se ha 8–15 cifre.

### 5.3 I link che aprono WhatsApp

```python
def link(numero, testo, apri_con="app_desktop"):
    if apri_con == "web":         return f"https://web.whatsapp.com/send?phone={numero}&text={quote(testo)}"
    if apri_con == "app_desktop": return f"whatsapp://send?phone={numero}&text={quote(testo)}"
    return f"https://api.whatsapp.com/send?phone={numero}&text={quote(testo)}"   # «automatico»

def link_sms(numero, testo):      return f"sms:+{numero}?&body={quote(testo)}"   # per chi non ha WhatsApp
```

- **Non usare `wa.me`**: il suo rimando sostituisce le emoji con «�» (verificato). `api.whatsapp.com/send` le conserva.
- Impostazioni → WhatsApp: scelta di **«Apri con»** (app sul computer, WhatsApp Web, automatico) e numero del gestore.
- I link si generano **quando si legge la coda**, non quando si salva, così cambiare «Apri con» vale anche per i
  messaggi già in coda.

### 5.4 La pagina della coda (dashboard)

- Elenco «Da inviare» ordinato per `per_data`, poi per data di entrata, raggruppato in **«Oggi»** e **«Precedenti»**
  (rimasti indietro). Ricerca per nome, cognome o telefono (senza badare agli accenti) e filtro per tipo.
  Scheda «Inviati / tolti» con gli ultimi 50.
- Ogni voce mostra: ospite, tipo di messaggio, data della prenotazione, telefono, **stato dell'email gemella** (stessa
  chiave, stessa prenotazione), testo **modificabile prima dell'invio**, pulsanti **Apri WhatsApp**, **SMS**,
  **Segna come inviato** (annullabile) e **Togli**.
- **«Inviato» lo segna il gestore** a mano dopo aver mandato il messaggio. Aprire WhatsApp non vuol dire averlo
  mandato: è una decisione esplicita del committente.
- **«Invia in sequenza»**: segue i filtri attivi, evidenzia e centra la voce successiva dopo ogni «Segnato».
- Apertura dal browser (`apriMessaggio(url)` nel frontend):
  - `https://…` (WhatsApp Web, api.whatsapp.com) → **scheda nuova**. Da computer `api.whatsapp.com` si trasforma in
    `whatsapp://`.
  - `whatsapp://` e `sms:` → **stessa pagina** (in una scheda nuova restava bianca). Si ascoltano `blur` e
    `visibilitychange` per 2,5 s: se la pagina non ha perso il focus l'app non si è aperta, e un avviso propone
    «Apri con WhatsApp Web».
  - SMS da computer: il testo si copia negli appunti con un avviso.
- **Pulsante WhatsApp** anche nella scheda della prenotazione, della richiesta e dell'ospite:
  `POST /admin/whatsapp/link` con `prenotazione_id` / `richiesta_id` / `ospite_id` + `chiave` del modello (oppure testo
  libero) → restituisce testo, telefono, link WhatsApp e link SMS. **Non mette niente in coda.**
- Contatore dei messaggi da inviare nel menu della dashboard.

### 5.5 Stile dei testi WhatsApp (preferenza del committente, stile «C»)

Testo su più righe brevi, poche emoji fisse (🌿 saluto, 📅 data, 🕘 ora, 📍 luogo, 👉 link), `*grassetto*` su data e
ora, firma in `_corsivo_` e link corto. Esempio di **forma**, non di contenuto (i testi veri li decide il committente):

```
Ciao {nome} 🌿

la tua prenotazione è confermata.

📅 *{data_arrivo}*
🕘 *{ora_checkin}*
📍 {indirizzo}

👉 {link_prenotazione}

_{firma}_
```

### 5.6 Anteprima del link nei messaggi

Quando su WhatsApp si incolla un link del sito compare una scheda con titolo, descrizione e immagine, presi dai meta
`og:` dell'HTML servito dal server del sito. L'immagine si sceglie in dashboard (Impostazioni → Foto → «Immagine dei
link nei messaggi»), a 1200 × 630 px. Le pagine personali (con token) sono `noindex` e il token non compare mai in
`canonical` / `og:url`.

---

## 6. Link corti (regola ferrea del committente)

**Ogni link che ospite o gestore vede** (email, WhatsApp, pagine del sito e della dashboard) è nella forma corta
`<sito>/l/<codice>`, mai il link lungo con il token. Vale anche per i link aggiunti in futuro.

- Collezione `link_corti`: `codice` (indice unico), `destinazione` (indice unico), `created_at`, `scade_il` facoltativa.
- Codice di 10 caratteri `[A-Za-z0-9]` generato con `secrets` (circa 59 bit: non si indovina). **Stesso link = stesso
  codice.** Se due richieste lo creano insieme, al `DuplicateKeyError` si rilegge quello esistente.
- Si accorcia **solo sopra i 60 caratteri**: i link già brevi (`/prenota`) restano leggibili. **Mai le immagini**
  (Cloudinary, `.png`, `.jpg`…) e mai negli attributi `src` / `srcset` dell'HTML.
- I link con un token monouso nei parametri (`?token=`, `?accesso=`) **scadono dopo 2 giorni**.
- Si accorcia **al momento dell'invio**: in `send_email` (testo e HTML), in `accoda` del WhatsApp, nel pulsante
  WhatsApp, nelle anteprime e nelle prove dei modelli, e nelle risposte della dashboard che mostrano link. Togli dalla
  fine del link la punteggiatura e i segni di formattazione WhatsApp attaccati (`.,;:!?)]}*_~`).
- Risoluzione: `GET /api/public/l/{codice}` → `{destinazione}`. Il server del sito su `/l/<codice>` risponde con un
  **302** verso la pagina vera (`Cache-Control: no-store`, `Referrer-Policy: no-referrer`). I file del backend (es. un
  feed iCal) li serve direttamente perché alcune app non seguono i rimandi. Codice sconosciuto o scaduto → pagina 404.
- Nei test serve una funzione `espandi()` che rimetta i link veri per poterli controllare.

---

## 7. Il punto d'ingresso unico

```python
async def invia(db, chiave, a_email, variabili, prenotazione_id=None, telefono=None,
                per_data=None, whatsapp_in_coda=True, ospite_id=None, richiesta_id=None) -> dict:
    esito = {"email": None, "whatsapp_id": None}
    if prenotazione_id and telefono is None:
        telefono, ospite_id = await contatto_prenotazione(db, prenotazione_id)
    if a_email:
        try:
            extra = await extra_evento(db, prenotazione_id) if chiave in CHIAVI_EVENTO else None  # .ics + pulsanti
            esito["email"] = await send_template_email(db, chiave, a_email, variabili, extra,
                                                       prenotazione_id=prenotazione_id, richiesta_id=richiesta_id)
        except Exception:
            logger.exception("Email %s non registrata", chiave)
    if telefono and whatsapp_in_coda:
        esito["whatsapp_id"] = await whatsapp.accoda(db, chiave, telefono, variabili,
                                                     prenotazione_id=prenotazione_id, ospite_id=ospite_id, per_data=per_data)
    return esito
```

- Si chiama **dove succede la cosa** (conferma della richiesta, pagamento ricevuto, modifica, annullamento…), mai
  sparsa in più punti per lo stesso evento.
- L'esito si restituisce alla dashboard. Esempio: dopo «Conferma» l'avviso dice se l'email è partita davvero e, se ci
  sono WhatsApp da mandare per quella prenotazione, la pagina **va da sola** alla coda con quei messaggi evidenziati
  (preferenza del committente: le procedure a passi scorrono e si centrano sul passo successivo, mai una pagina ferma
  con solo un avviso).
- **Avvisi al gestore** (nuova richiesta, pagamento arrivato…) passano da un'altra funzione unica,
  `notifica(db, evento, email=(modello, variabili), push=...)`, con le scelte per evento salvate nelle impostazioni
  (email sì/no, notifica sul telefono sì/no). Se il tuo sito ha già le notifiche push, collegale qui; se no, parti
  dalle sole email.

---

## 8. Invii automatici (job)

- Registro `JOBS = {"nome": funzione}`. Ogni job è `async def nome(db) -> str` e restituisce una riga di riepilogo.
- Avvio: `python -m app.jobs run_all` dal **cron dell'hosting ogni 5 minuti**, oppure `POST /api/jobs/run` con header
  `X-Cron-Secret` (confronto con `secrets.compare_digest`; senza `CRON_SECRET` risponde 503). Un job che fallisce non
  ferma gli altri e l'esito di ogni giro si salva.
- **Idempotenza, il punto chiave.**
  - *Una volta per oggetto*: prendi un elemento alla volta con `find_one_and_update` atomico, filtrando sul campo
    «già fatto» vuoto e impostandolo nello stesso momento:
    ```python
    while True:
        doc = await db.prenotazioni.find_one_and_update(
            {"stato": "confermata", "arrivo": {"$gte": dal, "$lt": al}, "messaggi.promemoria_inviato_il": None},
            {"$set": {"messaggi.promemoria_inviato_il": utcnow()}})
        if not doc: break
        await invia(db, "promemoria_arrivo", email, variabili, prenotazione_id=..., per_data=domani)
    ```
    Quando la prenotazione viene spostata il campo si **azzera**, così il promemoria ripartirà per la data nuova.
  - *Una volta al giorno* (riepiloghi): `insert_one({"_id": f"{chiave}:{giorno}"})` in `invii_giornalieri`; se dà
    `DuplicateKeyError` è già stato fatto.
- **Orari dalle impostazioni** (es. «promemoria alle 18:00 del giorno prima»): il job controlla
  `ora_raggiunta(hh:mm, adesso_fuso_Europe/Rome)` e poi lavora sul giorno giusto. Calcola gli intervalli nel fuso
  italiano e convertili in UTC per le query.
- Job utili nell'altro sito, da adattare: promemoria il giorno prima (email + WhatsApp), riepilogo mattutino al
  gestore, **avviso serale al gestore se ci sono WhatsApp ancora da inviare**, richieste scadute senza risposta, pulizia
  dei token scaduti. Per una casa vacanze potrebbero servire istruzioni per l'arrivo X giorni prima, promemoria del
  check-out, richiesta di recensione dopo la partenza, saldo in scadenza. **Proponili, non darli per decisi.**

---

## 9. «Tutto modificabile da dashboard» (preferenza del committente)

Niente valori fissi nel codice. Si regolano dalla dashboard, ognuno per conto suo:
- testo, oggetto e interruttore di ogni modello, su ogni canale;
- orari e anticipo dei promemoria, interruttore per ogni invio automatico;
- «Apri con» di WhatsApp e numero del gestore;
- nome del mittente, indirizzo per le risposte, indirizzo che riceve gli avvisi;
- immagine dell'anteprima dei link.

Se una chiave manca (Resend, webhook), la funzione è **spenta con un messaggio chiaro in dashboard** (es. «Le email non
partono: manca la chiave di Resend. Restano salvate in Diagnostica.») e tutto il resto funziona.

---

## 10. Regole da rispettare

- Nessun dato riservato in email, WhatsApp, eventi del calendario, nomi dei file e log (documenti d'identità, dati di
  pagamento, note private). Nei log solo la chiave del modello e il tipo di errore.
- Nessun tracker: niente pixel o tracciamento di aperture e clic.
- Testi in italiano, del tu, informativi. Nei testi a nome del committente usa la **prima persona singolare** (è un
  libero professionista, non un'azienda).
- Le pagine personali raggiunte dai link (con token di 43 caratteri, `secrets.token_urlsafe(32)`) sono `noindex`,
  escluse da `robots.txt` e mostrano solo i dati necessari.

---

## 11. Prove (da scrivere insieme al codice)

Prove di integrazione contro il backend avviato, su un **database a parte** con dati di prova inventati, senza chiavi e
senza indirizzi veri nei test:
1. Senza `RESEND_API_KEY`: una conferma crea la voce in `email_outbox` con `stato=non_inviata`, `motivo=non_attivo` e
   l'allegato `.ics`, **e la prenotazione va a buon fine lo stesso**.
2. `render()`: le righe con variabili vuote spariscono (riga sola, «Etichetta: {var}», con `*` / `_` intorno).
3. WhatsApp: la conferma mette in coda un messaggio con il testo giusto. Una seconda chiamata per la stessa
   prenotazione aggiorna il testo invece di duplicarlo. Spostamento e annullamento tolgono i messaggi superati.
   Senza telefono valido non mette niente in coda.
4. Link: nessun link sopra i 60 caratteri nei testi di email e WhatsApp. `/l/<codice>` fa il 302 verso la pagina
   giusta. Un codice sconosciuto dà 404. Il link monouso scade.
5. Job: due `run_all` di fila (e due in parallelo) → **un solo** promemoria per prenotazione. Dopo uno spostamento il
   promemoria riparte. Un riepilogo giornaliero parte una volta sola.
6. Webhook: firma sbagliata → 400. Timestamp vecchio → 400. Senza segreto → 503. `delivered` dopo `delayed` →
   consegnata. `delayed` dopo `delivered` → resta consegnata.
7. `EMAIL_SOLO_A` e `EMAIL_LIMITE_GIORNO` danno i motivi giusti.
8. Dashboard (browser headless, a 1440 e 360 px): coda, modifica del testo, «Segna come inviato» e annulla, filtri,
   «Invia in sequenza», anteprima e prova dei modelli, stato dell'email accanto alla prenotazione, «Rimanda l'email».

Prova reale finale, solo con il via del committente e una chiave di test: un'email vera a un indirizzo suo
(`EMAIL_SOLO_A` impostato) e un WhatsApp aperto dalla coda verso il suo numero.

---

## 12. Ordine di lavoro consigliato

1. **Analisi**: cosa c'è già nel progetto per email e WhatsApp, cosa si tiene, cosa si sostituisce. Riepilogo al
   committente → via.
2. Modelli nel DB + `render` + seed + pagina «Messaggi automatici» (con anteprima e prova). I testi li proponi tu, li
   approva il committente.
3. Email: `send_email`, `email_outbox`, layout, `.ics`, Diagnostica, avviso in dashboard se manca la chiave.
4. Link corti (backend + rimando nel server del sito) e applicazione a tutti gli invii.
5. Coda WhatsApp + pagina in dashboard + pulsanti WhatsApp nelle schede + Impostazioni → WhatsApp.
6. `invia()` collegata a tutti gli eventi del sito + annullamento dei messaggi superati.
7. Job pianificati idempotenti + cron.
8. Webhook di Resend + stato dell'email in dashboard + «Rimanda».
9. Prove, aggiornamento della documentazione del progetto, commit, riepilogo → via.

Ogni fase finisce con le prove, il commit e un riepilogo semplice per il committente, poi **si aspetta il via**.
