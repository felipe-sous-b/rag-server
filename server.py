"""
Servidor MCP remoto + painel administrativo web.

- Ferramenta MCP `search_books`: usada pelo Claude para buscar nos livros.
- Painel web em /admin: upload de PDFs, acompanhamento de progresso e
  gestão da biblioteca (protegido pelo mesmo RAG_AUTH_TOKEN).
"""
import asyncio
import os
import pathlib
import urllib.parse

import httpx
import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from ingestion import DATABASE_URL, EMBEDDING_URL, embed_async, hybrid_search, process_book_async

AUTH_TOKEN = os.environ["RAG_AUTH_TOKEN"]
BOOKS_DIR = os.environ.get("BOOKS_DIR", "/books")
ADMIN_HTML_PATH = pathlib.Path(__file__).parent / "admin.html"

mcp = FastMCP(
    "book-rag",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def search_books(query: str, top_k: int = 12) -> str:
    """Busca trechos relevantes na base de livros técnicos de engenharia e
    arquitetura de software (Clean Architecture, Design Patterns, DDIA,
    Accelerate, Clean Code, etc.). Use antes de decidir uma prática de
    arquitetura, padrão de projeto ou técnica de implementação. Combina
    busca semântica com busca textual exata, então tanto perguntas
    conceituais quanto termos técnicos específicos (nomes de padrões, leis,
    princípios) tendem a funcionar bem. Retorna vários candidatos por
    chamada — avalie e cite apenas os que de fato respondem à pergunta."""
    results = await hybrid_search(query, top_k=top_k)

    if not results:
        return "Nenhum trecho relevante encontrado na base de livros."

    partes = []
    for title, page, text, score in results:
        partes.append(f"[{title}, p.{page}] (score: {score:.4f})\n{text}")

    return "\n\n---\n\n".join(partes)


class BearerAuthMiddleware:
    """Exige `Authorization: Bearer <token>` (ou `?token=` na URL) em toda
    requisição, exceto no endpoint de saúde, na página do painel e no
    endpoint /messages (protegido pelo session_id, gerado só depois que o
    /sse inicial já validou o token).

    Implementado como middleware ASGI puro (não BaseHTTPMiddleware) porque
    o BaseHTTPMiddleware do Starlette tem um bug conhecido com respostas de
    streaming de longa duração como SSE."""

    PUBLIC_PATHS = {"/health", "/admin"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path in self.PUBLIC_PATHS or path.startswith("/messages"):
            return await self.app(scope, receive, send)

        headers = dict(scope["headers"])
        auth_header = headers.get(b"authorization", b"").decode()
        query_params = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
        query_token = query_params.get("token", [""])[0]

        if auth_header == f"Bearer {AUTH_TOKEN}" or query_token == AUTH_TOKEN:
            return await self.app(scope, receive, send)

        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        return await response(scope, receive, send)


async def health(request):
    return JSONResponse({"status": "ok"})


async def admin_page(request):
    return HTMLResponse(ADMIN_HTML_PATH.read_text(encoding="utf-8"))


async def admin_status(request):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT id, book_title, filename, status, total_chunks, error_message, updated_at
                   FROM ingest_jobs ORDER BY updated_at DESC LIMIT 100"""
            )
            rows = await cur.fetchall()

    jobs = [
        {
            "id": r[0],
            "book_title": r[1],
            "filename": r[2],
            "status": r[3],
            "total_chunks": r[4],
            "error_message": r[5],
            "updated_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]
    return JSONResponse({"jobs": jobs})


async def admin_books(request):
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT book_title, COUNT(*), MAX(page_number)
                   FROM book_chunks GROUP BY book_title ORDER BY book_title"""
            )
            rows = await cur.fetchall()

    books = [{"title": r[0], "chunks": r[1], "pages": r[2]} for r in rows]
    return JSONResponse({"books": books})


def _safe_filename(name: str) -> str:
    name = os.path.basename(name)
    return name.replace("/", "_").replace("\\", "_")


async def _create_job(filename: str, title: str) -> int:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO ingest_jobs (book_title, filename, status)
                   VALUES (%s, %s, 'pending') RETURNING id""",
                (title, filename),
            )
            row = await cur.fetchone()
        await conn.commit()
        return row[0]


async def admin_upload(request):
    form = await request.form()
    files = form.getlist("files")
    if not files:
        return JSONResponse({"error": "nenhum arquivo enviado"}, status_code=400)

    os.makedirs(BOOKS_DIR, exist_ok=True)
    created = []
    for upload in files:
        filename = _safe_filename(upload.filename)
        if not filename.lower().endswith(".pdf"):
            continue
        dest_path = os.path.join(BOOKS_DIR, filename)
        content = await upload.read()
        with open(dest_path, "wb") as f:
            f.write(content)

        title = os.path.splitext(filename)[0]
        job_id = await _create_job(filename, title)
        asyncio.create_task(process_book_async(dest_path, job_id, force=True))
        created.append({"filename": filename, "job_id": job_id})

    return JSONResponse({"created": created})


async def admin_reprocess(request):
    body = await request.json()
    filename = _safe_filename(body.get("filename", ""))
    path = os.path.join(BOOKS_DIR, filename)
    if not os.path.isfile(path):
        return JSONResponse({"error": "arquivo não encontrado no servidor"}, status_code=404)

    title = os.path.splitext(filename)[0]
    job_id = await _create_job(filename, title)
    asyncio.create_task(process_book_async(path, job_id, force=True))
    return JSONResponse({"job_id": job_id})


async def admin_delete_book(request):
    title = request.path_params["title"]
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM book_chunks WHERE book_title = %s", (title,))
        await conn.commit()
    return JSONResponse({"deleted": title})


# App ASGI exposto via SSE (transporte remoto do MCP) + rotas do painel admin
app = mcp.sse_app()
app.add_middleware(BearerAuthMiddleware)
app.router.routes.append(Route("/health", health))
app.router.routes.append(Route("/admin", admin_page))
app.router.routes.append(Route("/admin/api/status", admin_status))
app.router.routes.append(Route("/admin/api/books", admin_books))
app.router.routes.append(Route("/admin/api/upload", admin_upload, methods=["POST"]))
app.router.routes.append(Route("/admin/api/reprocess", admin_reprocess, methods=["POST"]))
app.router.routes.append(Route("/admin/api/books/{title}", admin_delete_book, methods=["DELETE"]))