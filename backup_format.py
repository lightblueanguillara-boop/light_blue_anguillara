"""Formato del file di backup (lettura e controllo).

Modulo indipendente dal resto del server: il ripristino deve funzionare anche senza le
variabili d'ambiente di produzione, con il solo file di backup in mano.
"""
import gzip

from bson import json_util

FORMAT = 'light-blue-backup'
VERSION = 1


def read_backup(data: bytes) -> dict:
    """Legge e controlla un file di backup. Solleva ValueError se non è un backup valido."""
    try:
        doc = json_util.loads(gzip.decompress(data).decode())
    except Exception:
        raise ValueError('Questo non è un file di backup di Light Blue')
    meta = doc.get('meta') if isinstance(doc, dict) else None
    if not isinstance(meta, dict) or meta.get('format') != FORMAT or not isinstance(doc.get('collections'), dict):
        raise ValueError('Questo non è un file di backup di Light Blue')
    if int(meta.get('version', 0)) > VERSION:
        raise ValueError('Backup creato da una versione più recente del programma')
    return doc
