from datetime import date, datetime, timezone
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, EmailStr, field_validator, model_validator
import uuid

MAX_STAY_NIGHTS = 60          # soggiorno massimo prenotabile online
MAX_BOOKING_AHEAD_DAYS = 730  # prenotabile fino a 2 anni in anticipo

def _validate_iso_date(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    try:
        datetime.strptime(v, '%Y-%m-%d')
        return v
    except (ValueError, TypeError):
        raise ValueError('Date must be in YYYY-MM-DD format')

def validate_stay(check_in: str, check_out: str, allow_past: bool = False) -> None:
    """Controlli comuni su un intervallo di soggiorno (date ISO già validate)."""
    ci = datetime.strptime(check_in, '%Y-%m-%d').date()
    co = datetime.strptime(check_out, '%Y-%m-%d').date()
    if co <= ci:
        raise ValueError('La data di partenza deve essere successiva a quella di arrivo')
    if (co - ci).days > MAX_STAY_NIGHTS:
        raise ValueError(f'Soggiorno massimo prenotabile online: {MAX_STAY_NIGHTS} notti')
    today = date.today()
    if not allow_past and ci < today:
        raise ValueError('La data di arrivo non può essere nel passato')
    if (ci - today).days > MAX_BOOKING_AHEAD_DAYS:
        raise ValueError('Data di arrivo troppo lontana nel tempo')


class AdminLogin(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)
    device_id: Optional[str] = Field(default=None, max_length=64)   # per riconoscere i nuovi dispositivi
    device_label: Optional[str] = Field(default=None, max_length=120)


class PushSubscribe(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: dict
    label: Optional[str] = Field(default=None, max_length=120)

    @field_validator('keys')
    @classmethod
    def _keys(cls, v):
        if not isinstance(v.get('p256dh'), str) or not isinstance(v.get('auth'), str) or len(v['p256dh']) > 200 or len(v['auth']) > 100:
            raise ValueError('Chiavi di iscrizione non valide')
        return {'p256dh': v['p256dh'], 'auth': v['auth']}


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=10, max_length=200)

class Booking(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    guest_name: str
    guest_email: EmailStr
    guest_phone: Optional[str] = None
    check_in: str  
    check_out: str
    adults: int = 2
    children: int = 0
    total_price: float
    deposit_amount: float
    payment_choice: Literal['deposit', 'full'] = 'deposit'
    is_refundable: bool = True
    status: Literal['pending', 'confirmed', 'cancelled', 'external', 'request', 'awaiting_payment', 'rejected', 'expired', 'blocked'] = 'pending'
    payment_status: Literal['unpaid', 'deposit_paid', 'fully_paid', 'refunded'] = 'unpaid'
    source: Literal['website', 'airbnb', 'booking', 'manual', 'block'] = 'website'
    notes: Optional[str] = None
    consent_newsletter: bool = False
    payment_intent_id: Optional[str] = None
    refund_amount: Optional[float] = None
    last_reminder_at: Optional[str] = None
    terms_accepted_at: Optional[str] = None
    # Prenotazione su richiesta
    contact_whatsapp: bool = False          # l'ospite accetta di essere contattato su WhatsApp
    request_expires_at: Optional[str] = None  # entro quando il proprietario deve rispondere
    payment_due_at: Optional[str] = None      # entro quando l'ospite deve pagare dopo l'ok
    payment_url: Optional[str] = None
    decided_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class BookingCreate(BaseModel):
    guest_name: str = Field(min_length=2, max_length=120)
    guest_email: EmailStr
    guest_phone: Optional[str] = Field(default=None, max_length=40)
    check_in: str
    check_out: str
    adults: int = Field(default=2, ge=1, le=10)
    children: int = Field(default=0, ge=0, le=10)
    payment_choice: Literal['deposit', 'full'] = 'deposit'
    is_refundable: bool = True
    notes: Optional[str] = Field(default=None, max_length=2000)
    consent_newsletter: bool = False
    accept_terms: bool = False
    origin_url: str = Field(default='', max_length=300)

    @field_validator('check_in', 'check_out')
    @classmethod
    def _date_format(cls, v):
        return _validate_iso_date(v)

    @model_validator(mode='after')
    def _check_stay(self):
        validate_stay(self.check_in, self.check_out)
        if not self.accept_terms:
            raise ValueError('È necessario accettare le Condizioni di prenotazione')
        return self

class BookingRequestCreate(BookingCreate):
    """Richiesta di prenotazione (modalità "su richiesta"): il telefono è obbligatorio."""
    guest_phone: str = Field(min_length=6, max_length=40)
    contact_whatsapp: bool = False


class RejectRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)


class BlockCreate(BaseModel):
    """Date chiuse dal proprietario. check_out = giorno successivo all'ultima notte bloccata."""
    check_in: str
    check_out: str
    note: Optional[str] = Field(default=None, max_length=300)

    @field_validator('check_in', 'check_out')
    @classmethod
    def _date_format(cls, v):
        return _validate_iso_date(v)

    @model_validator(mode='after')
    def _check(self):
        ci = datetime.strptime(self.check_in, '%Y-%m-%d').date()
        co = datetime.strptime(self.check_out, '%Y-%m-%d').date()
        if co <= ci:
            raise ValueError('Seleziona almeno una notte')
        if (co - ci).days > 366:
            raise ValueError('Puoi bloccare al massimo un anno alla volta')
        if ci < date.today():
            raise ValueError('Non puoi bloccare date passate')
        return self


class PriceUpdate(BaseModel):
    """Prezzo personalizzato per le notti in [start, end). price=None ripristina la tariffa normale."""
    start: str
    end: str
    price: Optional[float] = Field(default=None, gt=0, le=100000)

    @field_validator('start', 'end')
    @classmethod
    def _date_format(cls, v):
        return _validate_iso_date(v)

    @model_validator(mode='after')
    def _check(self):
        s = datetime.strptime(self.start, '%Y-%m-%d').date()
        e = datetime.strptime(self.end, '%Y-%m-%d').date()
        if e <= s:
            raise ValueError('Seleziona almeno una notte')
        if (e - s).days > 366:
            raise ValueError('Puoi modificare al massimo un anno alla volta')
        return self


class BookingUpdate(BaseModel):
    status: Optional[Literal['pending', 'confirmed', 'cancelled', 'external', 'request', 'awaiting_payment', 'rejected', 'expired', 'blocked']] = None
    payment_status: Optional[Literal['unpaid', 'deposit_paid', 'fully_paid', 'refunded']] = None
    guest_name: Optional[str] = None
    guest_email: Optional[EmailStr] = None
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    is_refundable: Optional[bool] = None

    @field_validator('check_in', 'check_out')
    @classmethod
    def _date_format(cls, v):
        return _validate_iso_date(v)

class ContactMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    email: EmailStr
    phone: Optional[str] = None
    subject: Optional[str] = None
    message: str
    consent_newsletter: bool = False
    status: Literal['new', 'read', 'replied'] = 'new'
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    replies: List[dict] = Field(default_factory=list)

class ContactCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=40)
    subject: Optional[str] = Field(default=None, max_length=200)
    message: str = Field(min_length=2, max_length=5000)
    consent_newsletter: bool = False
    website: Optional[str] = Field(default=None, max_length=200)  # campo trappola anti-bot

