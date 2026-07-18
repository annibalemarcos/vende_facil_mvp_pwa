from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import secrets
from bs4 import BeautifulSoup

import psycopg2
import psycopg2.extras

from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("VENDE_FACIL_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
DB_PATH = DATA_DIR / "vende_facil.sqlite"
EXPORT_PATH = DATA_DIR / "vende_facil_export.json"
UPLOAD_DIR = DATA_DIR / "uploads"

# Se DATABASE_URL estiver definida (Render/Railway/Neon/Supabase etc. fornecem essa
# variável automaticamente ao criar um banco Postgres gerenciado), o app usa Postgres
# como banco de dados persistente de verdade — sem depender de disco/volume no host.
# Sem essa variável, o app continua funcionando com SQLite em arquivo local (ótimo
# para uso local / MVP em uma máquina só).
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
USE_POSTGRES = bool(DATABASE_URL)

# SQLite trata MAX(a, b) com 2+ argumentos como função escalar (maior valor entre eles).
# Postgres não faz isso — MAX ali é só agregação. O equivalente escalar no Postgres é GREATEST.
SCALAR_MAX = "GREATEST" if USE_POSTGRES else "MAX"

app = Flask(__name__)
app.secret_key = os.environ.get("VENDE_FACIL_SECRET", "dev-secret-change-me")

DEFAULT_LOGIN_EMAIL = os.environ.get("VENDE_FACIL_LOGIN_EMAIL", "admin@vendefacil.com")
DEFAULT_LOGIN_PASSWORD = os.environ.get("VENDE_FACIL_LOGIN_PASSWORD", "Strongeta@1990")

STATUSES = ["rascunho", "anunciado", "reservado", "vendido", "parado"]
TEMPERATURES = ["🔥 quente", "😐 morno", "❄️ frio"]
PLATFORMS = ["OLX", "Mercado Livre", "Instagram", "WhatsApp", "Facebook", "Outro"]
PAYMENT_STATUSES = ["pendente", "recebido", "parcial"]
OLX_TIMEOUT_SECONDS = 12
MAX_IMPORT_IMAGE_BYTES = 8 * 1024 * 1024
QUARANTINE_DAYS = 30
OLX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

THEMES = {
    "roxo": {"label": "Roxo elétrico", "primary": "#7c3aed", "secondary": "#ec4899", "soft": "#f1e9ff", "bg": "#faf7ff"},
    "verde": {"label": "Verde Pix", "primary": "#059669", "secondary": "#10b981", "soft": "#dcfce7", "bg": "#f7fff9"},
    "azul": {"label": "Azul vitrine", "primary": "#2563eb", "secondary": "#06b6d4", "soft": "#dbeafe", "bg": "#f7fbff"},
    "laranja": {"label": "Laranja feira", "primary": "#ea580c", "secondary": "#f59e0b", "soft": "#ffedd5", "bg": "#fffaf3"},
    "grafite": {"label": "Grafite limpo", "primary": "#111827", "secondary": "#475569", "soft": "#e5e7eb", "bg": "#f8fafc"},
}

DEFAULT_SETTINGS = {
    "app_name": "Vende Fácil",
    "brand_emoji": "🛒",
    "theme": "roxo",
    "default_city": "Campinas/SP",
    "match_threshold": "70",
    "show_match_alert": "1",
    "match_sound": "1",
    "fun_mode": "1",
    "login_email": DEFAULT_LOGIN_EMAIL,
    "login_password": DEFAULT_LOGIN_PASSWORD,
}


def get_db():
    """Retorna a conexão do banco para esta request (Postgres se DATABASE_URL
    estiver definida; SQLite local caso contrário). A conexão fica em `g` e é
    reaproveitada durante toda a requisição, fechada no teardown."""
    if "db" not in g:
        if USE_POSTGRES:
            g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _adapt_sql(sql: str) -> str:
    """Converte os placeholders estilo SQLite ('?') para o estilo psycopg2 ('%s')
    quando o app está rodando em Postgres. Todo o resto do código continua
    escrevendo SQL com '?', como sempre."""
    return sql.replace("?", "%s") if USE_POSTGRES else sql


def query(sql: str, args: tuple[Any, ...] = (), one: bool = False):
    db = get_db()
    cur = db.cursor()
    # Sem args, não passamos uma tupla vazia ao psycopg2: ele faz substituição de
    # placeholders no estilo %-format mesmo sem parâmetros, e isso quebra queries
    # com '%' literal (ex: LIKE 'texto%'). None desliga essa substituição.
    cur.execute(_adapt_sql(sql), args if (args or not USE_POSTGRES) else None)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, args: tuple[Any, ...] = (), returning: str | None = None) -> int | None:
    """Executa um INSERT/UPDATE/DELETE e comita.

    `returning`: nome da coluna de id a devolver (só usado no caminho Postgres,
    via `RETURNING`, já que lá não existe `lastrowid`). No SQLite o lastrowid
    sempre é devolvido normalmente, independente desse parâmetro.
    """
    db = get_db()
    cur = db.cursor()
    sql_to_run = _adapt_sql(sql)
    if USE_POSTGRES and returning and "RETURNING" not in sql_to_run.upper():
        sql_to_run = sql_to_run.rstrip().rstrip(";") + f" RETURNING {returning}"
    cur.execute(sql_to_run, args if (args or not USE_POSTGRES) else None)
    if USE_POSTGRES:
        last_id = cur.fetchone()[returning] if returning else None
    else:
        last_id = cur.lastrowid
    db.commit()
    cur.close()
    return last_id


def table_columns(db, table: str) -> list[str]:
    """Lista as colunas de uma tabela, funcionando tanto em SQLite (PRAGMA)
    quanto em Postgres (information_schema)."""
    if USE_POSTGRES:
        cur = db.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        cols = [row["column_name"] for row in cur.fetchall()]
        cur.close()
        return cols
    cur = db.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def normalize_match_text(value: str | None) -> str:
    """Normaliza texto para comparação: minúsculo, sem acento e sem HTML."""
    if not value:
        return ""
    text = html_to_text(str(value), keep_lines=False)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    tags: list[str] = []
    for raw in value.replace(";", ",").split(","):
        tag = normalize_match_text(raw)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


MATCH_STOPWORDS = {
    "para", "com", "sem", "uma", "uns", "das", "dos", "por", "que", "olx",
    "produto", "anuncio", "anuncios", "novo", "nova", "usado", "usada", "vendo",
    "venda", "retirada", "entrega", "funcionando", "normalmente", "fotos", "foto",
    "video", "chat", "caso", "quer", "quero", "tenho", "tem", "esta", "esse",
    "essa", "dele", "dela", "mais", "menos", "muito", "pouco", "real", "reais",
    "campinas", "grande", "sao", "sp", "brasil", "https", "www", "html", "br",
}


def match_terms(*values: str | None) -> set[str]:
    """Extrai termos úteis para match, inclusive palavras de título/descrição/notas."""
    terms: set[str] = set()
    for value in values:
        text = normalize_match_text(value)
        if not text:
            continue
        for token in re.findall(r"[a-z0-9]{3,}", text):
            if token not in MATCH_STOPWORDS:
                terms.add(token)
    return terms


def money(value: Any) -> str:
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


app.jinja_env.filters["money"] = money


