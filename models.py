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
    payment_choice: Literal['full'] = 'full'
    cancellation_policy: Literal['flexible', 'moderate', 'strict'] = 'moderate'
    status: Literal['pending', 'confirmed', 'cancelled', 'external'] = 'pending'
    payment_status: Literal['unpaid', 'fully_paid', 'refunded'] = 'unpaid'
    source: Literal['website', 'airbnb', 'booking', 'manual'] = 'website'
    notes: Optional[str] = None
    consent_newsletter: bool = False
    payment_intent_id: Optional[str] = None
    refund_amount: Optional[float] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class BookingCreate(BaseModel):
    guest_name: str
    guest_email: EmailStr
    guest_phone: Optional[str] = None
    check_in: str
    check_out: str
    adults: int = 2
    children: int = 0
    consent_newsletter: bool = False

class BookingUpdate(BaseModel):
    guest_name: Optional[str] = None
    guest_email: Optional[EmailStr] = None
    guest_phone: Optional[str] = None
    status: Optional[Literal['pending', 'confirmed', 'cancelled', 'external']] = None
    payment_status: Optional[Literal['unpaid', 'fully_paid', 'refunded']] = None
    total_price: Optional[float] = None
    notes: Optional[str] = None

class Subscriber(BaseModel):
    email: EmailStr
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class Settings(BaseModel):
    villa_name: str = "Light Blue Villa"
    villa_email: str = ""
    villa_phone: str = ""
    villa_address: str = ""
    villa_lake: str = ""
    villa_cir: str = ""
    villa_description: str = ""
    default_price_per_night: float = 250.0
    default_cancellation_policy: Literal['flexible', 'moderate', 'strict'] = 'moderate'
    seasonal_rates: List[dict] = []
    ical_airbnb_url: Optional[str] = ""
    ical_booking_url: Optional[str] = ""
    last_ical_sync_at: Optional[str] = None
    last_ical_sync_count: Optional[int] = 0
    last_minute_enabled: bool = False
    last_minute_window_days: Optional[int] = None
    last_minute_discount_percent: Optional[float] = None
    last_minute_title: Optional[str] = None
    last_minute_subtitle: Optional[str] = None

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

class MarketingEmail(BaseModel):
    subject: str
    html_content: str
    recipients: List[EmailStr]

class RefundRequest(BaseModel):
    amount: Optional[float] = None
    reason: Optional[str] = None

class PaymentTransaction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    booking_id: str
    amount: float
    payment_status: Literal['initiated', 'paid', 'unpaid', 'failed'] = 'initiated'
    status: Literal['open', 'complete', 'expired'] = 'open'
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Optional[dict] = {}
