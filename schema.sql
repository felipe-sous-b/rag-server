-- Já executado manualmente no Passo 2 via terminal do serviço rag-db.
-- Este arquivo fica no repositório como documentação e para recriar
-- o banco em caso de necessidade.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS book_chunks (
    id SERIAL PRIMARY KEY,
    book_title TEXT NOT NULL,
    page_number INT,
    chunk_text TEXT NOT NULL,
    embedding VECTOR(384)
);

CREATE INDEX IF NOT EXISTS book_chunks_embedding_idx
    ON book_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
