import collections
import datetime as dt
import hashlib
import io
import json
import logging
import numbers
import os
import sqlite3
import threading
import uuid

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile
from logging.handlers import RotatingFileHandler

DB_PATH = "data/state.db"
SHEET = os.getenv("SHEET_NAME", "0")
MAX_DELETE_RATIO = float(os.getenv("MAX_DELETE_RATIO", "0.5"))

handler = RotatingFileHandler("logs/processor.log", maxBytes=5_000_000, backupCount=5)
logging.basicConfig(
    level=logging.INFO,
    handlers=[handler],
    format="%(asctime)s %(levelname)s %(message)s",
)
app = FastAPI()
lock = threading.Lock()


def norm(v) -> str:
    return " ".join(str(v).split())  # toglie spazi iniziali/finali, doppi spazi e a capo


KEY_COLUMNS = [norm(c) for c in os.getenv("KEY_COLUMN", "Product code").split(",")]


def canon(v):
    """Valore normalizzato, indipendente dai tipi di pandas e dall'app che ha salvato il file."""
    if pd.isna(v):
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, bool):
        return v
    if isinstance(v, numbers.Real):
        f = round(float(v), 6)
        return int(f) if f.is_integer() else f
    s = str(v).strip()
    return s or None


def key_of(rec: dict) -> str:
    return "|".join(str(rec[c]) for c in KEY_COLUMNS)


def row_hash(rec: dict) -> str:
    # per il confronto i testi vengono normalizzati del tutto (spazi e a capo compresi)
    n = {k: (norm(v) if isinstance(v, str) else v) for k, v in rec.items()}
    return hashlib.sha256(
        json.dumps(n, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def is_total_row(rec: dict) -> bool:
    """Riga di totale: chiave vuota e nessun testo nelle altre colonne."""
    key_empty = any(rec[c] is None for c in KEY_COLUMNS)
    has_text = any(
        isinstance(v, str) for c, v in rec.items() if c not in KEY_COLUMNS
    )
    return key_empty and not has_text


def read_table(raw: bytes, sheet) -> list:
    """Trova la riga di intestazione (prime 30 righe) e restituisce [(riga_excel, record)]."""
    raw_df = pd.read_excel(io.BytesIO(raw), sheet_name=sheet, header=None, dtype=object)
    wanted = set(KEY_COLUMNS)
    for i in range(min(len(raw_df), 30)):
        header = ["" if pd.isna(v) else norm(v) for v in raw_df.iloc[i].tolist()]
        if wanted <= set(header):
            cols = [(j, h) for j, h in enumerate(header) if h]
            out = []
            for n, row in enumerate(raw_df.iloc[i + 1:].itertuples(index=False), start=i + 2):
                rec = {h: canon(row[j]) for j, h in cols}
                if all(v is None for v in rec.values()):
                    continue  # riga completamente vuota
                out.append((n, rec))
            return out
    raise HTTPException(
        422, f"Intestazione con le colonne {KEY_COLUMNS} non trovata nelle prime 30 righe"
    )


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS snapshot (k TEXT PRIMARY KEY, h TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS pending "
        "(run_id TEXT PRIMARY KEY, file_hash TEXT, current TEXT)"
    )
    return con


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/state")
def state():
    con = db()
    try:
        n = con.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        fh = con.execute("SELECT value FROM meta WHERE name='file_hash'").fetchone()
        pend = con.execute("SELECT run_id FROM pending").fetchone()
        return {
            "snapshot_rows": n,
            "committed_file_hash": fh[0][:8] if fh else None,
            "pending_run": pend[0] if pend else None,
        }
    finally:
        con.close()


@app.post("/process")
async def process(file: UploadFile):
    raw = await file.read()
    file_hash = hashlib.sha256(raw).hexdigest()
    run_id = uuid.uuid4().hex[:8]

    with lock:
        con = db()
        try:
            last = con.execute(
                "SELECT value FROM meta WHERE name='file_hash'"
            ).fetchone()
            if last and last[0] == file_hash:
                return {"changed": False, "run_id": run_id, "file_hash": file_hash[:8]}

            sheet = int(SHEET) if SHEET.isdigit() else SHEET
            rows = read_table(raw, sheet)

            records, totals, bad = [], 0, []
            for xl_row, rec in rows:
                if is_total_row(rec):
                    totals += 1
                elif any(rec[c] is None for c in KEY_COLUMNS):
                    bad.append(xl_row)
                else:
                    records.append(rec)
            if bad:
                raise HTTPException(422, f"Righe con chiave vuota (righe Excel): {bad[:10]}")
            if totals:
                logging.info("run=%s scartate %d righe di totale", run_id, totals)

            # --- qui puoi aggiungere i tuoi filtri sulla lista `records` ---

            keys = [key_of(r) for r in records]
            dups = [k for k, n in collections.Counter(keys).items() if n > 1]
            if dups:
                raise HTTPException(422, f"Chiavi duplicate (prime 10): {dups[:10]}")

            current = {k: row_hash(r) for k, r in zip(keys, records)}
            snap = dict(con.execute("SELECT k, h FROM snapshot").fetchall())
            upserts = [r for k, r in zip(keys, records) if snap.get(k) != current[k]]
            deletes = [k for k in snap if k not in current]

            if snap and len(deletes) > len(snap) * MAX_DELETE_RATIO:
                raise HTTPException(
                    409, f"Troppe cancellazioni ({len(deletes)}/{len(snap)}): bloccato"
                )

            con.execute("DELETE FROM pending")
            con.execute(
                "INSERT INTO pending VALUES (?, ?, ?)",
                (run_id, file_hash, json.dumps(current)),
            )
            con.commit()

            logging.info(
                "run=%s file=%s righe=%d snapshot=%d upserts=%d deletes=%d",
                run_id, file_hash[:8], len(records), len(snap), len(upserts), len(deletes),
            )
            return {
                "changed": bool(upserts or deletes),
                "run_id": run_id,
                "file_hash": file_hash[:8],
                "rows_in": len(records),
                "snapshot_rows": len(snap),
                "upserts": upserts,
                "deletes": deletes,
            }
        except HTTPException:
            logging.exception("run=%s rifiutato", run_id)
            raise
        except Exception:
            logging.exception("run=%s errore", run_id)
            raise
        finally:
            con.close()


@app.post("/commit/{run_id}")
def commit(run_id: str):
    with lock:
        con = db()
        try:
            row = con.execute(
                "SELECT file_hash, current FROM pending WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Run non trovata o superata da una più recente")
            file_hash, current = row[0], json.loads(row[1])
            with con:  # transazione atomica
                con.execute("DELETE FROM snapshot")
                con.executemany("INSERT INTO snapshot VALUES (?, ?)", current.items())
                con.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('file_hash', ?)", (file_hash,)
                )
                con.execute("DELETE FROM pending")
            logging.info("run=%s commit ok", run_id)
            return {"committed": True, "run_id": run_id}
        finally:
            con.close()