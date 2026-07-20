"""
Servidor MCP remoto via Streamable HTTP — usado por clientes modernos como
o Antigravity. Roda como um SEGUNDO serviço, separado do server.py (que
continua servindo o Claude via SSE, sem nenhuma alteração).

Reaproveita a mesma ferramenta de busca e o mesmo banco (via ingestion.py),
então os dois servidores enxergam a mesma biblioteca de livros.
"""
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from ingestion import hybrid_search

AUTH_TOKEN = os.environ["RAG_AUTH_TOKEN"]

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
    princípios) tendem a funcionar bem.

    IMPORTANTE — corpus bilíngue (PT/EN): vários livros estão em inglês.
    Se a busca em português não trouxer o livro esperado (comum com nomes
    próprios e termos técnicos consagrados em inglês, como "Dependency
    Rule", "Conway's law", "Single Responsibility Principle"), chame esta
    ferramenta de novo com a mesma pergunta traduzida pro inglês antes de
    concluir que o conteúdo não existe na base — combine os resultados das
    duas chamadas na sua resposta.

    Retorna vários candidatos por chamada — avalie e cite apenas os que de
    fato respondem à pergunta."""
    results = await hybrid_search(query, top_k=top_k)

    if not results:
        return "Nenhum trecho relevante encontrado na base de livros."

    partes = []
    for title, page, text, score in results:
        partes.append(f"[{title}, p.{page}] (score: {score:.4f})\n{text}")

    return "\n\n---\n\n".join(partes)


class BearerAuthMiddleware:
    """Igual à do server.py — só sem a exceção de /messages, porque o
    Streamable HTTP não usa um endpoint separado pra mensagens; é tudo
    em /mcp."""

    PUBLIC_PATHS = {"/health"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path in self.PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        headers = dict(scope["headers"])
        auth_header = headers.get(b"authorization", b"").decode()

        if auth_header == f"Bearer {AUTH_TOKEN}":
            return await self.app(scope, receive, send)

        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        return await response(scope, receive, send)


async def health(request):
    return JSONResponse({"status": "ok"})


# App ASGI exposto via Streamable HTTP (endpoint único /mcp)
app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)
app.router.routes.append(Route("/health", health))