def html_to_text(value: str | None, keep_lines: bool = False) -> str:
    """Converte texto/HTML importado em texto limpo.

    A OLX às vezes coloca <br>, &nbsp; e até trechos HTML dentro dos metadados.
    O app deve guardar descrição como texto humano, não como sopa de tags.
    """
    if not value:
        return ""

    text = html.unescape(str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")

    if keep_lines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        cleaned: list[str] = []
        blank = False
        for line in lines:
            if line:
                cleaned.append(line)
                blank = False
            elif cleaned and not blank:
                cleaned.append("")
                blank = True
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        return "\n".join(cleaned).strip()

    return re.sub(r"\s+", " ", text).strip()


def compact_spaces(value: str | None) -> str:
    return html_to_text(value, keep_lines=False)


def clean_description(value: str | None) -> str:
    return html_to_text(value, keep_lines=True)


def meta_content(soup: BeautifulSoup, *names: str, clean: bool = True) -> str:
    """Busca conteúdo em og:, twitter: e meta name comuns."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            content = html.unescape(str(tag.get("content") or "")).strip()
            return compact_spaces(content) if clean else content
    return ""


def first_jsonld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            graph = value.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            collect(json.loads(raw))
        except Exception:
            continue
    return objects


def deep_values(value: Any, keys: set[str], limit: int = 40) -> list[Any]:
    found: list[Any] = []

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).lower().replace("_", "-")
                if normalized in keys and child not in (None, "", [], {}):
                    found.append(child)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def page_json_objects(soup: BeautifulSoup) -> list[Any]:
    objects: list[Any] = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw or len(raw) < 30:
            continue
        stripped = raw.strip()
        candidates = []
        if stripped.startswith("{") or stripped.startswith("["):
            candidates.append(stripped)
        # Fallback para scripts do tipo window.__STATE__ = {...};
        match = re.search(r"=\s*(\{.*\})\s*;?\s*$", stripped, flags=re.DOTALL)
        if match:
            candidates.append(match.group(1))
        for candidate in candidates[:2]:
            try:
                objects.append(json.loads(candidate))
                break
            except Exception:
                continue
    return objects


def first_text_value(values: list[Any], min_len: int = 2, keep_lines: bool = False) -> str:
    for value in values:
        if isinstance(value, str):
            text = html_to_text(value, keep_lines=keep_lines)
            if len(text) >= min_len and not text.startswith("http"):
                return text
    return ""


def first_image_value(values: list[Any]) -> str:
    for value in values:
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, list):
            nested = first_image_value(value)
            if nested:
                return nested
        if isinstance(value, dict):
            nested = first_image_value(list(value.values()))
            if nested:
                return nested
    return ""


def image_extension(content_type: str, image_url: str) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(content_type) or Path(urlparse(image_url).path).suffix
    if ext == ".jpe":
        ext = ".jpg"
    if ext.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"
    return ext.lower()


def download_import_image(image_url: str, source_url: str = "") -> tuple[str, str | None]:
    """Baixa a capa do anúncio para a pasta persistente e devolve URL local.

    Retorna (url_local_ou_original, aviso). Se não der para baixar, mantém a URL original
    para não quebrar o cadastro.
    """
    image_url = (image_url or "").strip()
    if not image_url:
        return "", None
    if not image_url.startswith(("http://", "https://")):
        return image_url, "A imagem encontrada não era uma URL pública válida."

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": OLX_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": source_url or "https://www.olx.com.br/",
    }

    try:
        with requests.get(image_url, headers=headers, timeout=OLX_TIMEOUT_SECONDS, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "image" not in content_type.lower():
                return image_url, "Encontrei URL de imagem, mas o servidor não respondeu como imagem."

            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMPORT_IMAGE_BYTES:
                return image_url, "A imagem de capa era grande demais para importar; mantive a URL original."

            ext = image_extension(content_type, image_url)
            digest = hashlib.sha256(f"{source_url}|{image_url}".encode("utf-8")).hexdigest()[:18]
            filename = f"olx_capa_{digest}{ext}"
            path = UPLOAD_DIR / filename

            if not path.exists():
                total = 0
                with path.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_IMPORT_IMAGE_BYTES:
                            f.close()
                            path.unlink(missing_ok=True)
                            return image_url, "A imagem de capa passou do limite de tamanho; mantive a URL original."
                        f.write(chunk)

            return f"/uploads/{filename}", None
    except Exception:
        return image_url, "Não consegui baixar a capa; mantive a URL original."


def clean_olx_title(value: str) -> str:
    value = compact_spaces(value)
    value = re.sub(r"\s*[|\-–—]\s*OLX.*$", "", value, flags=re.IGNORECASE).strip()
    return value[:180]


def parse_brl_price(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = compact_spaces(str(value))
    if not text:
        return 0.0
    # Exemplos: R$ 1.250,00 | 1250 | 1,250.00
    match = re.search(r"(\d[\d\.\,]*)", text.replace(" ", ""))
    if not match:
        return 0.0
    number = match.group(1)
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    else:
        # Se vier 1.250 sem centavos, trata ponto como milhar.
        parts = number.split(".")
        if len(parts) > 1 and len(parts[-1]) == 3:
            number = "".join(parts)
    try:
        return float(number)
    except ValueError:
        return 0.0


def is_olx_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return parsed.scheme in {"http", "https"} and (host == "olx.com.br" or host.endswith(".olx.com.br"))


def category_from_olx_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    # URLs comuns: /grande-campinas/eletro/geladeiras-e-freezers/slug-id
    if len(parts) >= 3:
        candidate = parts[-2]
    elif len(parts) >= 2:
        candidate = parts[-1]
    else:
        return ""
    candidate = re.sub(r"-\d+$", "", candidate)
    return candidate.replace("-", " ").strip().title()


def tags_from_import(title: str, category: str, url: str) -> str:
    base = f"{title} {category} {category_from_olx_url(url) if url else ''}"
    useful: list[str] = []
    for token in sorted(match_terms(base)):
        if token not in useful:
            useful.append(token)
        if len(useful) >= 10:
            break
    if url and is_olx_url(url) and "olx" not in useful:
        useful.append("olx")
    return ", ".join(useful)



def looks_like_price_line(line: str) -> bool:
    return bool(re.search(r"\bR\$\s*\d", line, flags=re.IGNORECASE))


def extract_first_url(text: str, olx_only: bool = False) -> str:
    for match in re.findall(r"https?://[^\s<>'\"]+", text or ""):
        cleaned = match.rstrip(").,;]")
        if not olx_only or is_olx_url(cleaned):
            return cleaned
    return ""


def extract_first_image_url(text: str) -> str:
    for match in re.findall(r"https?://[^\s<>'\"]+", text or ""):
        cleaned = match.rstrip(").,;]")
        path = urlparse(cleaned).path.lower()
        if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return cleaned
    return ""


def is_noise_listing_line(line: str) -> bool:
    """Remove linhas comuns de UI quando o usuário cola texto da página da OLX."""
    normalized = compact_spaces(line).lower()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return True
    exact_noise = {
        "olx", "entrar", "login", "menu", "buscar", "favoritar", "compartilhar", "denunciar",
        "voltar", "próximo", "anterior", "ver telefone", "chat", "abrir chat", "fale com o vendedor",
        "vendedor", "comprar", "comprar agora", "negociar", "enviar mensagem", "ver localização",
        "descrição", "detalhes", "localização", "publicado", "publicado em", "categoria", "cep",
        "anúncios relacionados", "talvez você goste", "segurança", "dicas de segurança",
    }
    if normalized in exact_noise:
        return True
    prefixes = (
        "publicado em", "código do anúncio", "olx pay", "seguro olx", "este anúncio",
        "evite golpes", "não faça pagamentos", "baixe o app", "encontre anúncios",
        "você está em", "home /", "início /", "cookies", "política de",
    )
    return any(normalized.startswith(prefix) for prefix in prefixes)


def likely_location_line(line: str) -> bool:
    normalized = compact_spaces(line)
    states = "AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO"
    if re.search(rf"\b({states})\b", normalized):
        return True
    if re.search(r"\b(Campinas|São Paulo|Rio de Janeiro|Belo Horizonte|Curitiba|Brasília)\b", normalized, flags=re.IGNORECASE):
        return True
    return False


def category_from_text(title: str, raw_text: str) -> str:
    text = f"{title}\n{raw_text}".lower()
    buckets = [
        ("Eletrodomésticos", ["freezer", "geladeira", "fogão", "microondas", "micro-ondas", "espremedor", "batedeira", "liquidificador", "cafeteira", "eletro"]),
        ("Bebê e Infantil", ["berço", "bebê", "carrinho", "mamadeira", "cadeirinha", "maternidade", "infantil"]),
        ("Móveis", ["mesa", "cadeira", "sofá", "rack", "guarda roupa", "armário", "estante", "cama"]),
        ("Eletrônicos", ["iphone", "celular", "notebook", "tablet", "monitor", "tv", "televisão", "som", "caixa de som"]),
        ("Roupas", ["roupa", "vestido", "camisa", "calça", "lote", "cama", "lençol", "toalha"]),
        ("Ferramentas", ["furadeira", "parafusadeira", "serra", "compressor", "ferramenta"]),
    ]
    for category, words in buckets:
        if any(word in text for word in words):
            return category
    return ""


def import_from_pasted_text(raw_text: str, source_url: str = "", image_url: str = "") -> dict[str, Any]:
    """Transforma texto copiado da OLX em pré-cadastro editável.

    Serve como fallback confiável quando a OLX bloqueia leitura por servidor com 403.
    O usuário abre o anúncio no navegador, copia o texto visível e cola aqui.
    """
    raw_text = raw_text or ""
    source_url = (source_url or "").strip() or extract_first_url(raw_text, olx_only=True)
    found_image_url = (image_url or "").strip() or extract_first_image_url(raw_text)
    cleaned_text = clean_description(raw_text)
    raw_lines = [compact_spaces(line) for line in cleaned_text.splitlines()]
    lines: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        if not line or is_noise_listing_line(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)

    price = 0.0
    price_index: int | None = None
    for idx, line in enumerate(lines):
        if looks_like_price_line(line):
            price = parse_brl_price(line)
            price_index = idx
            break

    title = ""
    title_candidates: list[tuple[int, str]] = []
    for idx, line in enumerate(lines[:12]):
        if looks_like_price_line(line) or line.startswith("http") or likely_location_line(line):
            continue
        # Títulos reais costumam ser maiores que labels da interface, mas não parágrafos gigantes.
        if 8 <= len(line) <= 160:
            title_candidates.append((idx, line))
    if price_index is not None:
        before_price = [item for item in title_candidates if item[0] < price_index]
        if before_price:
            title = before_price[-1][1]
    if not title and title_candidates:
        title = title_candidates[0][1]

    location = ""
    for line in lines:
        if likely_location_line(line):
            location = line
            break

    description_lines: list[str] = []
    skip_indexes = {idx for idx, line in enumerate(lines) if line == title or looks_like_price_line(line) or line.startswith("http")}
    for idx, line in enumerate(lines):
        if idx in skip_indexes:
            continue
        if line == location:
            continue
        if len(line) < 3:
            continue
        description_lines.append(line)

    # Evita que o título/cidade roubem a descrição quando o usuário colou pouca coisa.
    description = clean_description("\n".join(description_lines))
    category = category_from_text(title, cleaned_text) or (category_from_olx_url(source_url) if source_url and is_olx_url(source_url) else "")
    tags = tags_from_import(f"{title} {location}", category, source_url or "")

    local_image_url = found_image_url
    image_warning = None
    if found_image_url:
        local_image_url, image_warning = download_import_image(found_image_url, source_url)

    warnings: list[str] = [
        "Importei a partir de texto colado. Revise antes de salvar, porque texto copiado de página pode vir bagunçado.",
    ]
    if not title:
        warnings.append("Não consegui detectar o título com segurança.")
    if not price:
        warnings.append("Não encontrei preço no texto colado.")
    if not description:
        warnings.append("Não consegui montar uma descrição limpa; cole a descrição manualmente se necessário.")
    if location:
        warnings.append(f"Local detectado: {location}")
    if image_warning:
        warnings.append(image_warning)

    return {
        "title": clean_olx_title(title),
        "description": description,
        "category": category,
        "price": price,
        "min_price": 0,
        "quantity": 1,
        "status": "anunciado",
        "temperature": "😐 morno",
        "tags": tags,
        "image_url": local_image_url,
        "source_url": source_url,
        "source_platform": "OLX",
        "mode": "text",
        "raw_text": raw_text,
        "warnings": warnings,
    }



def olx_import_fallback(url: str, warning: str) -> dict[str, Any]:
    """Devolve uma prévia manual quando a OLX bloqueia o robô.

    Assim o usuário não cai num beco sem saída: o link já fica salvo e o
    formulário abre para preencher título/preço/descrição/imagem na mão.
    """
    url = (url or "").strip()
    category = category_from_olx_url(url) if is_olx_url(url) else ""
    return {
        "title": "",
        "description": "",
        "category": category,
        "price": 0,
        "min_price": 0,
        "quantity": 1,
        "status": "anunciado",
        "temperature": "😐 morno",
        "tags": tags_from_import("", category, url),
        "image_url": "",
        "source_url": url,
        "source_platform": "OLX",
        "mode": "manual",
        "warnings": [
            warning,
            "Abri o modo manual assistido: preencha os campos e salve. O link da OLX será registrado em Anúncios.",
            "Se quiser capa, cole a URL da imagem no campo de imagem; o app tenta baixar e salvar localmente ao salvar o produto.",
        ],
    }


def olx_request_headers(referer: str = "https://www.olx.com.br/") -> dict[str, str]:
    return {
        "User-Agent": OLX_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }


def fetch_html_like_browser(url: str) -> requests.Response:
    """Tenta buscar a página com um perfil mais parecido com navegador.

    Isso não burla bloqueio forte: se a OLX negar com 403, respeitamos e caímos
    no modo manual assistido.
    """
    session = requests.Session()
    try:
        session.get("https://www.olx.com.br/", headers=olx_request_headers(), timeout=6)
    except requests.RequestException:
        pass
    response = session.get(url, headers=olx_request_headers(), timeout=OLX_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()
    return response


def prepare_image_for_save(image_url: str, source_url: str = "") -> tuple[str, str | None]:
    image_url = (image_url or "").strip()
    if image_url.startswith(("http://", "https://")):
        return download_import_image(image_url, source_url)
    return image_url, None


def create_product_record(
    *,
    title: str,
    description: str = "",
    category: str = "",
    price: float = 0.0,
    min_price: float = 0.0,
    quantity: int = 1,
    status: str = "rascunho",
    temperature: str = "😐 morno",
    tags: str = "",
    image_url: str = "",
    source_url: str = "",
    source_platform: str = "OLX",
) -> int:
    """Cria produto e, se houver link de origem, já registra o anúncio/link."""
    image_url, _image_warning = prepare_image_for_save(image_url, source_url)
    product_id = execute(
        """
        INSERT INTO products(title, description, category, price, min_price, quantity, status, temperature, tags, image_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title.strip(),
            clean_description(description),
            category.strip(),
            float(price or 0),
            float(min_price or 0),
            max(0, int(quantity or 1)),
            status or "rascunho",
            temperature or "😐 morno",
            tags.strip(),
            image_url,
            now_iso(),
        ),
        returning="id",
    )
    if source_url:
        execute(
            """
            INSERT INTO listings(product_id, platform, url, status, views, messages, clicks, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (product_id, source_platform or "OLX", source_url.strip(), "ativo", 0, 0, 0, now_iso()),
        )
    return product_id


def product_is_quarantined(product: sqlite3.Row | dict[str, Any] | None) -> bool:
    return bool(product and str(product["quarantined_at"] or "").strip())


def quarantine_restore_deadline(quarantined_at: str | None) -> datetime | None:
    if not quarantined_at:
        return None
    try:
        return datetime.strptime(quarantined_at[:19], "%Y-%m-%d %H:%M:%S") + timedelta(days=QUARANTINE_DAYS)
    except Exception:
        return None


def quarantine_days_left(quarantined_at: str | None) -> int:
    deadline = quarantine_restore_deadline(quarantined_at)
    if not deadline:
        return 0
    return max(0, (deadline.date() - date.today()).days)


def quarantine_can_restore(quarantined_at: str | None) -> bool:
    deadline = quarantine_restore_deadline(quarantined_at)
    return bool(deadline and datetime.now() <= deadline)


def apply_sale_inventory(product_id: int, reason: str = "Venda registrada") -> None:
    """Atualiza estoque depois de uma venda.

    Produto único (quantidade 1 ou menos) sai da lista principal e vai para a quarentena.
    Produto com quantidade maior só baixa uma unidade.
    """
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        return
    current_qty = int(product["quantity"] or 0)
    if current_qty <= 1:
        execute(
            """
            UPDATE products
            SET status='vendido', quantity=0, quarantined_at=?, quarantine_reason=?
            WHERE id=?
            """,
            (now_iso(), reason, product_id),
        )
    else:
        execute("UPDATE products SET quantity=? WHERE id=?", (current_qty - 1, product_id))



def register_quick_sale(
    product_id: int,
    sale_price: float | None = None,
    payment_status: str = "recebido",
    platform: str = "Venda rápida",
    sold_at: str | None = None,
    lead_id: int | None = None,
    buyer_name: str = "",
    buyer_whatsapp: str = "",
    buyer_source: str = "",
    notes: str = "",
) -> None:
    """Registra venda rápida com comprador cadastrado ou comprador avulso."""
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        raise ValueError("Produto não encontrado.")
    price = float(product["price"] or 0) if sale_price is None else float(sale_price or 0)
    execute(
        """
        INSERT INTO sales(product_id, lead_id, buyer_name, buyer_whatsapp, buyer_source, sale_price, payment_status, platform, sold_at, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            lead_id,
            buyer_name.strip(),
            buyer_whatsapp.strip(),
            buyer_source.strip(),
            price,
            payment_status or "recebido",
            platform.strip() or "Venda rápida",
            sold_at or date.today().isoformat(),
            clean_description(notes or "Venda registrada pelo botão rápido."),
            now_iso(),
        ),
    )
    apply_sale_inventory(product_id, "Venda rápida registrada")


