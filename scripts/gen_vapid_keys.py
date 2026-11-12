"""Genera la coppia di chiavi VAPID per le notifiche push (da mettere nelle variabili di Railway).

Uso:  python scripts/gen_vapid_keys.py
La chiave privata va tenuta segreta; la pubblica viene data ai browser per iscriversi.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


key = ec.generate_private_key(ec.SECP256R1())
private_raw = key.private_numbers().private_value.to_bytes(32, 'big')
public_raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
print(f'VAPID_PUBLIC_KEY={b64url(public_raw)}')
print(f'VAPID_PRIVATE_KEY={b64url(private_raw)}')
