# Vende Fácil — MVP Local

App/Web App híbrido em formato **PWA local** para vendedores casuais organizarem produtos, anúncios, leads, vendas, recebíveis e matches produto ↔ lead.

A ideia é ser um **mini-CRM disfarçado**, sem cara de ERP triste.

## O que já vem pronto

- Dashboard com produtos, anúncios, leads, recebidos e valores a receber.
- Cadastro de produtos com preço, preço mínimo, quantidade, status, temperatura e tags.
- Importação da OLX também dentro da tela **Novo produto**: cola o link, puxa os dados, limpa a descrição e salva no mesmo fluxo.
- Cadastro de leads com WhatsApp, cidade, orçamento, tags, notas e pontuação.
- Cadastro de links de anúncios por plataforma: OLX, Mercado Livre, Instagram, WhatsApp etc.
- Importação de anúncio da OLX por link: cola a URL, revisa a prévia e salva como produto + link.
- Importação por texto colado: quando a OLX bloqueia com 403, cole o texto do anúncio e o app separa título, preço, descrição, cidade, tags e capa quando possível.
- Registro de vendas e recebíveis, com comprador cadastrado ou comprador avulso.
- Calculadora de preço considerando custo, lucro, taxa, frete e desconto.
- Algoritmo simples de match produto ↔ lead usando tags, orçamento e pontuação.
- Geração local simples de texto melhor para anúncio.
- Backup completo em JSON: produtos, leads, anúncios, vendas, configurações, login e imagens importadas.
- Interface mobile-first, colorida e instalável como PWA pelo navegador.
- Banco local SQLite em `data/vende_facil.sqlite`.
- Começa sem dados de exemplo: você inicia do zero mesmo.
- Auto-preenchimento de tags: as tags que você usa em produtos/leads aparecem como sugestões nos próximos cadastros.
- Alerta no topo quando houver matches quentes entre produtos e leads.
- Limpeza automática de descrições importadas: remove `<br>`, tags HTML e entidades estranhas.
- Importação da imagem de capa da OLX para pasta local persistente `data/uploads/`, quando a página permite.

## Como rodar no Windows

1. Extraia o `.zip`.
2. Entre na pasta `vende_facil_mvp`.
3. Dê dois cliques em:

```bat
run.bat
```

4. O app abre em:

```txt
http://127.0.0.1:5433
```

Para parar, volte no terminal e pressione `CTRL + C`.

## Começar do zero

Esta versão não vem com produtos, leads ou anúncios de exemplo. No primeiro uso, o app cria um banco vazio.

Se você já rodou uma versão antiga e quer apagar tudo, use:

```bat
reset_data.bat
```

Ele apaga somente o banco local `data/vende_facil.sqlite` e o arquivo de exportação, se existir.

## Como rodar manualmente

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Depois abra:

```txt
http://127.0.0.1:5433
```

## Como funciona o auto-preenchimento de tags

Quando você cadastra um produto ou lead com tags, por exemplo:

```txt
freezer, cozinha, comércio, campinas
```

O app salva essas tags no banco. Depois, nos próximos formulários de produto/lead, elas aparecem como botões de sugestão. Ao clicar em uma sugestão, ela é adicionada ao campo de tags sem duplicar.

As sugestões são ordenadas por frequência: tag usada mais vezes aparece primeiro. É memória de vendedor, não magia negra.

## Alerta de match no topo

Quando o app encontra matches com pontuação de 70% ou mais, ele mostra um alerta no topo da tela. O alerta aponta para a tela de Match e destaca o melhor cruzamento produto ↔ lead.

## Estrutura

```txt
vende_facil_mvp/
├─ app.py
├─ requirements.txt
├─ run.bat
├─ install_only.bat
├─ reset_data.bat
├─ README.md
├─ data/
│  └─ vende_facil.sqlite   # criado automaticamente
├─ templates/
│  ├─ base.html
│  ├─ dashboard.html
│  ├─ products.html
│  ├─ product_form.html
│  ├─ leads.html
│  ├─ lead_form.html
│  ├─ listings.html
│  ├─ olx_import.html
│  ├─ text_import.html
│  ├─ matches.html
│  ├─ sales.html
│  ├─ calculator.html
│  ├─ suggestion.html
│  └─ about.html
└─ static/
   ├─ css/app.css
   ├─ js/app.js
   ├─ manifest.json
   └─ sw.js
```

## Como o match funciona

O match calcula uma pontuação entre produto e lead:

- Tags iguais aumentam a pontuação.
- Produto dentro do orçamento do lead ganha pontos.
- Lead com pontuação alta ganha peso.
- Produto marcado como quente ganha peso.
- Produto acima do orçamento perde pontos.

Faixas:

- 0–39%: match fraco.
- 40–69%: talvez valha mandar.
- 70–84%: bom lead.
- 85–100%: chama agora.

## Próximas melhorias recomendadas