def fetch_olx_listing(url: str) -> dict[str, Any]:
    url = url.strip()
    if not is_olx_url(url):
        raise ValueError("Cole um link válido da OLX, tipo https://sp.olx.com.br/...")

    response = fetch_html_like_browser(url)
    soup = BeautifulSoup(response.text, "html.parser")
    jsonld = first_jsonld_objects(soup)

    title = clean_olx_title(meta_content(soup, "og:title", "twitter:title"))
    description = clean_description(meta_content(soup, "og:description", "description", "twitter:description", clean=False))
    image_url = meta_content(soup, "og:image", "twitter:image")
    price = parse_brl_price(meta_content(soup, "product:price:amount", "og:price:amount"))

    for obj in jsonld:
        obj_type = obj.get("@type")
        if isinstance(obj_type, list):
            obj_type = " ".join(str(x) for x in obj_type)
        obj_type = str(obj_type or "").lower()
        if not title and obj.get("name"):
            title = clean_olx_title(obj.get("name"))
        if not description and obj.get("description"):
            description = clean_description(obj.get("description"))
        if not image_url and obj.get("image"):
            image = obj.get("image")
            if isinstance(image, list):
                image_url = str(image[0]) if image else ""
            else:
                image_url = str(image)
        offers = obj.get("offers") if isinstance(obj, dict) else None
        if not price and isinstance(offers, dict):
            price = parse_brl_price(offers.get("price") or offers.get("lowPrice"))
        if title and price:
            break

    if not (title and price and description and image_url):
        for obj in page_json_objects(soup):
            if not title:
                title = clean_olx_title(first_text_value(deep_values(obj, {"subject", "title", "name"})))
            if not description:
                description = first_text_value(deep_values(obj, {"description", "body", "details"}), min_len=8, keep_lines=True)
            if not price:
                price = parse_brl_price(first_text_value(deep_values(obj, {"price", "value", "amount"})))
            if not image_url:
                image_url = first_image_value(deep_values(obj, {"image", "images", "picture", "pictures", "photo", "photos", "url"}))
            if title and price and description and image_url:
                break

    if not title and soup.title and soup.title.string:
        title = clean_olx_title(soup.title.string)

    final_url = response.url or url
    category = category_from_olx_url(final_url)
    tags = tags_from_import(title, category, final_url)

    local_image_url, image_warning = download_import_image(image_url, final_url)
    if local_image_url:
        image_url = local_image_url

    warnings: list[str] = []
    if image_warning:
        warnings.append(image_warning)
    if not title:
        warnings.append("Não consegui identificar o título automaticamente.")
    if not price:
        warnings.append("Não consegui identificar o preço automaticamente.")
    if not description:
        warnings.append("A descrição veio vazia ou protegida pela página.")
    if not image_url:
        warnings.append("Não encontrei imagem principal nos metadados.")

    return {
        "title": title,
        "description": description,
        "category": category,
        "price": price,
        "min_price": 0,
        "quantity": 1,
        "status": "anunciado",
        "temperature": "😐 morno",
        "tags": tags,
        "image_url": image_url,
        "source_url": final_url,
        "source_platform": "OLX",
        "mode": "auto",
        "warnings": warnings,
    }


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        price REAL DEFAULT 0,
        min_price REAL DEFAULT 0,
        quantity INTEGER DEFAULT 1,
        status TEXT DEFAULT 'rascunho',
        temperature TEXT DEFAULT '😐 morno',
        tags TEXT DEFAULT '',
        image_url TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        quarantined_at TEXT DEFAULT '',
        quarantine_reason TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        whatsapp TEXT DEFAULT '',
        city TEXT DEFAULT '',
        budget REAL DEFAULT 0,
        tags TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        score INTEGER DEFAULT 50,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        url TEXT DEFAULT '',
        status TEXT DEFAULT 'ativo',
        views INTEGER DEFAULT 0,
        messages INTEGER DEFAULT 0,
        clicks INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        lead_id INTEGER,
        buyer_name TEXT DEFAULT '',
        buyer_whatsapp TEXT DEFAULT '',
        buyer_source TEXT DEFAULT '',
        sale_price REAL DEFAULT 0,
        payment_status TEXT DEFAULT 'pendente',
        platform TEXT DEFAULT '',
        sold_at TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
        FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""

