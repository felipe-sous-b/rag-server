"""
Script de ingestão: lê todos os PDFs de uma pasta, quebra em pedaços (chunks),
gera o vetor de cada pedaço e salva no Postgres.

Uso:
    python ingest.py /caminho/para/pasta/com/pdfs
"""
import os
import sys

import httpx
import psycopg
from pypdf import PdfReader

DATABASE_URL = os.environ["DATABASE_URL"]
EMBEDDING_URL = os.environ["EMBEDDING_SERVICE_URL"]
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
MIN_CHUNK_LENGTH = 50


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Quebra o texto em pedaços de tamanho fixo, com sobreposição entre eles
    para não cortar uma ideia no meio."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


def embed(text: str) -> list[float]:
    """Chama o serviço de embeddings e devolve o vetor do texto."""
    resp = httpx.post(f"{EMBEDDING_URL}/embed", json={"inputs": text}, timeout=60)
    resp.raise_for_status()
    return resp.json()[0]


def ingest_pdf(path: str, conn: psycopg.Connection) -> None:
    title = os.path.splitext(os.path.basename(path))[0]
    reader = PdfReader(path)
    total_chunks = 0

    with conn.cursor() as cur:
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if not text.strip():
                continue

            for chunk in chunk_text(text):
                if len(chunk.strip()) < MIN_CHUNK_LENGTH:
                    continue
                vector = embed(chunk)
                cur.execute(
                    """
                    INSERT INTO book_chunks (book_title, page_number, chunk_text, embedding)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (title, page_num, chunk, vector),
                )
                total_chunks += 1

        conn.commit()

    print(f"Ingerido: {title} ({total_chunks} trechos)")


def main() -> None:
    folder = sys.argv[1] if len(sys.argv) > 1 else "/books"

    if not os.path.isdir(folder):
        print(f"Pasta não encontrada: {folder}")
        sys.exit(1)

    with psycopg.connect(DATABASE_URL) as conn:
        for filename in sorted(os.listdir(folder)):
            if filename.lower().endswith(".pdf"):
                ingest_pdf(os.path.join(folder, filename), conn)

    print("Ingestão concluída.")


if __name__ == "__main__":
    main()
