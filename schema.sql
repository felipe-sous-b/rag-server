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

-- Adicionado para suportar busca híbrida (full-text + vetor), que resolve
-- casos onde a busca puramente vetorial falha em achar termos técnicos
-- exatos e nomes próprios (confirmado em testes: "Dependency Rule",
-- "Conway's law"). A coluna é gerada automaticamente pelo Postgres a partir
-- de chunk_text, sem precisar de mudança no código de ingestão.
ALTER TABLE book_chunks
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', chunk_text)) STORED;

CREATE INDEX IF NOT EXISTS book_chunks_search_idx
    ON book_chunks USING GIN (search_vector);

-- Adicionado para suportar o painel administrativo web (rastreio de progresso).
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id SERIAL PRIMARY KEY,
    book_title TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_chunks INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);