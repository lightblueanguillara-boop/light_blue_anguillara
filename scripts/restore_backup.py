"""Ripristina un backup di Light Blue (file .json.gz scaricato dal pannello o ricevuto per email).

Uso:
  python scripts/restore_backup.py light-blue-backup-2026-11-15-0300.json.gz \\
      --mongo-url "mongodb://..." --db light_blue_db

Per sicurezza NON sovrascrive nulla: se il database di destinazione contiene già dati nelle
stesse raccolte si ferma. Per sostituirli davvero aggiungere --replace (chiede conferma).
"""
import argparse
import sys
from pathlib import Path

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup_format import read_backup  # noqa: E402  (non richiede le variabili del server)


def restore(data: bytes, database, replace: bool = False) -> dict:
    """Carica il backup nel database indicato. Ritorna il numero di documenti per raccolta."""
    doc = read_backup(data)
    collections = doc['collections']
    busy = [n for n in collections if database[n].estimated_document_count() > 0]
    if busy and not replace:
        raise RuntimeError('Il database contiene già dati in: ' + ', '.join(busy)
                           + '. Usa --replace per sostituirli.')
    restored = {}
    for name, docs in collections.items():
        if replace:
            database[name].delete_many({})
        if docs:
            database[name].insert_many(docs, ordered=False)
        restored[name] = len(docs)
    return restored


def main() -> int:
    ap = argparse.ArgumentParser(description='Ripristina un backup di Light Blue')
    ap.add_argument('file')
    ap.add_argument('--mongo-url', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--replace', action='store_true', help='cancella i dati presenti prima di ripristinare')
    ap.add_argument('--yes', action='store_true', help='non chiedere conferma')
    a = ap.parse_args()

    data = Path(a.file).read_bytes()
    try:
        meta = read_backup(data)['meta']
    except ValueError as e:
        print(f'Fermato: {e}')
        return 1
    print(f"Backup del {meta['created_at']} — {sum(meta['counts'].values())} documenti")
    for name, n in meta['counts'].items():
        print(f'  {name}: {n}')
    if a.replace and not a.yes:
        if input(f'I dati attuali del database "{a.db}" verranno SOSTITUITI. Scrivi SI per continuare: ') != 'SI':
            print('Annullato.')
            return 1
    try:
        restored = restore(data, MongoClient(a.mongo_url)[a.db], replace=a.replace)
    except RuntimeError as e:
        print(f'Fermato: {e}')
        return 1
    print(f'Ripristino completato: {sum(restored.values())} documenti.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