1. Upload real de fotos.
2. Múltiplos usuários e permissões por perfil.
3. Importação por link de Mercado Livre/Facebook Marketplace e outros canais.
4. Integração com IA para título, descrição e preço.
5. Comparador de preço OLX/Mercado Livre.
6. Build mobile com Flutter ou empacotamento desktop com Tauri/Electron.
7. Sincronização em nuvem com Supabase/PostgreSQL.

## Observação importante

Este MVP **não publica automaticamente** em OLX, Mercado Livre, Instagram ou WhatsApp. Ele salva links, organiza informações e gera textos/mensagens. Isso evita dependência inicial de APIs e deixa o produto validável mais rápido.

Tradução: primeiro faz vender; depois coloca motor turbo.

## Atualizações v3

- Removida a área **Missão da semana** do painel.
- Adicionado menu **Configurações** no topo.
- Agora é possível configurar:
  - nome do app;
  - emoji/marca;
  - tema visual;
  - cidade/região padrão;
  - pontuação mínima para alerta de match;
  - ligar/desligar alerta de match no topo;
  - ligar/desligar textos mais descontraídos.
- O alerta de match no topo respeita a pontuação definida nas Configurações.
- O app continua iniciando sem dados de exemplo.



## Importar anúncio da OLX

No menu **🧲 Importar OLX**, cole um link da OLX e clique em **Importar**.

O app tenta preencher automaticamente:

- nome do produto;
- descrição;
- preço;
- categoria aproximada;
- imagem/capa principal;
- tags sugeridas;
- link original do anúncio.

Depois ele mostra uma prévia editável. Ao clicar em **Salvar produto + link OLX**, o app cria o produto e também salva o link na área **Anúncios**.

Observação honesta: a importação lê metadados públicos da página. Se a OLX bloquear a página, mudar o HTML ou esconder algum campo, o app mostra aviso e deixa você completar manualmente. Quando encontra capa, tenta baixar a imagem para `data/uploads/`; se não conseguir, mantém a URL original. É importador assistido, não milagre com crachá.

## Atualizações v4

- Adicionado menu **🧲 Importar OLX**.
- Importação de anúncio da OLX por link.
- Pré-preenchimento de título, preço, descrição, categoria, imagem e tags quando disponíveis.
- Ao salvar uma importação, o link da OLX é registrado automaticamente em **Anúncios**.
- Adicionadas dependências `requests` e `beautifulsoup4` para leitura de metadados públicos.
- Atualizado cache do PWA para `vende-facil-v4`.


## Atualizações v5

- A opção de **importar anúncio da OLX** agora aparece também dentro da tela **+ Novo produto**.
- Fluxo novo: **Produtos → + Novo produto → colar link OLX → Puxar dados → revisar → salvar**.
- O formulário de produto é preenchido com título, preço, descrição, categoria, imagem e tags quando a página permite.
- Ao salvar um produto importado por essa tela, o app também registra automaticamente o link em **Anúncios**.
- O menu **🧲 Importar OLX** continua existindo como importador completo/separado.
- Atualizado cache do PWA para `vende-facil-v5`.


## Novidades da v6

- Opção em Configurações para ativar/desativar som quando houver match no topo.
- Botão para testar o som do match.
- Som curto gerado pelo navegador, sem arquivo externo.

## Login único

A partir da v7, o app abre primeiro uma tela de login e protege todas as telas internas por sessão.

Credenciais padrão:

```txt
Usuário: admin@vendefacil.com
Senha: Strongeta@1990
```

Não existe cadastro nesta versão. É um acesso único/local.

Para trocar sem editar código, você pode definir variáveis de ambiente antes de rodar:

```bat
set VENDE_FACIL_LOGIN_EMAIL=seu-email@exemplo.com
set VENDE_FACIL_LOGIN_PASSWORD=sua-senha-forte
set VENDE_FACIL_SECRET=uma-chave-secreta-grande
python app.py
```

## Novidades da v7

- Adicionada tela de login.
- Sem cadastro: apenas usuário/senha únicos.
- Todas as rotas principais agora exigem sessão logada.
- Adicionado botão **Sair** no topo.
- Mantido banco zerado no zip, sem dados de exemplo.

## Deploy em servidor

Esta versão já vem pronta para deploy com Gunicorn:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

Arquivos incluídos para produção:

- `Procfile`
- `render.yaml`
- `railway.toml`
- `Dockerfile`
- `.env.example`
- `DEPLOY.md`
- rota `/health`

Importante: para não perder dados, configure volume/disco persistente e a variável `VENDE_FACIL_DATA_DIR`.


## Novidades da v9

- Corrigida a importação de descrições da OLX que vinham com `<br>` e outras marcas HTML aparecendo no texto.
- Agora a descrição importada é salva com quebras de linha reais, limpa e legível.
- Ao salvar/editar produto, o app também limpa descrições coladas manualmente com HTML.
- A importação da OLX agora tenta baixar a imagem de capa para `data/uploads/` e usa uma URL local `/uploads/...`.
- Se a capa não puder ser baixada por bloqueio/tamanho/resposta inválida, o app mantém a URL original e mostra aviso.
- Atualizado cache do PWA para `vende-facil-v9`.


