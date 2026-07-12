# rag-server

Servidor RAG (ingestão de PDFs + busca via MCP) para o projeto `mcp-pdf` no EasyPanel.

## Variáveis de ambiente (usar estes valores exatos no serviço `rag-api`)

```
DATABASE_URL=postgresql://postgres:SUA_SENHA_DO_RAG_DB@mcp-pdf_rag-db:5432/mcp-pdf
EMBEDDING_SERVICE_URL=http://mcp-pdf_rag-embeddings:80
RAG_AUTH_TOKEN=gerar-com-o-comando-abaixo
```

Para gerar o `RAG_AUTH_TOKEN`, rode no seu computador:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Painel administrativo

Acesse `https://SEU_DOMINIO/admin` no navegador. Faça login com o valor de
`RAG_AUTH_TOKEN`. Lá você pode arrastar PDFs pra enviar, acompanhar o
progresso de processamento em tempo real e gerenciar (reprocessar/remover)
os livros já indexados.

## Como enviar este código pro GitHub

Dentro desta pasta:
```bash
git init
git add .
git commit -m "feat: servidor RAG inicial"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/rag-server.git
git push -u origin main
```
(Crie antes o repositório vazio e **privado** no site do GitHub, e troque `SEU_USUARIO` pelo seu usuário real.)
