"""
Módulo compartilhado de ingestão: usado tanto pelo script de linha de comando
(ingest.py) quanto pelo painel administrativo web (rotas /admin/api/* em
server.py). Centraliza as regras de robustez: truncamento de textos longos,
retry automático em falhas de rede e checagem de duplicidade.
"""
import asyncio
import os
import re
import time

import httpx
import psycopg
import pytesseract
from pdf2image import convert_from_path
from pypdf import PdfReader

DATABASE_URL = os.environ["DATABASE_URL"]
EMBEDDING_URL = os.environ["EMBEDDING_SERVICE_URL"]

CHUNK_SIZE = 1500
OVERLAP_SENTENCES = 2
MIN_CHUNK_LENGTH = 50
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# OCR: usado como fallback quando uma página não tem texto extraível
# (comum em PDFs escaneados/fotografados). Ativa só nessas páginas, então
# não deixa livros normais mais lentos.
OCR_LANGUAGES = "por+eng"
OCR_MIN_TEXT_LENGTH = 20
OCR_DPI = 200


def ocr_page(pdf_path: str, page_number: int) -> str:
    """Renderiza uma página do PDF como imagem (via Poppler, igual um leitor
    de PDF real faria) e roda OCR nela com o Tesseract."""
    try:
        images = convert_from_path(
            pdf_path, first_page=page_number, last_page=page_number, dpi=OCR_DPI
        )
        if not images:
            return ""
        return pytesseract.image_to_string(images[0], lang=OCR_LANGUAGES)
    except Exception:
        return ""


def clean_text(text: str) -> str:
    """Remove bytes NUL e outros caracteres que o Postgres não aceita."""
    return text.replace("\x00", "")


def _split_sentences(text: str) -> list[str]:
    """Quebra o texto em frases, tratando quebras de linha soltas (comuns em
    PDFs, onde uma frase é 'quebrada' visualmente por causa do layout da
    página) como espaço, mas preservando parágrafos duplos."""
    text = re.sub(r"\n{2,}", "\n\n", text)
    sentences = []
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.replace("\n", " ").strip()
        if not paragraph:
            continue
        for piece in SENTENCE_SPLIT_RE.split(paragraph):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
    return sentences


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap_sentences: int = OVERLAP_SENTENCES) -> list[str]:
    """Quebra o texto em pedaços respeitando limites de frase — nunca corta
    uma frase no meio. Empacota frases até chegar perto do tamanho alvo, com
    sobreposição de frases entre pedaços consecutivos para manter contexto."""
    sentences = _split_sentences(text)
    if not sentences:
        return []

    chunks = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        if len(sentence) > size * 2:
            # Frase absurdamente longa (raro; comum em texto de OCR sem
            # pontuação clara) — fecha o chunk atual e corta essa frase
            # sozinha, sem tentar preservar mais nada nela.
            if current:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
            for i in range(0, len(sentence), size):
                chunks.append(sentence[i : i + size])
            continue

        if current_len + len(sentence) > size and current:
            chunks.append(" ".join(current))
            current = current[-overlap_sentences:]
            current_len = sum(len(s) for s in current)

        current.append(sentence)
        current_len += len(sentence)

    if current:
        chunks.append(" ".join(current))

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


async def hybrid_search(query: str, top_k: int = 12) -> list[tuple[str, int, str, float]]:
    """Combina busca vetorial (semântica) com busca textual exata (full-text
    search nativo do Postgres) usando Reciprocal Rank Fusion (RRF).

    Resolve um ponto fraco confirmado da busca puramente vetorial: termos
    técnicos exatos e nomes próprios (ex: "Dependency Rule", "Conway's law")
    que o embedding sozinho às vezes não reconhece como relevantes, mesmo
    quando o texto exato está no corpus. A busca textual pega esses casos
    por correspondência literal de palavra, complementando o embedding."""
    RRF_K = 60
    CANDIDATE_POOL_SIZE = 30

    async with httpx.AsyncClient(timeout=30) as client:
        query_vector = await embed_async(client, query)

    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, book_title, page_number, chunk_text
                FROM book_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vector, CANDIDATE_POOL_SIZE),
            )
            vector_rows = await cur.fetchall()

            await cur.execute(
                """
                SELECT id, book_title, page_number, chunk_text
                FROM book_chunks
                WHERE search_vector @@ plainto_tsquery('simple', %s)
                ORDER BY ts_rank(search_vector, plainto_tsquery('simple', %s)) DESC
                LIMIT %s
                """,
                (query, query, CANDIDATE_POOL_SIZE),
            )
            text_rows = await cur.fetchall()

    scores: dict[int, float] = {}
    info: dict[int, tuple] = {}

    for rank, row in enumerate(vector_rows, start=1):
        row_id = row[0]
        scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
        info[row_id] = row

    for rank, row in enumerate(text_rows, start=1):
        row_id = row[0]
        scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
        info[row_id] = row

    ranked_ids = sorted(scores, key=lambda rid: scores[rid], reverse=True)[:top_k]
    return [(info[rid][1], info[rid][2], info[rid][3], scores[rid]) for rid in ranked_ids]


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
                    text = ""

                if len(text.strip()) < OCR_MIN_TEXT_LENGTH:
                    ocr_text = await asyncio.to_thread(ocr_page, path, page_num)
                    text = clean_text(ocr_text)

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