## v10 - Importação OLX com fallback 403

- O importador OLX agora tenta buscar a página com headers mais parecidos com navegador.
- Se a OLX responder HTTP 403/bloqueio, o app abre o **modo manual assistido** em vez de travar.
- No modo manual assistido, o link da OLX já fica preservado e você preenche título, preço, descrição e imagem.
- Ao salvar produto com URL pública de imagem, o app tenta baixar a capa para `data/uploads/`.


## v11 - Importação por texto colado

- Adicionado menu **📝 Importar texto**.
- Adicionada opção de importar por texto dentro da tela **+ Novo produto**.
- Fluxo recomendado quando a OLX dá HTTP 403 no servidor:

```txt
Abra o anúncio no navegador → copie título/preço/cidade/descrição → cole no app → revise → salve
```

- O app tenta detectar automaticamente:
  - título;
  - preço;
  - cidade/localização;
  - descrição limpa;
  - categoria aproximada;
  - tags;
  - link original da OLX, se estiver no texto;
  - URL de imagem, se colada.
- Se você informar uma URL pública da capa, o app tenta baixar e salvar em `data/uploads/`.
- Atualizado cache do PWA para `vende-facil-v11`.


## v12 - Match corrigido e diagnóstico

- Match ficou mais flexível e agora considera tags explícitas, título, categoria, descrição do produto, cidade/notas/tags do lead, orçamento, pontuação e status.
- Normalização sem acentos: `comércio` e `comercio` agora conversam entre si.
- Tela de Match ganhou diagnóstico: produtos disponíveis, leads, cruzamentos lidos e termos usados no cálculo.
- Filtro de pontuação na tela de Match: dá para baixar para `0%` e ver por que algo não está casando.
- Cache do PWA atualizado para `vende-facil-v12`.

## v13 - Painel financeiro, venda rápida e importação em lote

- Painel agora mostra **Expectativa de lucro**: soma do preço cadastrado dos produtos ainda não vendidos, multiplicado pela quantidade.
- Painel ganhou estatísticas financeiras extras:
  - valor em anúncios ativos;
  - recebido;
  - a receber;
  - total vendido;
  - ticket médio;
  - mínimo seguro;
  - margem para desconto;
  - links ativos e vendas pendentes.
- Cards de produto ganharam botão **Venda rápida**:
  - registra uma venda pelo preço cadastrado;
  - marca pagamento como recebido;
  - diminui a quantidade;
  - se a quantidade chegar a zero, marca o produto como vendido.
- Bordas visuais reduzidas para cerca de `10px`, deixando o app menos circular e mais limpo.
- Adicionada tela **🧺 Importar OLX em lote**:
  - cole vários links da OLX, um por linha;
  - o app tenta importar e salvar todos automaticamente;
  - links bloqueados pela OLX aparecem no relatório para você tratar pelo importador por texto.
- Cache do PWA atualizado para `vende-facil-v13`.


## v14 - Login editável, backup completo e venda rápida com comprador

- Configurações agora permitem alterar o usuário/e-mail e a senha do login único pela interface.
- A senha atual não aparece no formulário; deixe o campo de nova senha vazio para manter a senha existente.
- Backup completo em JSON agora exporta:
  - produtos;
  - leads;
  - anúncios/listings;
  - vendas;
  - configurações;
  - login salvo;
  - imagens importadas em `data/uploads/` quando forem pequenas o suficiente.
- Configurações ganharam importação de backup com confirmação. A importação substitui os dados atuais e restaura o login do backup.
- Botão **Venda rápida** agora abre um modal:
  - permite vender para lead cadastrado via dropdown;
  - ou registrar comprador avulso com nome, WhatsApp e origem, todos opcionais;
  - permite ajustar valor, pagamento, data, plataforma e observação antes de salvar.
- Tela **💸 Vendas e recebíveis** melhorada com cards financeiros, resumo de vendas com lead/comprador avulso e formulário mais completo.
- Cache do PWA atualizado para `vende-facil-v14`.

## Novidades da v15

- O menu **🎯 Match** agora mostra uma bolinha com a quantidade de matches quentes, igual notificação de app.
- Produtos únicos vendidos pela **Venda rápida** ou pela tela **Vendas** agora saem da lista principal automaticamente.
- Produtos únicos vendidos entram em **🧊 Quarentena** por 30 dias.
- Na quarentena, você pode:
  - trazer o produto de volta para a lista;
  - apagar de vez.
- Se o prazo de 30 dias passar, o botão de restaurar fica bloqueado e resta apagar definitivamente.
- Produtos em quarentena não entram nos matches nem na lista principal de produtos.
- O backup JSON agora inclui também o estado de quarentena dos produtos.

## v16 — Painel e calculadora

- Painel reorganizado com grid responsivo para evitar cards soltos, como o card de Leads quebrando sozinho na linha.
- Calculadora de preço ganhou dropdown para preencher os campos usando produtos e vendas já cadastrados.
- Cache do PWA atualizado para v16.
