"""
Script de ingestão via linha de comando: lê todos os PDFs de uma pasta e
processa cada um. Útil para ingestões em lote (ex: primeira carga de muitos
livros de uma vez) ou reprocessamento manual.

Para ingestão do dia a dia (um livro de cada vez, com interface visual),
use o painel web em /admin.

Uso:
    python ingest.py /caminho/para/pasta/com/pdfs
    python ingest.py /caminho/para/pasta/com/pdfs --force   # reprocessa mesmo se já existir
"""
import os
import sys

import psycopg
from pypdf import PdfReader

from ingestion import (
    DATABASE_URL,
    MIN_CHUNK_LENGTH,
    OCR_MIN_TEXT_LENGTH,
    chunk_text,
    clean_text,
    embed_sync,
    ocr_page,
)


def create_job(conn: psycopg.Connection, filename: str, title: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ingest_jobs (book_title, filename, status)
               VALUES (%s, %s, 'pending') RETURNING id""",
            (title, filename),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    return job_id


def update_job(conn: psycopg.Connection, job_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [job_id]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE ingest_jobs SET {sets}, updated_at = now() WHERE id = %s", values)
    conn.commit()


def already_ingested(conn: psycopg.Connection, title: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM book_chunks WHERE book_title = %s LIMIT 1", (title,))
        return cur.fetchone() is not None


def ingest_pdf(path: str, conn: psycopg.Connection, force: bool = False) -> None:
    filename = os.path.basename(path)
    title = os.path.splitext(filename)[0]

    if not force and already_ingested(conn, title):
        print(f"Pulado (já existe): {title}")
        return

    if force:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM book_chunks WHERE book_title = %s", (title,))
        conn.commit()

    job_id = create_job(conn, filename, title)
    update_job(conn, job_id, status="processing")

    try:
        reader = PdfReader(path)
    except Exception as exc:
        update_job(conn, job_id, status="error", error_message=str(exc)[:500])
        print(f"ERRO ao abrir {filename}: {exc}")
        return

    total_chunks = 0
    with conn.cursor() as cur:
        for page_num, page in enumerate(reader.pages, start=1):
            try:
                text = clean_text(page.extract_text() or "")
            except Exception:
                text = ""

            if len(text.strip()) < OCR_MIN_TEXT_LENGTH:
                text = clean_text(ocr_page(path, page_num))

            if not text.strip():
                continue

            for chunk in chunk_text(text):
                chunk = clean_text(chunk)
                if len(chunk.strip()) < MIN_CHUNK_LENGTH:
                    continue
                try:
                    vector = embed_sync(chunk)
                except Exception as exc:
                    conn.commit()
                    update_job(conn, job_id, status="error", error_message=str(exc)[:500], total_chunks=total_chunks)
                    print(f"ERRO ao ingerir {filename}: {exc}")
                    return
                cur.execute(
                    """INSERT INTO book_chunks (book_title, page_number, chunk_text, embedding)
                       VALUES (%s, %s, %s, %s)""",
                    (title, page_num, chunk, vector),
                )
                total_chunks += 1

        conn.commit()

    update_job(conn, job_id, status="done", total_chunks=total_chunks)
    print(f"Ingerido: {title} ({total_chunks} trechos)")


def main() -> None:
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    folder = args[0] if args else "/books"

    if not os.path.isdir(folder):
        print(f"Pasta não encontrada: {folder}")
        sys.exit(1)

    with psycopg.connect(DATABASE_URL) as conn:
        for filename in sorted(os.listdir(folder)):
            if not filename.lower().endswith(".pdf"):
                continue
            try:
                ingest_pdf(os.path.join(folder, filename), conn, force=force)
            except Exception as exc:
                conn.rollback()
                print(f"ERRO ao ingerir {filename}: {exc}")

    print("Ingestão concluída.")


if __name__ == "__main__":
    main()