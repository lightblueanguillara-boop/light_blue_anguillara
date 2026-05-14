from datetime import datetime, timezone
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, EmailStr, field_validator
import uuid

def _validate_iso_date(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    try:
        datetime.strptime(v, '%Y-%m-%d')
        return v
    except (ValueError, TypeError):
        raise ValueError('Date must be in YYYY-MM-DD format')

class AdminLogin(BaseModel):
    email: EmailStr
    password: str

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
    cancellation_policy: Literal['flexible', 'moderate', 'strict'] = 'moderate'
    status: Literal['pending', 'confirmed', 'cancelled', 'external'] = 'pending'
    payment_status: Literal['unpaid', 'deposit_paid', 'fully_paid', 'refunded'] = 'unpaid'
    source: Literal['website', 'airbnb', 'booking', 'manual'] = 'website'
    notes: Optional[str] = None
    consent_newsletter: bool = False
    payment_intent_id: Optional[str] = None
    refund_amount: Optional[float] = None
    last_reminder_at: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class BookingCreate(BaseModel):
    guest_name: str
    guest_email: EmailStr
    guest_phone: Optional[str] = None
    check_in: str
    check_out: str
    adults: int = 2
    children: int = 0
    payment_choice: Literal['deposit', 'full'] = 'deposit'
    notes: Optional[str] = None
    consent_newsletter: bool = False
    origin_url: str

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

class ContactCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    subject: Optional[str] = None
    message: str
    consent_newsletter: bool = False

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
    default_cancellation_policy: Literal['flexible', 'moderate', 'strict'] = 'moderate'
    ical_airbnb_url: str = ''
    ical_booking_url: str = ''
    villa_name: str = 'Light Blue - Anguillara Sabazia'
    villa_address: str = 'Piazza del Comune, 00061 Anguillara Sabazia (RM), Italia'
    villa_phone: str = '+39 000 000 0000'
    villa_email: str = 'info@lightblue-anguillara.it'
    villa_cir: str = 'IT058005C2MZEX4AR8'
    villa_lake: str = 'Lago di Bracciano'
    villa_description: str = "..." # (testo abbreviato per brevità)
    seasonal_rates: List[SeasonalRate] = []
    last_minute_enabled: bool = False
    last_minute_window_days: int = 14
    last_minute_discount_percent: float = 15.0
    last_minute_title: str = 'Last Minute · prossime date libere'
    last_minute_subtitle: str = 'Approfitta dello sconto sulle prossime due settimane'

class PaymentTransaction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    booking_id: str
    amount: float
    currency: str = 'eur'
    payment_status: str = 'initiated'
    status: str = 'open'
    payment_intent_id: Optional[str] = None
    metadata: dict = {}
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class MarketingEmail(BaseModel):
    subject: str
    html_content: str
    recipients: Optional[List[str]] = None      # AGGIUNTO: Per invio tramite email
    recipient_ids: Optional[List[str]] = None   # Per invio tramite ID

class RefundRequest(BaseModel):
    amount: Optional[float] = None
    reason: Optional[str] = None

class BookingUpdate(BaseModel):
    status: Optional[Literal['pending', 'confirmed', 'cancelled', 'external']] = None
    payment_status: Optional[Literal['unpaid', 'deposit_paid', 'fully_paid', 'refunded']] = None
    cancellation_policy: Optional[Literal['flexible', 'moderate', 'strict']] = None
    guest_name: Optional[str] = None
    guest_email: Optional[EmailStr] = None
    guest_phone: Optional[str] = None
    adults: Optional[int] = None
    children: Optional[int] = None
    notes: Optional[str] = None
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    total_price: Optional[float] = None
    deposit_amount: Optional[float] = None

    @field_validator('check_in', 'check_out')
    @classmethod
    def _date_format(cls, v):
        return _validate_iso_date(v)

class MessageUpdate(BaseModel):
    status: Optional[Literal['new', 'read', 'replied']] = None

class SettingsUpdate(BaseModel):
    # ... (campi esistenti per settings)
    default_price_per_night: Optional[float] = None
    # ... aggiungi gli altri come nel tuo file originale

class GalleryImage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    public_id: str
    caption: Optional[str] = ""
    category: Literal['gallery', 'home', 'rooms', 'general'] = 'general'
    order: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class GalleryImageUpdate(BaseModel):
    caption: Optional[str] = None
    category: Optional[Literal['gallery', 'home', 'rooms', 'general']] = None
    order: Optional[int] = None