# Usato da inbox.py per aggiornare lo stato di un messaggio (new/read/replied)
class MessageUpdate(BaseModel):
    status: Optional[Literal['new', 'read', 'replied']] = None

class Subscriber(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: EmailStr
    name: Optional[str] = None
    source: str = 'website'
    consent: bool = True
    consent_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class SeasonalRate(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    start_date: str
    end_date: str
    price_per_night: float
    priority: int = 1

class VillaSettings(BaseModel):
    id: str = 'global'
    default_price_per_night: float = 120.0
    deposit_percent: float = 30.0
    non_refundable_discount_percent: float = 5.0
    ical_airbnb_url: str = ''
    ical_booking_url: str = ''
    villa_name: str = 'Light Blue - Anguillara Sabazia'
    villa_address: str = 'Piazza del Comune, 00061 Anguillara Sabazia (RM), Italia'
    villa_phone: str = '+39 000 000 0000'
    villa_email: str = 'info@lightblue-anguillara.it'
    villa_cir: str = 'IT058005C2MZEX4AR8'
    villa_lake: str = 'Lago di Bracciano'
    villa_description: str = "Descrizione Villa..."
    seasonal_rates: List[SeasonalRate] = []
    last_minute_enabled: bool = False
    last_minute_window_days: int = 14
    last_minute_discount_percent: float = 15.0
    last_minute_title: str = 'Last Minute'
    last_minute_subtitle: str = 'Sconto prossime date'
    # Modalità di prenotazione: 'request' (su richiesta, default) o 'instant' (pagamento immediato)
    booking_mode: Literal['instant', 'request'] = 'request'
    request_hold_hours: int = 24   # ore per rispondere a una richiesta (date bloccate)
    payment_link_hours: int = 24   # ore per pagare dopo l'accettazione (max 24, limite Stripe)
    # Versioni di logo e immagine di anteprima (cambiano a ogni caricamento: servono per la cache)
    logo_version: str = 'default'
    share_version: str = 'default'
    # Notifiche sul telefono
    notify_groups: dict = Field(default_factory=lambda: {'A': True, 'B': True, 'C': True, 'D': True})
    notify_detail: Literal['detailed', 'discreet'] = 'detailed'
    notify_quiet_enabled: bool = True
    notify_quiet_start: str = '23:00'
    notify_quiet_end: str = '08:00'
    # Dati legali (Informativa privacy, Condizioni di prenotazione, footer)
    legal_owner_name: str = ''
    legal_owner_tax_id: str = ''
    legal_owner_address: str = ''
    tourist_tax_note: str = ''

# Usato da operations.py per aggiornare le impostazioni globali della villa
class SettingsUpdate(BaseModel):
    default_price_per_night: Optional[float] = Field(default=None, gt=0, le=100000)
    deposit_percent: Optional[float] = Field(default=None, ge=0, le=100)
    non_refundable_discount_percent: Optional[float] = Field(default=None, ge=0, le=90)
    ical_airbnb_url: Optional[str] = None
    ical_booking_url: Optional[str] = None
    villa_name: Optional[str] = None
    villa_address: Optional[str] = None
    villa_phone: Optional[str] = None
    villa_email: Optional[str] = None
    villa_cir: Optional[str] = None
    villa_lake: Optional[str] = None
    villa_description: Optional[str] = None
    seasonal_rates: Optional[List[SeasonalRate]] = None
    last_minute_enabled: Optional[bool] = None
    last_minute_window_days: Optional[int] = Field(default=None, ge=1, le=90)
    last_minute_discount_percent: Optional[float] = Field(default=None, ge=0, le=90)
    last_minute_title: Optional[str] = None
    last_minute_subtitle: Optional[str] = None
    booking_mode: Optional[Literal['instant', 'request']] = None
    notify_groups: Optional[dict] = None
    notify_detail: Optional[Literal['detailed', 'discreet']] = None
    notify_quiet_enabled: Optional[bool] = None
    notify_quiet_start: Optional[str] = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    notify_quiet_end: Optional[str] = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    request_hold_hours: Optional[int] = Field(default=None, ge=1, le=168)
    payment_link_hours: Optional[int] = Field(default=None, ge=1, le=24)
    legal_owner_name: Optional[str] = Field(default=None, max_length=200)
    legal_owner_tax_id: Optional[str] = Field(default=None, max_length=40)
    legal_owner_address: Optional[str] = Field(default=None, max_length=300)
    tourist_tax_note: Optional[str] = Field(default=None, max_length=1000)

class GalleryImage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    public_id: str
    caption: Optional[str] = ""
    category: Literal['gallery', 'home', 'rooms', 'general'] = 'general'
    order: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# Usato da operations.py per aggiornare i metadati di un'immagine
class GalleryImageUpdate(BaseModel):
    caption: Optional[str] = None
    category: Optional[Literal['gallery', 'home', 'rooms', 'general']] = None
    order: Optional[int] = None

class MarketingEmail(BaseModel):
    subject: str
    html_content: str
    recipients: List[EmailStr]  # coincide con selectedEmails inviato dal JSX

class RefundRequest(BaseModel):
    amount: Optional[float] = None
    reason: Optional[str] = None

class PaymentTransaction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    booking_id: str
    amount: float
    payment_status: Literal['initiated', 'paid', 'unpaid', 'failed'] = 'initiated'
    status: Literal['open', 'complete', 'expired', 'cancelled'] = 'open'
    payment_intent_id: Optional[str] = None
    metadata: dict = {}
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
