"""
Servidor MCP remoto: expõe a ferramenta `search_books`, que busca nos livros
os trechos mais relevantes para uma pergunta.
"""
import os

import httpx
import psycopg
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
EMBEDDING_URL = os.environ["EMBEDDING_SERVICE_URL"]
AUTH_TOKEN = os.environ["RAG_AUTH_TOKEN"]

mcp = FastMCP("book-rag")


async def embed_text(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{EMBEDDING_URL}/embed", json={"inputs": text})
        resp.raise_for_status()
        return resp.json()[0]


@mcp.tool()
async def search_books(query: str, top_k: int = 5) -> str:
    """Busca trechos relevantes na base de livros técnicos de engenharia e
    arquitetura de software (Clean Architecture, Design Patterns, DDIA,
    Accelerate, Clean Code, etc.). Use antes de decidir uma prática de
    arquitetura, padrão de projeto ou técnica de implementação."""
    query_vector = await embed_text(query)

    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT book_title, page_number, chunk_text,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM book_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vector, query_vector, top_k),
            )
            rows = await cur.fetchall()

    if not rows:
        return "Nenhum trecho relevante encontrado na base de livros."

    partes = []
    for title, page, text, similarity in rows:
        partes.append(f"[{title}, p.{page}] (similaridade: {similarity:.2f})\n{text}")

    return "\n\n---\n\n".join(partes)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Exige o cabeçalho `Authorization: Bearer <token>` em toda requisição,
    exceto no endpoint de saúde (/health)."""

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/health"):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {AUTH_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        return await call_next(request)


# App ASGI exposto via SSE (transporte remoto do MCP)
app = mcp.sse_app()
app.add_middleware(BearerAuthMiddleware)


@app.route("/health")
async def health(request):
    return JSONResponse({"status": "ok"})
