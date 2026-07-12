"""
Módulo compartilhado de ingestão: usado tanto pelo script de linha de comando
(ingest.py) quanto pelo painel administrativo web (rotas /admin/api/* em
server.py). Centraliza as regras de robustez: truncamento de textos longos,
retry automático em falhas de rede e checagem de duplicidade.
"""
import asyncio
import os
import time

import httpx
import psycopg
from pypdf import PdfReader

DATABASE_URL = os.environ["DATABASE_URL"]
EMBEDDING_URL = os.environ["EMBEDDING_SERVICE_URL"]

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
MIN_CHUNK_LENGTH = 50
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


def clean_text(text: str) -> str:
    """Remove bytes NUL e outros caracteres que o Postgres não aceita."""
    return text.replace("\x00", "")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Quebra o texto em pedaços de tamanho fixo, com sobreposição entre eles."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


def embed_sync(text: str) -> list[float]:
    """Chama o serviço de embeddings (versão síncrona, usada pelo script CLI).
    Trunca textos longos demais e tenta de novo em caso de falha de rede."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.post(
                f"{EMBEDDING_URL}/embed",
                json={"inputs": text, "truncate": True},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()[0]
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_error


async def embed_async(client: httpx.AsyncClient, text: str) -> list[float]:
    """Versão assíncrona (usada pelo painel web e pelo servidor MCP)."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.post(
                f"{EMBEDDING_URL}/embed",
                json={"inputs": text, "truncate": True},
            )
            resp.raise_for_status()
            return resp.json()[0]
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_error


async def process_book_async(path: str, job_id: int, force: bool = False) -> None:
    """Processa um único PDF de forma assíncrona, atualizando seu progresso
    na tabela ingest_jobs em tempo real (usado pelo painel web)."""
    title = os.path.splitext(os.path.basename(path))[0]

    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            if not force:
                await cur.execute(
                    "SELECT 1 FROM book_chunks WHERE book_title = %s LIMIT 1", (title,)
                )
                if await cur.fetchone():
                    await cur.execute(
                        "UPDATE ingest_jobs SET status='skipped', updated_at=now() WHERE id=%s",
                        (job_id,),
                    )
                    await conn.commit()
                    return
            else:
                await cur.execute("DELETE FROM book_chunks WHERE book_title = %s", (title,))

            await cur.execute(
                "UPDATE ingest_jobs SET status='processing', updated_at=now() WHERE id=%s",
                (job_id,),
            )
        await conn.commit()

        try:
            reader = PdfReader(path)
        except Exception as exc:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ingest_jobs SET status='error', error_message=%s, updated_at=now() WHERE id=%s",
                    (str(exc)[:500], job_id),
                )
            await conn.commit()
            return

        total_chunks = 0
        async with httpx.AsyncClient(timeout=60) as client:
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    text = clean_text(page.extract_text() or "")
                except Exception:
                    continue
                if not text.strip():
                    continue

                for chunk in chunk_text(text):
                    chunk = clean_text(chunk)
                    if len(chunk.strip()) < MIN_CHUNK_LENGTH:
                        continue

                    try:
                        vector = await embed_async(client, chunk)
                    except Exception as exc:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """UPDATE ingest_jobs
                                   SET status='error', error_message=%s, total_chunks=%s, updated_at=now()
                                   WHERE id=%s""",
                                (str(exc)[:500], total_chunks, job_id),
                            )
                        await conn.commit()
                        return

                    async with conn.cursor() as cur:
                        await cur.execute(
                            """INSERT INTO book_chunks (book_title, page_number, chunk_text, embedding)
                               VALUES (%s, %s, %s, %s)""",
                            (title, page_num, chunk, vector),
                        )
                    total_chunks += 1

                    if total_chunks % 10 == 0:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE ingest_jobs SET total_chunks=%s, updated_at=now() WHERE id=%s",
                                (total_chunks, job_id),
                            )
                        await conn.commit()

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ingest_jobs SET status='done', total_chunks=%s, updated_at=now() WHERE id=%s",
                (total_chunks, job_id),
            )
        await conn.commit()