SCHEMA_POSTGRES = """
    CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        price REAL DEFAULT 0,
        min_price REAL DEFAULT 0,
        quantity INTEGER DEFAULT 1,
        status TEXT DEFAULT 'rascunho',
        temperature TEXT DEFAULT '😐 morno',
        tags TEXT DEFAULT '',
        image_url TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        quarantined_at TEXT DEFAULT '',
        quarantine_reason TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        whatsapp TEXT DEFAULT '',
        city TEXT DEFAULT '',
        budget REAL DEFAULT 0,
        tags TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        score INTEGER DEFAULT 50,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS listings (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        url TEXT DEFAULT '',
        status TEXT DEFAULT 'ativo',
        views INTEGER DEFAULT 0,
        messages INTEGER DEFAULT 0,
        clicks INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sales (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL,
        buyer_name TEXT DEFAULT '',
        buyer_whatsapp TEXT DEFAULT '',
        buyer_source TEXT DEFAULT '',
        sale_price REAL DEFAULT 0,
        payment_status TEXT DEFAULT 'pendente',
        platform TEXT DEFAULT '',
        sold_at TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""

PRODUCT_COLUMN_MIGRATIONS = {
    "quarantined_at": "ALTER TABLE products ADD COLUMN quarantined_at TEXT DEFAULT ''",
    "quarantine_reason": "ALTER TABLE products ADD COLUMN quarantine_reason TEXT DEFAULT ''",
}
SALES_COLUMN_MIGRATIONS = {
    "buyer_name": "ALTER TABLE sales ADD COLUMN buyer_name TEXT DEFAULT ''",
    "buyer_whatsapp": "ALTER TABLE sales ADD COLUMN buyer_whatsapp TEXT DEFAULT ''",
    "buyer_source": "ALTER TABLE sales ADD COLUMN buyer_source TEXT DEFAULT ''",
}


def init_db() -> None:
    """Cria as tabelas (se não existirem) e aplica migrações leves.

    Funciona tanto com Postgres (produção, persistente por padrão em qualquer
    host, sem precisar de disco) quanto com SQLite (uso local em arquivo).
    """
    if USE_POSTGRES:
        db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur = db.cursor()
            cur.execute(SCHEMA_POSTGRES)
            db.commit()

            product_cols = set(table_columns(db, "products"))
            for column, ddl in PRODUCT_COLUMN_MIGRATIONS.items():
                if column not in product_cols:
                    cur.execute(ddl)

            sales_cols = set(table_columns(db, "sales"))
            for column, ddl in SALES_COLUMN_MIGRATIONS.items():
                if column not in sales_cols:
                    cur.execute(ddl)
            db.commit()

            for key, value in DEFAULT_SETTINGS.items():
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (key, value),
                )
            db.commit()
            cur.close()
        finally:
            db.close()
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_SQLITE)

        # Migrações leves para bancos já existentes. Assim você atualiza o app sem perder dados.
        product_cols = set(table_columns(db, "products"))
        for column, ddl in PRODUCT_COLUMN_MIGRATIONS.items():
            if column not in product_cols:
                db.execute(ddl)

        sales_cols = set(table_columns(db, "sales"))
        for column, ddl in SALES_COLUMN_MIGRATIONS.items():
            if column not in sales_cols:
                db.execute(ddl)

        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        db.commit()


def normalize_bool(value: str | None) -> str:
    return "1" if value in {"1", "true", "on", "yes", "sim"} else "0"


def get_settings() -> dict[str, Any]:
    rows = query("SELECT key, value FROM settings")
    settings = DEFAULT_SETTINGS.copy()
    settings.update({row["key"]: row["value"] for row in rows})

    if settings.get("theme") not in THEMES:
        settings["theme"] = DEFAULT_SETTINGS["theme"]

    try:
        threshold = int(settings.get("match_threshold", DEFAULT_SETTINGS["match_threshold"]))
    except ValueError:
        threshold = int(DEFAULT_SETTINGS["match_threshold"])
    settings["match_threshold"] = str(max(25, min(95, threshold)))

    settings["show_match_alert"] = normalize_bool(settings.get("show_match_alert"))
    settings["match_sound"] = normalize_bool(settings.get("match_sound"))
    settings["fun_mode"] = normalize_bool(settings.get("fun_mode"))
    settings["theme_data"] = THEMES[settings["theme"]]
    settings["themes"] = THEMES
    return settings


def save_settings(values: dict[str, str]) -> None:
    allowed = set(DEFAULT_SETTINGS)
    for key, value in values.items():
        if key not in allowed:
            continue
        execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def tag_usage(limit: int = 80) -> list[dict[str, Any]]:
    """Tags usadas nos produtos/leads, ordenadas por frequência.

    É o auto-preenchimento do jeito honesto: o app aprende com o uso real.
    Se você nunca usou uma tag, ela não aparece como sugestão ainda.
    """
    counts: dict[str, int] = {}
    for row in query("SELECT tags FROM products WHERE COALESCE(quarantined_at, '') = '' AND status != 'vendido' UNION ALL SELECT tags FROM leads"):
        for tag in parse_tags(row["tags"]):
            counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"tag": tag, "count": count} for tag, count in ranked]


def hot_match_summary() -> dict[str, Any]:
    settings = get_settings()
    if settings["show_match_alert"] != "1":
        return {"count": 0, "best": None, "threshold": int(settings["match_threshold"])}
    threshold = int(settings["match_threshold"])
    matches = [m for m in all_matches(min_score=1) if m["score"] >= threshold]
    best = matches[0] if matches else None
    return {"count": len(matches), "best": best, "threshold": threshold}


def match_product_lead(product: sqlite3.Row, lead: sqlite3.Row) -> dict[str, Any]:
    # Campos explícitos contam mais; texto livre ajuda, mas não manda sozinho.
    p_explicit = set(parse_tags(product["tags"]) + parse_tags(product["category"]))
    l_explicit = set(parse_tags(lead["tags"]))

    p_text = match_terms(product["title"], product["description"], product["category"], product["tags"])
    l_text = match_terms(lead["name"], lead["city"], lead["notes"], lead["tags"])

    explicit_common = sorted(p_explicit.intersection(l_explicit))
    text_common = sorted((p_text.intersection(l_text)) - set(explicit_common))

    score = 0
    reasons: list[str] = []

    if explicit_common:
        score += min(55, len(explicit_common) * 24)
        reasons.append(f"tags iguais: {', '.join(explicit_common[:6])}")

    if text_common:
        score += min(30, len(text_common) * 10)
        reasons.append(f"termos parecidos: {', '.join(text_common[:6])}")

    # Categoria do produto aparecendo nas tags/notas do lead é um bom sinal.
    category_terms = match_terms(product["category"])
    if category_terms and category_terms.intersection(l_text):
        score += 12
        reasons.append("categoria combina com o interesse do lead")

    budget = float(lead["budget"] or 0)
    price = float(product["price"] or 0)
    if budget and price:
        if price <= budget:
            score += 25
            reasons.append("cabe no orçamento")
        elif price <= budget * 1.15:
            score += 12
            reasons.append("passa um pouco do orçamento, mas dá negociação")
        else:
            score -= 18
            reasons.append("acima do orçamento informado")
    elif (explicit_common or text_common) and not budget:
        score += 6
        reasons.append("lead sem orçamento: match calculado por interesse")

    if (product["temperature"] or "").startswith("🔥"):
        score += 8
        reasons.append("produto marcado como quente")

    lead_score = int(lead["score"] or 0)
    if lead_score >= 80:
        score += 12
        reasons.append("lead com pontuação alta")
    elif lead_score >= 50:
        score += 5
        reasons.append("lead com pontuação média/boa")

    status = product["status"] or ""
    if status in {"anunciado", "rascunho", "reservado", "parado"}:
        score += 5
        reasons.append("produto ainda não foi vendido")

    score = max(0, min(100, score))
    label = "fraco"
    emoji = "🧊"
    if score >= 85:
        label = "chama agora"
        emoji = "🚀"
    elif score >= 70:
        label = "bom lead"
        emoji = "🔥"
    elif score >= 40:
        label = "talvez valha mandar"
        emoji = "👀"

    debug = {
        "product_terms": sorted(p_text)[:18],
        "lead_terms": sorted(l_text)[:18],
        "explicit_common": explicit_common,
        "text_common": text_common,
    }

    return {
        "product": product,
        "lead": lead,
        "score": score,
        "label": label,
        "emoji": emoji,
        "reasons": reasons or ["sem sinais suficientes: coloque tags parecidas no produto e no lead"],
        "debug": debug,
    }


def all_matches(product_id: int | None = None, min_score: int = 1) -> list[dict[str, Any]]:
    if product_id:
        products = query("SELECT * FROM products WHERE id = ? AND COALESCE(quarantined_at, '') = '' AND status != 'vendido'", (product_id,))
    else:
        products = query("SELECT * FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = '' ORDER BY created_at DESC")
    leads = query("SELECT * FROM leads ORDER BY score DESC, created_at DESC")
    matches: list[dict[str, Any]] = []
    for product in products:
        for lead in leads:
            match = match_product_lead(product, lead)
            if match["score"] >= min_score:
                matches.append(match)
    return sorted(matches, key=lambda x: x["score"], reverse=True)


def match_diagnostics(product_id: int | None = None) -> dict[str, Any]:
    products_count = query("SELECT COUNT(*) AS total FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = ''", one=True)["total"]
    leads_count = query("SELECT COUNT(*) AS total FROM leads", one=True)["total"]
    pairs = products_count * leads_count
    visible = len(all_matches(product_id, min_score=1)) if pairs else 0
    return {
        "products": products_count,
        "leads": leads_count,
        "pairs": pairs,
        "visible": visible,
    }

@app.context_processor
def inject_globals():
    hot = hot_match_summary()
    quarantine_count = query("SELECT COUNT(*) AS c FROM products WHERE COALESCE(quarantined_at, '') != ''", one=True)["c"]
    match_count = int(hot.get("count") or 0)
    return {
        "statuses": STATUSES,
        "temperatures": TEMPERATURES,
        "platforms": PLATFORMS,
        "payment_statuses": PAYMENT_STATUSES,
        "today": date.today().isoformat(),
        "tag_suggestions": tag_usage(),
        "hot_match_alert": hot,
        "match_badge_count": match_count,
        "match_badge_label": "99+" if match_count > 99 else str(match_count),
        "quarantine_count": quarantine_count,
        "app_settings": get_settings(),
        "themes": THEMES,
        "db_engine_label": "Postgres" if USE_POSTGRES else "SQLite",
    }




def safe_next_url(value: str | None) -> str:
    """Evita redirecionamento externo depois do login."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("dashboard")


