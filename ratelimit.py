"""Limitatore di richieste in memoria (un solo processo, sufficiente per questo sito).

Protegge login, modulo contatti e checkout da tentativi a raffica e spam.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict = defaultdict(deque)


def client_ip(request: Request) -> str:
    """IP reale del visitatore dietro il proxy di Railway."""
    real = request.headers.get('x-real-ip')
    if real:
        return real.strip()
    fwd = request.headers.get('x-forwarded-for')
    if fwd:
        # l'ultimo valore è quello aggiunto dal proxy più vicino (non falsificabile dal client)
        return fwd.split(',')[-1].strip()
    return request.client.host if request.client else 'unknown'


def check_rate(key: str, limit: int, window_seconds: int) -> None:
    """Solleva 429 se `key` ha superato `limit` richieste negli ultimi `window_seconds`."""
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, 'Troppe richieste. Riprova tra qualche minuto.')
    q.append(now)
    # pulizia periodica per non far crescere la memoria
    if len(_hits) > 10000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            _hits.pop(k, None)


def rate_limit(request: Request, name: str, limit: int, window_seconds: int) -> None:
    check_rate(f'{name}:{client_ip(request)}', limit, window_seconds)


def reset_all() -> None:
    """Solo per i test."""
    _hits.clear()
