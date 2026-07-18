# Deploy do Vende Fácil

## O detalhe importante

O app agora suporta dois bancos de dados:

- **Postgres** (recomendado em produção): basta definir a variável de ambiente `DATABASE_URL` que o app passa a usar Postgres automaticamente. É a forma mais simples de ter persistência de verdade no Render/Railway **sem precisar de disco/volume pago** — o banco gerenciado já é persistente por conta própria.
- **SQLite** (padrão, sem `DATABASE_URL`): grava em `vende_facil.sqlite` na pasta `data/`. Ótimo para uso local, mas em hosts como Render/Railway sem disco os arquivos são apagados a cada deploy/restart — por isso, em produção, use Postgres.

Imagens importadas continuam indo para `data/uploads/`. Se você usar Postgres mas não tiver disco, as imagens importadas ainda podem sumir em redeploy (o banco de dados fica seguro, só as imagens locais não). Para produção completa, combine Postgres + um disco pequeno só para uploads, ou aceite que a imagem de capa some e o produto continua com os outros dados intactos.

## Variáveis de ambiente

```env
VENDE_FACIL_LOGIN_EMAIL=admin@vendefacil.com
VENDE_FACIL_LOGIN_PASSWORD=troque-essa-senha
VENDE_FACIL_SECRET=uma-chave-grande-e-aleatoria
VENDE_FACIL_DATA_DIR=./data
# Defina para usar Postgres (produção). Deixe de fora para usar SQLite (local).
DATABASE_URL=postgresql://usuario:senha@host:5432/banco
```

## Render — com Postgres (recomendado, resolve o problema de dados sumindo)

O `render.yaml` deste projeto já vem pronto com um banco Postgres gerenciado e a variável `DATABASE_URL` ligada automaticamente ao serviço web. Para usar:

1. Suba o projeto (com o `render.yaml` atualizado) para um repositório Git (GitHub/GitLab).
2. No Render, clique em **New > Blueprint** e aponte para esse repositório.
3. O Render vai detectar o `render.yaml` e criar dois recursos: o **Web Service** (`vende-facil`) e o **Postgres** (`vende-facil-db`), já conectados via `DATABASE_URL`.
4. Quando pedir, preencha `VENDE_FACIL_LOGIN_PASSWORD` (o `VENDE_FACIL_SECRET` é gerado automaticamente).
5. Deploy. Pronto — a partir daqui, redeploys e restarts **não apagam mais seus dados**, porque eles ficam no Postgres, não no disco do serviço web.

> O `render.yaml` usa `plan: free` para o Postgres. O plano gratuito do Render expira em 30 dias (o banco é removido depois disso) — para manter os dados por mais tempo, troque para um plano pago (ex.: `basic-256mb`) antes de expirar, direto no Dashboard do Render ou editando o `render.yaml`.

Se preferir configurar manualmente pelo Dashboard em vez do Blueprint:

1. Crie um banco em **New > Postgres**.
2. Copie a **Internal Database URL** (mesma região) ou **External Database URL**.
3. No seu Web Service, vá em **Environment** e adicione `DATABASE_URL` com esse valor.
4. Redeploy o serviço.

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

Health check path: `/health` (agora retorna também qual banco está em uso, ex.: `{"db_engine": "postgres"}`, útil para conferir se a configuração pegou).

## Render — alternativa com SQLite + disco (sem Postgres)

Se preferir continuar com SQLite, adicione um **Disk** ao serviço web e monte em:

```txt
/opt/render/project/src/data
```

Depois defina `VENDE_FACIL_DATA_DIR=/opt/render/project/src/data` e **não** defina `DATABASE_URL`. Discos exigem um plano pago (não estão disponíveis no free tier de web services).

## Railway — com Postgres (recomendado)

1. No seu projeto Railway, clique em **New > Database > Add PostgreSQL**. O Railway cria o banco e já expõe uma variável `DATABASE_URL` dentro do projeto.
2. No serviço do app, vá em **Variables** e adicione uma referência à variável do Postgres (`${{Postgres.DATABASE_URL}}`) como `DATABASE_URL` — ou copie o valor direto da aba **Connect** do banco.
3. Redeploy. O `railway.toml` já está configurado com o `startCommand` e `healthcheckPath` corretos; nenhuma outra mudança é necessária.

## Railway — alternativa com SQLite + volume

Adicione um **Volume** ao serviço e monte em `/app/data`, depois defina `VENDE_FACIL_DATA_DIR=/app/data` e não defina `DATABASE_URL`.

## Docker/VPS

Com Postgres (ex.: um Postgres já rodando na sua rede ou um serviço gerenciado como Neon/Supabase):

```bash
docker build -t vende-facil .
docker run -p 5433:5433 \
  -e PORT=5433 \
  -e VENDE_FACIL_LOGIN_EMAIL=admin@vendefacil.com \
  -e VENDE_FACIL_LOGIN_PASSWORD='troque-essa-senha' \
  -e VENDE_FACIL_SECRET='uma-chave-grande' \
  -e DATABASE_URL='postgresql://usuario:senha@host:5432/banco' \
  vende-facil
```

Com SQLite (uso local/VPS único, sem Postgres):

```bash
docker build -t vende-facil .
docker run -p 5433:5433 \
  -e PORT=5433 \
  -e VENDE_FACIL_LOGIN_EMAIL=admin@vendefacil.com \
  -e VENDE_FACIL_LOGIN_PASSWORD='troque-essa-senha' \
  -e VENDE_FACIL_SECRET='uma-chave-grande' \
  -e VENDE_FACIL_DATA_DIR=/app/data \
  -v vende_facil_data:/app/data \
  vende-facil
```


## v10 - Importação OLX com fallback 403

- O importador OLX agora tenta buscar a página com headers mais parecidos com navegador.
- Se a OLX responder HTTP 403/bloqueio, o app abre o **modo manual assistido** em vez de travar.
- No modo manual assistido, o link da OLX já fica preservado e você preenche título, preço, descrição e imagem.
- Ao salvar produto com URL pública de imagem, o app tenta baixar a capa para `data/uploads/`.

## v11 - Banco de dados persistente com Postgres

- O app agora detecta a variável `DATABASE_URL` e usa Postgres automaticamente quando ela existe, mantendo compatibilidade total com SQLite quando ela não existe.
- Isso resolve o problema de dados sumirem em redeploys em hosts sem disco persistente (como o plano gratuito do Render/Railway): o Postgres gerenciado já é persistente por natureza.
- Backup/Restore (`/export` e `/import-data`) funcionam do mesmo jeito nos dois bancos.