@app.before_request
def require_login():
    endpoint = request.endpoint or ""
    public_endpoints = {"login", "static", "health"}
    if endpoint in public_endpoints or endpoint.startswith("static"):
        return None
    if not session.get("logged_in"):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_url))
    return None


@app.route("/health")
def health():
    return {"status": "ok", "app": "vende_facil", "db_engine": "postgres" if USE_POSTGRES else "sqlite"}


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in") and request.method == "GET":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        settings = get_settings()
        login_email = (settings.get("login_email") or DEFAULT_LOGIN_EMAIL).strip().lower()
        login_password = settings.get("login_password") or DEFAULT_LOGIN_PASSWORD
        valid_email = secrets.compare_digest(email, login_email)
        valid_password = secrets.compare_digest(password, login_password)

        if valid_email and valid_password:
            session.clear()
            session["logged_in"] = True
            session["user_email"] = login_email
            flash("Login feito. Pode entrar no balcão. 🔐", "success")
            return redirect(safe_next_url(request.args.get("next")))

        flash("Login inválido. Eita: ou o e-mail ou a senha não bateram.", "danger")

    return render_template("login.html", title="Entrar")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Você saiu do app. Porta fechada, balcão protegido. 🔒", "info")
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    stats = {
        "products": query("SELECT COUNT(*) AS c FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = ''", one=True)["c"],
        "active": query("SELECT COUNT(*) AS c FROM products WHERE status = 'anunciado' AND COALESCE(quarantined_at, '') = ''", one=True)["c"],
        "sold": query("SELECT COUNT(*) AS c FROM products WHERE status = 'vendido'", one=True)["c"],
        "quarantine": query("SELECT COUNT(*) AS c FROM products WHERE COALESCE(quarantined_at, '') != ''", one=True)["c"],
        "leads": query("SELECT COUNT(*) AS c FROM leads", one=True)["c"],
        "revenue": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales WHERE payment_status = 'recebido'", one=True)["total"],
        "receivable": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales WHERE payment_status != 'recebido'", one=True)["total"],
        "expected_profit": query(f"SELECT COALESCE(SUM(price * {SCALAR_MAX}(quantity, 1)), 0) AS total FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = ''", one=True)["total"],
        "listed_value": query(f"SELECT COALESCE(SUM(price * {SCALAR_MAX}(quantity, 1)), 0) AS total FROM products WHERE status = 'anunciado' AND COALESCE(quarantined_at, '') = ''", one=True)["total"],
        "minimum_value": query(f"SELECT COALESCE(SUM(min_price * {SCALAR_MAX}(quantity, 1)), 0) AS total FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = ''", one=True)["total"],
        "discount_room": query(f"SELECT COALESCE(SUM({SCALAR_MAX}(price - min_price, 0) * {SCALAR_MAX}(quantity, 1)), 0) AS total FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = ''", one=True)["total"],
        "sold_total": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales", one=True)["total"],
        "average_ticket": query("SELECT COALESCE(AVG(sale_price), 0) AS total FROM sales", one=True)["total"],
        "pending_sales": query("SELECT COUNT(*) AS c FROM sales WHERE payment_status != 'recebido'", one=True)["c"],
        "active_links": query("SELECT COUNT(*) AS c FROM listings WHERE status = 'ativo'", one=True)["c"],
    }
    hot_products = query("SELECT * FROM products WHERE temperature LIKE '🔥%' AND status != 'vendido' AND COALESCE(quarantined_at, '') = '' ORDER BY created_at DESC LIMIT 5")
    cold_products = query("SELECT * FROM products WHERE (temperature LIKE '❄️%' OR status = 'parado') AND status != 'vendido' AND COALESCE(quarantined_at, '') = '' ORDER BY created_at DESC LIMIT 5")
    recent_sales = query(
        """
        SELECT sales.*, products.title AS product_title, leads.name AS lead_name
        FROM sales
        JOIN products ON products.id = sales.product_id
        LEFT JOIN leads ON leads.id = sales.lead_id
        ORDER BY sales.created_at DESC LIMIT 5
        """
    )
    top_matches = all_matches(min_score=25)[:5]
    return render_template("dashboard.html", stats=stats, hot_products=hot_products, cold_products=cold_products, recent_sales=recent_sales, top_matches=top_matches)


@app.route("/products")
def products():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = "SELECT * FROM products WHERE COALESCE(quarantined_at, '') = ''"
    args: list[Any] = []
    if q:
        sql += " AND (title LIKE ? OR description LIKE ? OR tags LIKE ? OR category LIKE ?)"
        like = f"%{q}%"
        args.extend([like, like, like, like])
    if status:
        sql += " AND status = ?"
        args.append(status)
    else:
        sql += " AND status != 'vendido'"
    sql += " ORDER BY created_at DESC"
    rows = query(sql, tuple(args))
    leads_rows = query("SELECT id, name, whatsapp, city FROM leads ORDER BY name")
    return render_template("products.html", products=rows, q=q, status=status, leads=leads_rows)


@app.route("/products/new", methods=["GET", "POST"])
def product_new():
    imported = None
    import_url = ""
    paste_text = ""

    if request.method == "POST" and request.form.get("_action") == "import_text":
        paste_text = request.form.get("import_paste_text", "").strip()
        source_url = request.form.get("import_text_source_url", "").strip()
        image_url = request.form.get("import_text_image_url", "").strip()
        if not paste_text:
            flash("Cole o texto do anúncio para eu tentar separar as informações.", "warning")
        else:
            imported = import_from_pasted_text(paste_text, source_url, image_url)
            flash("Texto colado transformado em cadastro. Agora revise e salve — o garimpo bruto virou ficha. 📝", "success")
        return render_template("product_form.html", product=None, imported=imported, import_url="", paste_text=paste_text)

    if request.method == "POST" and request.form.get("_action") == "import_olx":
        import_url = request.form.get("import_olx_url", "").strip()
        try:
            imported = fetch_olx_listing(import_url)
            flash("Dados da OLX puxados para o cadastro. Agora revise e salve — piloto automático com freio de mão. 🧲", "success")
        except requests.Timeout:
            imported = olx_import_fallback(import_url, "A OLX demorou para responder.")
            flash("A OLX demorou para responder. Abri o modo manual assistido para você não perder o fluxo.", "warning")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            imported = olx_import_fallback(import_url, f"HTTP {status}: a OLX bloqueou a leitura automática, o anúncio expirou ou a página está protegida.")
            flash(f"Não consegui ler esse anúncio automaticamente. HTTP {status}. Abri o modo manual assistido.", "warning")
        except requests.RequestException:
            imported = olx_import_fallback(import_url, "Não consegui acessar a OLX agora.")
            flash("Não consegui acessar a OLX agora. Abri o modo manual assistido.", "warning")
        except ValueError as exc:
            flash(str(exc), "warning")
        except Exception:
            imported = olx_import_fallback(import_url, "Não consegui importar esse link automaticamente.")
            flash("Não consegui importar esse link. Abri o modo manual assistido.", "warning")
        return render_template("product_form.html", product=None, imported=imported, import_url=import_url)

    if request.method == "POST":
        source_url = request.form.get("source_url", "").strip()
        source_platform = request.form.get("source_platform", "").strip() or "OLX"
        create_product_record(
            title=request.form["title"],
            description=request.form.get("description", ""),
            category=request.form.get("category", ""),
            price=float(request.form.get("price") or 0),
            min_price=float(request.form.get("min_price") or 0),
            quantity=int(request.form.get("quantity") or 1),
            status=request.form.get("status", "rascunho"),
            temperature=request.form.get("temperature", "😐 morno"),
            tags=request.form.get("tags", ""),
            image_url=request.form.get("image_url", ""),
            source_url=source_url,
            source_platform=source_platform,
        )
        if source_url:
            flash("Produto importado e link da OLX salvo. Meio caminho andado, sem copiar tudo na unha. 🧲", "success")
        else:
            flash("Produto cadastrado. Mais um item na prateleira digital. 🧺", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=None, imported=None, import_url="", paste_text="")


@app.route("/import/text", methods=["GET", "POST"])
def text_import():
    imported = None
    raw_text = request.form.get("raw_text", "").strip() if request.method == "POST" else ""
    source_url = request.form.get("source_url", "").strip() if request.method == "POST" else ""
    image_url = request.form.get("image_url", "").strip() if request.method == "POST" else ""
    if request.method == "POST":
        if not raw_text:
            flash("Cole o texto do anúncio para importar. Campo vazio não vende nem água no deserto.", "warning")
        else:
            imported = import_from_pasted_text(raw_text, source_url, image_url)
            flash("Importei a partir do texto colado. Revise os campos antes de salvar. 📝", "success")
    return render_template("text_import.html", raw_text=raw_text, source_url=source_url, image_url=image_url, imported=imported)


@app.route("/import/olx", methods=["GET", "POST"])
def olx_import():
    imported = None
    url = request.form.get("url", "").strip() if request.method == "POST" else request.args.get("url", "").strip()
    if request.method == "POST":
        try:
            imported = fetch_olx_listing(url)
            flash("Importação OLX feita. Revise antes de salvar — robô ajuda, mas não assina contrato. 🕵️", "success")
        except requests.Timeout:
            imported = olx_import_fallback(url, "A OLX demorou para responder.")
            flash("A OLX demorou para responder. Abri o modo manual assistido.", "warning")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            imported = olx_import_fallback(url, f"HTTP {status}: a OLX bloqueou a leitura automática, o anúncio expirou ou a página está protegida.")
            flash(f"Não consegui ler esse anúncio automaticamente. HTTP {status}. Abri o modo manual assistido.", "warning")
        except requests.RequestException:
            imported = olx_import_fallback(url, "Não consegui acessar a OLX agora.")
            flash("Não consegui acessar a OLX agora. Abri o modo manual assistido.", "warning")
        except ValueError as exc:
            flash(str(exc), "warning")
        except Exception:
            imported = olx_import_fallback(url, "Não consegui importar esse link automaticamente.")
            flash("Não consegui importar esse link. Abri o modo manual assistido.", "warning")
    return render_template("olx_import.html", url=url, imported=imported)


@app.route("/import/olx/bulk", methods=["GET", "POST"])
def olx_bulk_import():
    raw_urls = request.form.get("urls", "").strip() if request.method == "POST" else ""
    results: list[dict[str, Any]] = []
    if request.method == "POST":
        urls: list[str] = []
        for line in re.split(r"[\n\r\t ]+", raw_urls):
            line = line.strip().strip(",;)")
            if not line:
                continue
            if line not in urls:
                urls.append(line)

        if not urls:
            flash("Cole pelo menos um link da OLX para importar em lote.", "warning")
        else:
            imported_count = 0
            for url in urls:
                item = {"url": url, "ok": False, "title": "", "message": ""}
                try:
                    imported = fetch_olx_listing(url)
                    if not imported.get("title"):
                        raise ValueError("Não encontrei título suficiente para salvar automaticamente.")
                    create_product_record(
                        title=imported.get("title", ""),
                        description=imported.get("description", ""),
                        category=imported.get("category", ""),
                        price=float(imported.get("price") or 0),
                        min_price=float(imported.get("min_price") or 0),
                        quantity=int(imported.get("quantity") or 1),
                        status=imported.get("status") or "anunciado",
                        temperature=imported.get("temperature") or "😐 morno",
                        tags=imported.get("tags", ""),
                        image_url=imported.get("image_url", ""),
                        source_url=imported.get("source_url") or url,
                        source_platform="OLX",
                    )
                    imported_count += 1
                    item.update({"ok": True, "title": imported.get("title", ""), "message": "Importado e salvo."})
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else "?"
                    item["message"] = f"Falhou: HTTP {status}. Provável bloqueio da OLX; use importar por texto para este anúncio."
                except requests.Timeout:
                    item["message"] = "Falhou: a OLX demorou para responder."
                except ValueError as exc:
                    item["message"] = f"Falhou: {exc}"
                except requests.RequestException:
                    item["message"] = "Falhou: não consegui acessar esse link."
                except Exception:
                    item["message"] = "Falhou: erro inesperado na importação."
                results.append(item)

            if imported_count:
                flash(f"Importação em lote concluída: {imported_count} anúncio(s) salvo(s).", "success")
            if imported_count < len(results):
                flash("Alguns links não importaram. Quando a OLX bloqueia, use o importador por texto colado.", "warning")

    return render_template("olx_bulk_import.html", urls=raw_urls, results=results)


@app.route("/products/<int:product_id>/quick-sale", methods=["POST"])
def product_quick_sale(product_id: int):
    try:
        buyer_mode = request.form.get("buyer_mode", "custom")
        lead_id = int(request.form["lead_id"]) if buyer_mode == "lead" and request.form.get("lead_id") else None
        register_quick_sale(
            product_id,
            sale_price=float(request.form.get("sale_price") or 0) if request.form.get("sale_price") else None,
            payment_status=request.form.get("payment_status", "recebido"),
            platform=request.form.get("platform", "Venda rápida"),
            sold_at=request.form.get("sold_at") or date.today().isoformat(),
            lead_id=lead_id,
            buyer_name=request.form.get("buyer_name", "") if not lead_id else "",
            buyer_whatsapp=request.form.get("buyer_whatsapp", "") if not lead_id else "",
            buyer_source=request.form.get("buyer_source", "") if not lead_id else "",
            notes=request.form.get("notes", ""),
        )
        flash("Venda rápida registrada. Caixa cantou sem abrir planilha. 💸", "success")
    except ValueError:
        flash("Produto não encontrado para venda rápida.", "warning")
    return redirect(request.referrer or url_for("products"))


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def product_edit(product_id: int):
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("products"))
    if request.method == "POST":
        execute(
            """
            UPDATE products SET title=?, description=?, category=?, price=?, min_price=?, quantity=?, status=?, temperature=?, tags=?, image_url=?
            WHERE id=?
            """,
            (
                request.form["title"].strip(),
                clean_description(request.form.get("description", "")),
                request.form.get("category", "").strip(),
                float(request.form.get("price") or 0),
                float(request.form.get("min_price") or 0),
                int(request.form.get("quantity") or 1),
                request.form.get("status", "rascunho"),
                request.form.get("temperature", "😐 morno"),
                request.form.get("tags", "").strip(),
                prepare_image_for_save(request.form.get("image_url", ""))[0],
                product_id,
            ),
        )
        flash("Produto atualizado. Tá ficando bonito esse balcão. ✨", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html", product=product)


@app.route("/products/<int:product_id>/delete", methods=["POST"])
def product_delete(product_id: int):
    execute("DELETE FROM products WHERE id = ?", (product_id,))
    flash("Produto removido.", "info")
    return redirect(url_for("products"))


@app.route("/products/<int:product_id>/suggest")
def product_suggest(product_id: int):
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("products"))
    title = product["title"].strip()
    category = product["category"].strip() or "produto"
    price = money(product["price"])
    desc = product["description"].strip()
    city = get_settings().get("default_city") or "sua região"
    suggested = f"{title} - funcionando / pronto para retirada\n\n{desc}\n\nPreço: {price}. Produto em {city}. Posso enviar mais fotos e vídeo pelo chat. Pode chamar sem novela: se estiver anunciado, ainda está disponível."
    return render_template("suggestion.html", product=product, suggested=suggested)


@app.route("/leads")
def leads():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM leads WHERE 1=1"
    args: list[Any] = []
    if q:
        like = f"%{q}%"
        sql += " AND (name LIKE ? OR city LIKE ? OR tags LIKE ? OR notes LIKE ?)"
        args.extend([like, like, like, like])
    sql += " ORDER BY score DESC, created_at DESC"
    rows = query(sql, tuple(args))
    return render_template("leads.html", leads=rows, q=q)


@app.route("/leads/new", methods=["GET", "POST"])
def lead_new():
    if request.method == "POST":
        execute(
            """
            INSERT INTO leads(name, whatsapp, city, budget, tags, notes, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.form["name"].strip(),
                request.form.get("whatsapp", "").strip(),
                request.form.get("city", "").strip(),
                float(request.form.get("budget") or 0),
                request.form.get("tags", "").strip(),
                clean_description(request.form.get("notes", "")),
                int(request.form.get("score") or 50),
                now_iso(),
            ),
        )
        flash("Lead salvo. Mais uma pessoa no radar. 📡", "success")
        return redirect(url_for("leads"))
    return render_template("lead_form.html", lead=None)


@app.route("/leads/<int:lead_id>/edit", methods=["GET", "POST"])
def lead_edit(lead_id: int):
    lead = query("SELECT * FROM leads WHERE id = ?", (lead_id,), one=True)
    if not lead:
        flash("Lead não encontrado.", "warning")
        return redirect(url_for("leads"))
    if request.method == "POST":
        execute(
            """
            UPDATE leads SET name=?, whatsapp=?, city=?, budget=?, tags=?, notes=?, score=? WHERE id=?
            """,
            (
                request.form["name"].strip(),
                request.form.get("whatsapp", "").strip(),
                request.form.get("city", "").strip(),
                float(request.form.get("budget") or 0),
                request.form.get("tags", "").strip(),
                clean_description(request.form.get("notes", "")),
                int(request.form.get("score") or 50),
                lead_id,
            ),
        )
        flash("Lead atualizado. CRM sem gravata, do jeito certo. 😎", "success")
        return redirect(url_for("leads"))
    return render_template("lead_form.html", lead=lead)


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
def lead_delete(lead_id: int):
    execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    flash("Lead removido.", "info")
    return redirect(url_for("leads"))


@app.route("/listings", methods=["GET", "POST"])
def listings():
    if request.method == "POST":
        execute(
            """
            INSERT INTO listings(product_id, platform, url, status, views, messages, clicks, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(request.form["product_id"]),
                request.form["platform"],
                request.form.get("url", "").strip(),
                request.form.get("status", "ativo"),
                int(request.form.get("views") or 0),
                int(request.form.get("messages") or 0),
                int(request.form.get("clicks") or 0),
                now_iso(),
            ),
        )
        flash("Anúncio/link salvo. Agora ninguém se perde no matagal das plataformas. 🧭", "success")
        return redirect(url_for("listings"))
    products = query("SELECT id, title FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = '' ORDER BY title")
    rows = query(
        """
        SELECT listings.*, products.title AS product_title
        FROM listings
        JOIN products ON products.id = listings.product_id
        ORDER BY listings.created_at DESC
        """
    )
    return render_template("listings.html", listings=rows, products=products)


@app.route("/listings/<int:listing_id>/delete", methods=["POST"])
def listing_delete(listing_id: int):
    execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    flash("Anúncio removido.", "info")
    return redirect(url_for("listings"))


@app.route("/sales", methods=["GET", "POST"])
def sales():
    if request.method == "POST":
        product_id = int(request.form["product_id"])
        buyer_mode = request.form.get("buyer_mode", "custom")
        lead_id = int(request.form["lead_id"]) if buyer_mode == "lead" and request.form.get("lead_id") else None
        register_quick_sale(
            product_id,
            sale_price=float(request.form.get("sale_price") or 0),
            payment_status=request.form.get("payment_status", "pendente"),
            platform=request.form.get("platform", "").strip() or "Venda",
            sold_at=request.form.get("sold_at") or date.today().isoformat(),
            lead_id=lead_id,
            buyer_name=request.form.get("buyer_name", "").strip() if not lead_id else "",
            buyer_whatsapp=request.form.get("buyer_whatsapp", "").strip() if not lead_id else "",
            buyer_source=request.form.get("buyer_source", "").strip() if not lead_id else "",
            notes=clean_description(request.form.get("notes", "")),
        )
        flash("Venda registrada. Pix mental recebido com sucesso. 💸", "success")
        return redirect(url_for("sales"))

    rows = query(
        """
        SELECT sales.*, products.title AS product_title, products.price AS product_price,
               leads.name AS lead_name, leads.whatsapp AS lead_whatsapp
        FROM sales
        JOIN products ON products.id = sales.product_id
        LEFT JOIN leads ON leads.id = sales.lead_id
        ORDER BY sold_at DESC, sales.created_at DESC
        """
    )
    products = query("SELECT id, title, price FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = '' ORDER BY title")
    all_products = query("SELECT id, title, price FROM products WHERE COALESCE(quarantined_at, '') = '' ORDER BY title")
    leads_rows = query("SELECT id, name, whatsapp, city FROM leads ORDER BY name")
    stats = {
        "count": query("SELECT COUNT(*) AS c FROM sales", one=True)["c"],
        "received": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales WHERE payment_status = 'recebido'", one=True)["total"],
        "receivable": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales WHERE payment_status != 'recebido'", one=True)["total"],
        "sold_total": query("SELECT COALESCE(SUM(sale_price), 0) AS total FROM sales", one=True)["total"],
        "average_ticket": query("SELECT COALESCE(AVG(sale_price), 0) AS total FROM sales", one=True)["total"],
        "pending": query("SELECT COUNT(*) AS c FROM sales WHERE payment_status = 'pendente'", one=True)["c"],
        "partial": query("SELECT COUNT(*) AS c FROM sales WHERE payment_status = 'parcial'", one=True)["c"],
        "with_lead": query("SELECT COUNT(*) AS c FROM sales WHERE lead_id IS NOT NULL", one=True)["c"],
        "avulso": query("SELECT COUNT(*) AS c FROM sales WHERE lead_id IS NULL", one=True)["c"],
    }
    return render_template("sales.html", sales=rows, products=products, all_products=all_products, leads=leads_rows, stats=stats)


@app.route("/sales/<int:sale_id>/delete", methods=["POST"])
def sale_delete(sale_id: int):
    execute("DELETE FROM sales WHERE id = ?", (sale_id,))
    flash("Venda removida.", "info")
    return redirect(url_for("sales"))


@app.route("/matches")
def matches():
    product_id = request.args.get("product_id", type=int)
    min_score = request.args.get("min_score", default=1, type=int)
    min_score = max(0, min(100, min_score))
    products = query("SELECT id, title FROM products WHERE status != 'vendido' AND COALESCE(quarantined_at, '') = '' ORDER BY title")
    rows = all_matches(product_id, min_score=min_score)[:100]
    diagnostics = match_diagnostics(product_id)
    return render_template("matches.html", matches=rows, products=products, product_id=product_id, min_score=min_score, diagnostics=diagnostics)


@app.route("/quarantine")
def quarantine():
    rows = query(
        """
        SELECT * FROM products
        WHERE COALESCE(quarantined_at, '') != ''
        ORDER BY quarantined_at DESC
        """
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["days_left"] = quarantine_days_left(row["quarantined_at"])
        item["can_restore"] = quarantine_can_restore(row["quarantined_at"])
        deadline = quarantine_restore_deadline(row["quarantined_at"])
        item["deadline"] = deadline.strftime("%d/%m/%Y") if deadline else "—"
        items.append(item)
    return render_template("quarantine.html", products=items, quarantine_days=QUARANTINE_DAYS)


@app.route("/products/<int:product_id>/restore", methods=["POST"])
def product_restore(product_id: int):
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product or not product_is_quarantined(product):
        flash("Produto não encontrado na quarentena.", "warning")
        return redirect(url_for("quarantine"))
    if not quarantine_can_restore(product["quarantined_at"]):
        flash("O prazo de 30 dias para trazer esse produto de volta acabou. Agora só dá para apagar de vez.", "warning")
        return redirect(url_for("quarantine"))
    execute(
        """
        UPDATE products
        SET status='anunciado', quantity=CASE WHEN quantity <= 0 THEN 1 ELSE quantity END,
            quarantined_at='', quarantine_reason=''
        WHERE id=?
        """,
        (product_id,),
    )
    flash("Produto voltou para a lista. Ressuscitado com sucesso, sem ritual estranho. ♻️", "success")
    return redirect(url_for("products"))


@app.route("/products/<int:product_id>/delete-forever", methods=["POST"])
def product_delete_forever(product_id: int):
    product = query("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("quarantine"))
    execute("DELETE FROM listings WHERE product_id=?", (product_id,))
    execute("DELETE FROM sales WHERE product_id=?", (product_id,))
    execute("DELETE FROM products WHERE id=?", (product_id,))
    flash("Produto apagado de vez. Sumiu do mapa, sem volta.", "info")
    return redirect(url_for("quarantine"))


@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    result = None

    product_rows = query(
        """
        SELECT id, title, price, min_price, quantity, status
        FROM products
        WHERE COALESCE(quarantined_at, '') = ''
        ORDER BY created_at DESC
        LIMIT 80
        """
    )
    sale_rows = query(
        """
        SELECT sales.id, sales.sale_price, sales.payment_status, sales.sold_at,
               products.title AS product_title, products.min_price AS product_min_price,
               leads.name AS lead_name, sales.buyer_name AS buyer_name
        FROM sales
        JOIN products ON products.id = sales.product_id
        LEFT JOIN leads ON leads.id = sales.lead_id
        ORDER BY sales.created_at DESC
        LIMIT 80
        """
    )

    calc_items: list[dict[str, Any]] = []
    for p in product_rows:
        price = float(p["price"] or 0)
        cost = float(p["min_price"] or 0)
        profit = max(price - cost, 0)
        qty = int(p["quantity"] or 1)
        status = p["status"] or "produto"
        calc_items.append({
            "kind": "produto",
            "key": f"product-{p['id']}",
            "label": f"📦 Produto • {p['title']} • {money(price)} • qtd {qty} • {status}",
            "cost": f"{cost:.2f}",
            "profit": f"{profit:.2f}",
            "discount": "0.00",
            "price": f"{price:.2f}",
        })

    for s in sale_rows:
        sale_price = float(s["sale_price"] or 0)
        cost = float(s["product_min_price"] or 0)
        profit = max(sale_price - cost, 0)
        buyer = s["lead_name"] or s["buyer_name"] or "sem comprador"
        sold_at = s["sold_at"] or "sem data"
        calc_items.append({
            "kind": "venda",
            "key": f"sale-{s['id']}",
            "label": f"💸 Venda • {s['product_title']} • {money(sale_price)} • {buyer} • {sold_at}",
            "cost": f"{cost:.2f}",
            "profit": f"{profit:.2f}",
            "discount": "0.00",
            "price": f"{sale_price:.2f}",
        })

    if request.method == "POST":
        cost = float(request.form.get("cost") or 0)
        desired_profit = float(request.form.get("desired_profit") or 0)
        fee_percent = float(request.form.get("fee_percent") or 0)
        shipping = float(request.form.get("shipping") or 0)
        discount = float(request.form.get("discount") or 0)
        recommended = (cost + desired_profit + shipping + discount) / max(0.01, (1 - fee_percent / 100))
        minimum = (cost + shipping) / max(0.01, (1 - fee_percent / 100))
        result = {
            "recommended": recommended,
            "minimum": minimum,
            "profit_at_recommended": recommended - (recommended * fee_percent / 100) - cost - shipping,
            "warning": "Se vender abaixo do mínimo, você está pagando para trabalhar. Aí é poesia triste.",
        }
    return render_template("calculator.html", result=result, calc_items=calc_items)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        current = get_settings()
        theme = request.form.get("theme", DEFAULT_SETTINGS["theme"])
        if theme not in THEMES:
            theme = DEFAULT_SETTINGS["theme"]

        try:
            threshold = int(request.form.get("match_threshold") or DEFAULT_SETTINGS["match_threshold"])
        except ValueError:
            threshold = int(DEFAULT_SETTINGS["match_threshold"])
        threshold = max(25, min(95, threshold))

        login_email = request.form.get("login_email", current.get("login_email", DEFAULT_LOGIN_EMAIL)).strip().lower()
        login_password = request.form.get("login_password", "")
        if not login_password:
            login_password = current.get("login_password") or DEFAULT_LOGIN_PASSWORD

        values = {
            "app_name": request.form.get("app_name", DEFAULT_SETTINGS["app_name"]).strip() or DEFAULT_SETTINGS["app_name"],
            "brand_emoji": request.form.get("brand_emoji", DEFAULT_SETTINGS["brand_emoji"]).strip() or DEFAULT_SETTINGS["brand_emoji"],
            "theme": theme,
            "default_city": request.form.get("default_city", DEFAULT_SETTINGS["default_city"]).strip() or DEFAULT_SETTINGS["default_city"],
            "match_threshold": str(threshold),
            "show_match_alert": "1" if request.form.get("show_match_alert") else "0",
            "match_sound": "1" if request.form.get("match_sound") else "0",
            "fun_mode": "1" if request.form.get("fun_mode") else "0",
            "login_email": login_email or DEFAULT_LOGIN_EMAIL,
            "login_password": login_password,
        }
        save_settings(values)
        session["user_email"] = values["login_email"]
        flash("Configurações salvas. Login, visual e alertas atualizados. ⚙️", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html")


def table_rows(table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in query(f"SELECT * FROM {table}")]


def backup_payload(include_uploads: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "app": "vende_facil",
        "version": 15,
        "exported_at": now_iso(),
        "tables": {},
        "uploads": [],
    }
    for table in ["products", "leads", "listings", "sales", "settings"]:
        data["tables"][table] = table_rows(table)

    if include_uploads and UPLOAD_DIR.exists():
        for path in sorted(UPLOAD_DIR.iterdir()):
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except Exception:
                continue
            if len(raw) > MAX_IMPORT_IMAGE_BYTES:
                continue
            data["uploads"].append({
                "filename": path.name,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "base64": base64.b64encode(raw).decode("ascii"),
            })
    return data


def restore_backup_payload(payload: dict[str, Any]) -> tuple[int, int]:
    if not isinstance(payload, dict) or payload.get("app") != "vende_facil":
        raise ValueError("Arquivo inválido: isso não parece um backup do Vende Fácil.")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("Backup sem bloco de tabelas.")

    db = get_db()
    if not USE_POSTGRES:
        db.execute("PRAGMA foreign_keys = OFF")
    # A ordem respeita as dependências de chave estrangeira (filhos antes dos pais),
    # então funciona tanto no SQLite quanto no Postgres (que sempre valida FKs).
    for table in ["sales", "listings", "products", "leads", "settings"]:
        execute(f"DELETE FROM {table}")

    imported_rows = 0
    for table in ["products", "leads", "listings", "sales", "settings"]:
        rows = tables.get(table, [])
        if not isinstance(rows, list):
            continue
        columns = table_columns(db, table)
        for row in rows:
            if not isinstance(row, dict):
                continue
            clean_row = {key: row.get(key) for key in columns if key in row}
            if not clean_row:
                continue
            placeholders = ", ".join("?" for _ in clean_row)
            col_sql = ", ".join(clean_row.keys())
            execute(f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})", tuple(clean_row.values()))
            imported_rows += 1

    for key, value in DEFAULT_SETTINGS.items():
        if USE_POSTGRES:
            execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (key, value))
        else:
            execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    if not USE_POSTGRES:
        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

    imported_uploads = 0
    uploads = payload.get("uploads", [])
    if isinstance(uploads, list):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for existing in UPLOAD_DIR.iterdir():
            if existing.is_file():
                existing.unlink(missing_ok=True)
        for item in uploads:
            if not isinstance(item, dict):
                continue
            filename = Path(str(item.get("filename") or "")).name
            encoded = item.get("base64")
            if not filename or not encoded:
                continue
            try:
                raw = base64.b64decode(encoded)
            except Exception:
                continue
            if len(raw) > MAX_IMPORT_IMAGE_BYTES:
                continue
            (UPLOAD_DIR / filename).write_bytes(raw)
            imported_uploads += 1
    return imported_rows, imported_uploads


@app.route("/export")
def export_data():
    EXPORT_PATH.write_text(json.dumps(backup_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    return send_file(EXPORT_PATH, as_attachment=True, download_name=f"vende_facil_backup_{date.today().isoformat()}.json")


@app.route("/import-data", methods=["POST"])
def import_data():
    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Escolha um arquivo JSON de backup para importar.", "warning")
        return redirect(url_for("settings"))
    if request.form.get("confirm_replace") != "1":
        flash("Marque a confirmação antes de importar. Backup substitui dados atuais — sem susto ninja.", "warning")
        return redirect(url_for("settings"))
    try:
        payload = json.loads(file.read().decode("utf-8"))
        rows, uploads = restore_backup_payload(payload)
        session.clear()
        flash(f"Backup importado: {rows} registro(s) e {uploads} imagem(ns). Faça login novamente.", "success")
        return redirect(url_for("login"))
    except Exception as exc:
        flash(f"Não consegui importar esse backup: {exc}", "danger")
        return redirect(url_for("settings"))


@app.route("/api/tag-suggestions")
def api_tag_suggestions():
    return jsonify(tag_usage())


@app.route("/about")
def about():
    return render_template("about.html")


with app.app_context():
    init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5433))
    app.run(host="127.0.0.1", port=port, debug=True)
