#!/usr/bin/env python3
"""
Telegram bot that downloads open-access research papers using a fallback chain:
Unpaywall -> OpenAlex -> Semantic Scholar -> Sci-Hub -> Library Genesis ->
Z-Library -> Anna's Archive -> Europe PMC -> arXiv -> Crossref (metadata)

How to run:
  1. pip install python-telegram-bot requests
  2. pip install scihub           # Sci-Hub (optional)
  3. pip install libgen-api       # Library Genesis (optional)
  4. pip install zlibrary-sync    # Z-Library (optional)
  5. set TELEGRAM_BOT_TOKEN=your_token_here
     set UNPAYWALL_EMAIL=your@email.com
  7. python bot.py

Bot searches legal OA repositories first, then falls back to the shadow-library
sources listed above.  Each non-legal source is tried only when the requested
package is installed – the bot logs a warning and skips gracefully otherwise.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ==================== OPTIONAL DEPENDENCIES (graceful import) ====================

_HAS_SCIHUB = False
_HAS_LIBGEN = False
_HAS_ZLIBRARY = False
_HAS_ANNAS = False

try:
    from scihub import SciHub  # type: ignore
    _HAS_SCIHUB = True
except ImportError:
    pass

try:
    from libgen_api import LibgenSearch  # type: ignore
    _HAS_LIBGEN = True
except ImportError:
    pass

try:
    from zlibrary import ZLibraryAPI  # type: ignore
    _HAS_ZLIBRARY = True
except ImportError:
    pass

try:
    import annas_py  # type: ignore
    from annas_py.models.args import FileType  # type: ignore
    _HAS_ANNAS = True
except ImportError:
    pass

# ==================== CONFIGURATION ====================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "YOUR_BOT_TOKEN_HERE"
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL") or "sunsorady32@gmail.com"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
TOR_PROXY = os.getenv("TOR_PROXY")  # e.g. "socks5://127.0.0.1:9050"
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 60
MAX_DOI_PER_HOUR = 5
RATE_LIMIT_WINDOW = 3600  # 1 hour in seconds
REQUIRED_CHANNEL = "@dansmethod"
ADMIN_PASSWORD = "1509"
DATA_FILE = "bot_data.json"

UNPAYWALL_API = "https://api.unpaywall.org/v2"
OPENALEX_API = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/v1/paper"
CROSSREF_API = "https://api.crossref.org/works"

HEADERS = {"User-Agent": "OpenAccessTelegramBot/2.0 (contact: your-email@example.com)"}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DOI_PATTERN = re.compile(r"10\.\d{4,}/[\w\-.():;/]+?(?=[\s,;!?]|$)")

# ==================== RATE LIMITING ====================

_user_doi_times: dict[int, list[float]] = defaultdict(list)


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """Check if *user_id* may make another DOI request.

    Returns ``(allowed, retry_after_seconds)``.
    """
    now = time.time()
    times = _user_doi_times[user_id]
    # Remove entries older than the window
    cutoff = now - RATE_LIMIT_WINDOW
    _user_doi_times[user_id] = [t for t in times if t > cutoff]
    times = _user_doi_times[user_id]

    if len(times) >= MAX_DOI_PER_HOUR:
        retry_after = int(times[0] + RATE_LIMIT_WINDOW - now) + 1
        return False, retry_after

    times.append(now)
    return True, 0


# ==================== CHANNEL MEMBERSHIP CHECK ====================


async def _is_user_member(bot, user_id: int) -> tuple[bool, str]:
    """Check if *user_id* has joined the required channel.

    Returns ``(is_member, channel_invite_link)``.
    The bot must be an admin of the channel for this to work.
    """
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        if member.status in ("member", "administrator", "creator"):
            return True, ""
        return False, REQUIRED_CHANNEL
    except Exception:
        logger.warning("Could not verify channel membership (bot may not be admin)")
        return True, ""


# ==================== DATA PERSISTENCE ====================

_data_lock = threading.Lock()


def _load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"users": [], "banned": []}
    try:
        with _data_lock, open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": [], "banned": []}


def _save_data(data: dict) -> None:
    with _data_lock, open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _track_user(user_id: int) -> None:
    data = _load_data()
    if user_id not in data["users"]:
        data["users"].append(user_id)
        _save_data(data)


def _is_banned(user_id: int) -> bool:
    data = _load_data()
    return user_id in data.get("banned", [])


# ==================== RESULT TYPE ====================


@dataclass
class SourceResult:
    """Unified return type for every PDF source.

    Exactly one of *pdf_bytes* or *pdf_url* is normally set when the source
    successfully locates the paper.  When both are *None* the source had no
    result.
    """

    pdf_bytes: Optional[bytes] = None
    pdf_url: Optional[str] = None

    def __bool__(self) -> bool:
        return self.pdf_bytes is not None or self.pdf_url is not None

    @property
    def too_large(self) -> bool:
        return self.pdf_url is not None and self.pdf_bytes is None


# ==================== HELPERS ====================


def extract_doi(text: str) -> Optional[str]:
    match = DOI_PATTERN.search(text)
    return match.group(0).rstrip(".)") if match else None


BASIC_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📄 Input DOI")],
        [KeyboardButton("ℹ️ Help"), KeyboardButton("👤 About")],
    ],
    resize_keyboard=True,
)


def _solve_nih_pow(html: str) -> Optional[dict]:
    """Solve NIH PMC Proof-of-Work challenge. Returns cookie dict or None."""
    m_challenge = re.search(r'const POW_CHALLENGE\s*=\s*"([^"]+)"', html)
    m_difficulty = re.search(r'const POW_DIFFICULTY\s*=\s*"(\d+)"', html)
    m_cookie_name = re.search(r'const POW_COOKIE_NAME\s*=\s*"([^"]+)"', html)
    if not (m_challenge and m_difficulty):
        return None
    challenge = m_challenge.group(1)
    difficulty = int(m_difficulty.group(1))
    cookie_name = m_cookie_name.group(1) if m_cookie_name else "cloudpmc-viewer-pow"

    prefix = "0" * difficulty
    nonce = 0
    max_nonce = 1_000_000
    logger.info(f"Solving NIH PoW: difficulty={difficulty}, challenge={challenge[:30]}...")
    while nonce < max_nonce:
        data = challenge + str(nonce)
        h = hashlib.sha256(data.encode()).hexdigest()
        if h.startswith(prefix):
            logger.info(f"NIH PoW solved: nonce={nonce}, hash={h[:16]}...")
            return {cookie_name: f"{challenge},{nonce}"}
        nonce += 1
    logger.warning(f"NIH PoW not solved after {max_nonce} iterations")
    return None


def _check_size_before_download(url: str) -> Optional[int]:
    """Issue a HEAD request and return Content-Length in bytes, or *None*."""
    try:
        with requests.head(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        ) as r:
            if r.ok:
                cl = r.headers.get("Content-Length")
                if cl:
                    return int(cl)
    except Exception as exc:
        logger.debug(f"HEAD check failed for {url}: {exc}")
    return None


def download_pdf_bytes(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> SourceResult:
    """Download PDF with redirect detection, HTML check, base64 fallback, and one retry.

    Checks ``Content-Length`` against the 50 MB limit before downloading.
    Returns a :class:`SourceResult` – if the file exceeds the limit the
    result will hold *pdf_url* (the source URL) instead of *pdf_bytes*.
    """
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://scholar.google.com/",
    }
    cookies: dict = {}

    # Quick size check before committing to a full download.
    content_length = _check_size_before_download(url)
    if content_length is not None and content_length > MAX_FILE_SIZE:
        logger.info(
            f"PDF too large ({content_length / 1024 / 1024:.1f} MB > 50 MB), "
            f"returning URL instead: {url}"
        )
        return SourceResult(pdf_url=url)

    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(
                url,
                headers=browser_headers,
                cookies=cookies,
                stream=True,
                timeout=timeout,
                allow_redirects=True,
            ) as r:
                logger.info(
                    f"Attempt {attempt + 1}/{MAX_RETRIES}: status={r.status_code}, "
                    f"final_url={r.url}, "
                    f"Content-Type={r.headers.get('Content-Type', 'N/A')}, "
                    f"Content-Length={r.headers.get('Content-Length', 'unknown')}"
                )
                if r.history:
                    for i, resp in enumerate(r.history):
                        logger.info(f"  Redirect {i + 1}: {resp.status_code} -> {resp.url}")

                if r.status_code >= 400:
                    logger.warning(f"HTTP error {r.status_code} for {url}")
                    if attempt < MAX_RETRIES - 1:
                        continue
                    return SourceResult()

                body_bytes = b"".join(r.iter_content(chunk_size=8192))

                ct = r.headers.get("Content-Type", "").lower()
                if "text/html" in ct:
                    logger.warning(f"HTML response for {url}")
                    if attempt < MAX_RETRIES - 1:
                        decoded_html = body_bytes.decode("utf-8", errors="replace")
                        pow_cookies = _solve_nih_pow(decoded_html)
                        if pow_cookies:
                            logger.info("Retrying NIH URL with PoW cookie")
                            cookies.update(pow_cookies)
                            continue
                    return SourceResult()

                if body_bytes.startswith(b"%PDF"):
                    if len(body_bytes) > MAX_FILE_SIZE:
                        logger.info(
                            f"Downloaded PDF exceeds 50 MB ({len(body_bytes) / 1024 / 1024:.1f} MB), "
                            "returning URL"
                        )
                        return SourceResult(pdf_url=url)
                    return SourceResult(pdf_bytes=body_bytes)

                try:
                    decoded = base64.b64decode(body_bytes)
                    if decoded.startswith(b"%PDF"):
                        logger.info(f"Base64-decoded PDF from {url}")
                        if len(decoded) > MAX_FILE_SIZE:
                            logger.info(
                                f"Decoded PDF exceeds 50 MB "
                                f"({len(decoded) / 1024 / 1024:.1f} MB), returning URL"
                            )
                            return SourceResult(pdf_url=url)
                        return SourceResult(pdf_bytes=decoded)
                except Exception:
                    pass

                logger.warning(f"Not a valid PDF (starts with: {body_bytes[:80]})")
                if attempt < MAX_RETRIES - 1:
                    continue
                return SourceResult()

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout attempt {attempt + 1}/{MAX_RETRIES} for {url}")
            if attempt < MAX_RETRIES - 1:
                continue
            return SourceResult()
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error attempt {attempt + 1}/{MAX_RETRIES} for {url}: {e}")
            if attempt < MAX_RETRIES - 1:
                continue
            return SourceResult()
        except Exception as e:
            logger.error(f"Download error for {url}: {e}")
            return SourceResult()

    return SourceResult()


def safe_filename(doi: str) -> str:
    sanitized = re.sub(r"[^\w\-.]", "_", doi)
    return f"{sanitized}.pdf"


# ==================== SOURCE 1: UNPAYWALL ====================


def get_pdf_from_unpaywall(doi: str) -> SourceResult:
    """Try Unpaywall. Returns a :class:`SourceResult`."""
    url = f"{UNPAYWALL_API}/{doi}"
    params = {"email": UNPAYWALL_EMAIL}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("is_oa"):
            logger.info(f"Unpaywall: {doi} is not open-access")
            return SourceResult()
        best = data.get("best_oa_location")
        if best:
            pdf_url = best.get("url_for_pdf") or best.get("url")
            if pdf_url:
                logger.info(f"Unpaywall: trying best_oa_location URL {pdf_url}")
                result = download_pdf_bytes(pdf_url)
                if result:
                    return result
        for loc in data.get("oa_locations", []):
            pdf_url = loc.get("url_for_pdf") or loc.get("url")
            if pdf_url:
                logger.info(f"Unpaywall: trying oa_location URL {pdf_url}")
                result = download_pdf_bytes(pdf_url)
                if result:
                    return result
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.info(f"Unpaywall: DOI {doi} not found (404)")
        else:
            logger.warning(f"Unpaywall HTTP error for {doi}: {e}")
    except Exception as e:
        logger.warning(f"Unpaywall failed for {doi}: {e}")
    return SourceResult()


# ==================== SOURCE 2: OPENALEX ====================


def get_pdf_from_openalex(doi: str) -> SourceResult:
    """Try OpenAlex. Returns a :class:`SourceResult`."""
    url = f"{OPENALEX_API}/https://doi.org/{doi}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        oa = data.get("open_access")
        if oa and oa.get("is_oa") and oa.get("oa_url"):
            pdf_url = oa["oa_url"]
            logger.info(f"OpenAlex: trying oa_url {pdf_url}")
            result = download_pdf_bytes(pdf_url)
            if result:
                return result
        best = data.get("best_oa_location")
        if best:
            pdf_url = best.get("url_for_pdf") or best.get("url")
            if pdf_url:
                logger.info(f"OpenAlex: trying best_oa_location URL {pdf_url}")
                result = download_pdf_bytes(pdf_url)
                if result:
                    return result
        locations = data.get("oa_locations", [])
        for loc in locations:
            pdf_url = loc.get("url_for_pdf") or loc.get("url")
            if pdf_url:
                logger.info(f"OpenAlex: trying oa_location URL {pdf_url}")
                result = download_pdf_bytes(pdf_url)
                if result:
                    return result
    except Exception as e:
        logger.warning(f"OpenAlex failed for {doi}: {e}")
    return SourceResult()


# ==================== SOURCE 3: SEMANTIC SCHOLAR ====================


def get_pdf_from_semantic_scholar(doi: str) -> SourceResult:
    """Try Semantic Scholar. Returns a :class:`SourceResult`."""
    url = f"{SEMANTIC_SCHOLAR_API}/{doi}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        oa_pdf = data.get("openAccessPdf")
        if oa_pdf:
            pdf_url = oa_pdf if isinstance(oa_pdf, str) else oa_pdf.get("url")
            if pdf_url:
                logger.info(f"Semantic Scholar: trying openAccessPdf {pdf_url}")
                result = download_pdf_bytes(pdf_url)
                if result:
                    return result
        pdf_b64 = data.get("pdf")
        if pdf_b64 and isinstance(pdf_b64, str):
            try:
                decoded = base64.b64decode(pdf_b64)
                if decoded.startswith(b"%PDF"):
                    if len(decoded) > MAX_FILE_SIZE:
                        logger.info("Semantic Scholar: decoded PDF exceeds 50 MB")
                        return SourceResult()
                    logger.info("Semantic Scholar: decoded base64 PDF from response")
                    return SourceResult(pdf_bytes=decoded)
            except Exception:
                pass

        for url_obj in data.get("urls", []):
            if isinstance(url_obj, str) and url_obj.lower().endswith(".pdf"):
                logger.info(f"Semantic Scholar: trying URL {url_obj}")
                result = download_pdf_bytes(url_obj)
                if result:
                    return result
    except Exception as e:
        logger.warning(f"Semantic Scholar failed for {doi}: {e}")
    return SourceResult()


# ==================== SOURCE 4: SCI-HUB ====================


def _get_pdf_from_scihub_sync(doi: str) -> SourceResult:
    """Synchronous helper – called via ``asyncio.to_thread``."""
    if not _HAS_SCIHUB:
        logger.warning("Sci-Hub: package 'scihub' not installed, skipping")
        return SourceResult()
    try:
        sh = SciHub()
        result = sh.download(doi)
        # sh.download returns a dict like {'pdf': <bytes>, ...}
        if isinstance(result, dict):
            pdf_bytes = result.get("pdf")
            if pdf_bytes and pdf_bytes.startswith(b"%PDF"):
                if len(pdf_bytes) > MAX_FILE_SIZE:
                    logger.info(
                        f"Sci-Hub: PDF exceeds 50 MB "
                        f"({len(pdf_bytes) / 1024 / 1024:.1f} MB)"
                    )
                    return SourceResult()
                logger.info("Sci-Hub: PDF downloaded successfully")
                return SourceResult(pdf_bytes=pdf_bytes)
    except Exception as e:
        logger.warning(f"Sci-Hub failed for {doi}: {e}")
    return SourceResult()


async def get_pdf_from_scihub(doi: str) -> SourceResult:
    """Try Sci‑Hub. Returns a :class:`SourceResult`."""
    return await asyncio.to_thread(_get_pdf_from_scihub_sync, doi)


# ==================== SOURCE 5: LIBRARY GENESIS ====================


def _get_pdf_from_libgen_sync(doi: str) -> SourceResult:
    """Synchronous helper – called via ``asyncio.to_thread``."""
    if not _HAS_LIBGEN:
        logger.warning("LibGen: package 'libgen-api' not installed, skipping")
        return SourceResult()
    try:
        lg = LibgenSearch()
        results = lg.search_title(doi)
        if results and len(results) > 0:
            links = lg.resolve_download_links(results[0])
            for mirror_key in ("Mirror_1", "Mirror_2", "Mirror_3"):
                dl_url = links.get(mirror_key)
                if dl_url:
                    logger.info(f"LibGen: trying {mirror_key}: {dl_url}")
                    result = download_pdf_bytes(dl_url)
                    if result:
                        return result
    except Exception as e:
        logger.warning(f"LibGen failed for {doi}: {e}")
    return SourceResult()


async def get_pdf_from_libgen(doi: str) -> SourceResult:
    """Try Library Genesis. Returns a :class:`SourceResult`."""
    return await asyncio.to_thread(_get_pdf_from_libgen_sync, doi)


# ==================== SOURCE 6: Z-LIBRARY ====================


def _get_pdf_from_zlibrary_sync(doi: str) -> SourceResult:
    """Synchronous helper – called via ``asyncio.to_thread``."""
    if not _HAS_ZLIBRARY:
        logger.warning("Z-Library: package 'zlibrary-sync' not installed, skipping")
        return SourceResult()
    try:
        z = ZLibraryAPI()
        results = z.search(doi)
        if results and len(results) > 0:
            details = z.get_book_details(results[0].book_id)
            if details and details.download_url:
                logger.info(f"Z-Library: trying URL {details.download_url}")
                result = download_pdf_bytes(details.download_url)
                if result:
                    return result
    except Exception as e:
        logger.warning(f"Z-Library failed for {doi}: {e}")
    return SourceResult()


async def get_pdf_from_zlibrary(doi: str) -> SourceResult:
    """Try Z-Library. Returns a :class:`SourceResult`."""
    return await asyncio.to_thread(_get_pdf_from_zlibrary_sync, doi)


# ==================== SOURCE 7: ANNA'S ARCHIVE ====================


def _get_pdf_from_annas_archive_sync(doi: str) -> SourceResult:
    """Synchronous helper – called via ``asyncio.to_thread``."""
    if not _HAS_ANNAS:
        logger.warning("Anna's Archive: package 'annas-py' not installed, skipping")
        return SourceResult()
    try:
        results = annas_py.search(doi, file_type=FileType.PDF)
        if results and len(results) > 0:
            info = annas_py.get_informations(results[0].id)
            if info and info.urls:
                for dl_link in info.urls:
                    if dl_link and dl_link.url:
                        logger.info(f"Anna's Archive: trying URL {dl_link.url}")
                        result = download_pdf_bytes(dl_link.url)
                        if result:
                            return result
    except Exception as e:
        logger.warning(f"Anna's Archive failed for {doi}: {e}")
    return SourceResult()


async def get_pdf_from_annas_archive(doi: str) -> SourceResult:
    """Try Anna's Archive. Returns a :class:`SourceResult`."""
    return await asyncio.to_thread(_get_pdf_from_annas_archive_sync, doi)


# ==================== SOURCE 8: EUROPE PMC ====================


def get_pdf_from_europe_pmc(doi: str) -> SourceResult:
    """Try Europe PMC. Returns a :class:`SourceResult`."""
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": f"DOI:{doi}", "format": "json", "pageSize": 1}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("resultList", {}).get("result", [])
        if not results:
            return SourceResult()
        result = results[0]
        source = result.get("source")
        pid = result.get("id")
        pmcid = result.get("pmcid") or result.get("fullTextIdList", {}).get("fullTextId", [None])[0]
        accid = None
        if source == "PMC" and pid:
            accid = f"PMC{pid}"
        elif pmcid:
            accid = pmcid
        if accid:
            pdf_url = (
                f"https://europepmc.org/backend/ptpmcrender.fcgi"
                f"?accid={accid}&blobtype=pdf"
            )
            logger.info(f"Europe PMC: trying {pdf_url}")
            r = download_pdf_bytes(pdf_url)
            if r:
                return r
        for link in result.get("fullTextUrlList", {}).get("fullTextUrl", []):
            if link.get("availability") == "Free" and "pdf" in link.get("documentStyle", "").lower():
                pdf_url = link.get("url")
                if pdf_url:
                    logger.info(f"Europe PMC: trying fullTextUrl {pdf_url}")
                    r = download_pdf_bytes(pdf_url)
                    if r:
                        return r
    except Exception as e:
        logger.warning(f"Europe PMC failed for {doi}: {e}")
    return SourceResult()


# ==================== SOURCE 9: ARXIV ====================


def get_pdf_from_arxiv(doi: str) -> SourceResult:
    """Try arXiv. Returns a :class:`SourceResult`."""
    url = "https://export.arxiv.org/api/query"
    params = {"search_query": f"doi:{doi}", "max_results": 1}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.content)
        for entry in root.findall("atom:entry", ns):
            id_elem = entry.find("atom:id", ns)
            if id_elem is not None:
                arxiv_id = id_elem.text.strip().split("/")[-1].split("v")[0]
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                logger.info(f"arXiv: trying {pdf_url}")
                r = download_pdf_bytes(pdf_url)
                if r:
                    return r
    except Exception as e:
        logger.warning(f"arXiv failed for {doi}: {e}")
    return SourceResult()


# ==================== SOURCE 10: CROSSREF (METADATA ONLY) ====================


def get_metadata_from_crossref(doi: str) -> Optional[str]:
    """Crossref metadata (no PDF). Returns a formatted summary string or None."""
    url = f"{CROSSREF_API}/{doi}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        title = ""
        if msg.get("title"):
            title = msg["title"][0] if isinstance(msg["title"], list) else msg["title"]
        authors = []
        for author in msg.get("author", []):
            given = author.get("given", "")
            family = author.get("family", "")
            name = f"{given} {family}".strip()
            if name:
                authors.append(name)
        abstract = msg.get("abstract", "")
        if abstract:
            abstract = re.sub(r"<[^>]+>", "", abstract)
            if len(abstract) > 500:
                abstract = abstract[:500] + "..."
        parts = []
        if title:
            parts.append(f"*Title:* {title}")
        if authors:
            parts.append(f"*Authors:* {', '.join(authors)}")
        if abstract:
            parts.append(f"*Abstract:* {abstract}")
        if parts:
            parts.append(f"\nDOI: `{doi}`")
            return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Crossref failed for {doi}: {e}")
    return None


# ==================== ASYNC WRAPPERS (existing sync sources) ====================


async def get_pdf_from_unpaywall_async(doi: str) -> SourceResult:
    return await asyncio.to_thread(get_pdf_from_unpaywall, doi)


async def get_pdf_from_openalex_async(doi: str) -> SourceResult:
    return await asyncio.to_thread(get_pdf_from_openalex, doi)


async def get_pdf_from_semantic_scholar_async(doi: str) -> SourceResult:
    return await asyncio.to_thread(get_pdf_from_semantic_scholar, doi)


async def get_pdf_from_europe_pmc_async(doi: str) -> SourceResult:
    return await asyncio.to_thread(get_pdf_from_europe_pmc, doi)


async def get_pdf_from_arxiv_async(doi: str) -> SourceResult:
    return await asyncio.to_thread(get_pdf_from_arxiv, doi)


# ==================== TELEGRAM HANDLERS ====================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📄 *Open Access PDF Bot*\n\n"
        "Send me a DOI (e.g. `10.1038/nature12373`) or paste a link.\n"
        "I'll search legal OA sources first, then shadow libraries.\n\n"
        "Commands:\n"
        "`/doi <DOI>` — fetch a paper by DOI\n"
        "`/help` — show this help\n"
        "`/about` — credits",
        parse_mode="Markdown",
        reply_markup=BASIC_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send any message containing a DOI like `10.1038/nature12373`\n"
        "or use `/doi 10.1038/nature12373`\n\n"
        "Fallback chain:\n"
        "1️⃣ Unpaywall (legal)\n"
        "2️⃣ OpenAlex (legal)\n"
        "3️⃣ Semantic Scholar (legal)\n"
        "4️⃣ Sci‑Hub\n"
        "5️⃣ Library Genesis\n"
        "6️⃣ Z‑Library\n"
        "7️⃣ Anna's Archive\n"
        "8️⃣ Europe PMC\n"
        "9️⃣ arXiv\n"
        "🔟 Crossref (metadata only)\n\n"
        "Sources 4‑6 require an extra Python package (scihub, libgen-api, zlibrary-sync).\n"
        "Max file size: 50 MB. Larger files are returned as download links.",
        parse_mode="Markdown",
        reply_markup=BASIC_KEYBOARD,
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📄 *Open Access PDF Bot*\n\n"
        "📌 *Version:* 2.0\n"
        "👤 *Developer:* DanSun-2026\n"
        "✉️ *Telegram:* @TheGodVann\n\n"
        "🔍 Fetches open-access PDFs via:\n"
        "Unpaywall → OpenAlex → Semantic Scholar → Sci‑Hub →\n"
        "LibGen → Z‑Library → Anna's Archive → Europe PMC → arXiv\n\n"
        "Built with Python · python-telegram-bot · Requests\n"
        "NIH PoW auto-solver included.",
        parse_mode="Markdown",
        reply_markup=BASIC_KEYBOARD,
    )


async def doi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: `/doi <DOI>`", parse_mode="Markdown")
        return
    doi = extract_doi(text)
    if not doi:
        await update.message.reply_text("Could not extract a valid DOI from your input.")
        return
    await handle_doi(update, context, doi)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or update.message.caption or "").strip()

    if text == "📄 Input DOI":
        await update.message.reply_text(
            "Please send me a DOI (e.g. `10.1038/nature12373`) or paste a link containing one.",
            parse_mode="Markdown",
        )
        return
    elif text == "ℹ️ Help":
        await help_command(update, context)
        return
    elif text == "👤 About":
        await about_command(update, context)
        return

    doi = extract_doi(text)
    if not doi:
        await update.message.reply_text(
            "No DOI found. Tap 📄 Input DOI or just type/paste a DOI.",
            reply_markup=BASIC_KEYBOARD,
        )
        return
    await handle_doi(update, context, doi)


async def handle_doi(update: Update, context: ContextTypes.DEFAULT_TYPE, doi: str) -> None:
    user_id = update.effective_user.id if update.effective_user else 0

    if _is_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    _track_user(user_id)

    is_member, channel = await _is_user_member(context.bot, user_id)
    if not is_member:
        await update.message.reply_text(
            f"🔒 You must join {REQUIRED_CHANNEL} to use this bot.\n"
            f"Join here: https://t.me/{REQUIRED_CHANNEL.lstrip('@')}",
        )
        return

    allowed, retry_after = check_rate_limit(user_id)
    if not allowed:
        minutes = retry_after // 60
        seconds = retry_after % 60
        await update.message.reply_text(
            f"⏳ Rate limit reached ({MAX_DOI_PER_HOUR}/hour). "
            f"Please try again in {minutes}m {seconds}s.",
        )
        return

    msg = await update.message.reply_text(
        f"🔍 Searching for DOI: `{doi}` ...", parse_mode="Markdown"
    )

    sources: list[tuple[str, object]] = [
        ("Unpaywall", get_pdf_from_unpaywall_async),
        ("OpenAlex", get_pdf_from_openalex_async),
        ("Semantic Scholar", get_pdf_from_semantic_scholar_async),
        ("Sci‑Hub", get_pdf_from_scihub),
        ("Library Genesis", get_pdf_from_libgen),
        ("Z‑Library", get_pdf_from_zlibrary),
        ("Anna's Archive", get_pdf_from_annas_archive),
        ("Europe PMC", get_pdf_from_europe_pmc_async),
        ("arXiv", get_pdf_from_arxiv_async),
    ]

    result: SourceResult = SourceResult()

    for name, func in sources:
        await msg.edit_text(f"🔍 Trying {name} ...")
        try:
            result = await func(doi)
        except Exception as e:
            logger.warning(f"{name} threw an exception for {doi}: {e}")
            continue

        if result:
            logger.info(f"PDF found via {name} for {doi}")
            if result.too_large:
                await msg.edit_text(
                    f"📄 Paper found via {name} but exceeds 50 MB limit."
                )
            break

    # --- METADATA FALLBACK (if no PDF) ---
    if not result:
        await msg.edit_text(
            "📄 No open-access PDF found. Checking Crossref for metadata ..."
        )
        crossref_data = get_metadata_from_crossref(doi)
        if crossref_data:
            crossref_data += "\n\n_No open-access PDF available._"
            await msg.edit_text(
                crossref_data,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            await msg.edit_text(
                f"❌ Could not find any open-access version or metadata "
                f"for DOI: `{doi}`.",
                parse_mode="Markdown",
            )
        return

    # --- HANDLE LARGE FILES (send link instead of document) ---
    if result.too_large:
        download_url = result.pdf_url or ""
        await msg.edit_text(
            f"📄 *Paper found*\n\n"
            f"DOI: `{doi}`\n\n"
            f"The PDF exceeds Telegram's 50 MB limit.\n"
            f"Download it directly: [Open PDF]({download_url})",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    # --- SEND PDF ---
    await msg.edit_text("✅ PDF found! Downloading ...")

    filename = safe_filename(doi)
    try:
        await update.message.reply_document(
            document=result.pdf_bytes,
            filename=filename,
            caption=f"📄 {doi}\n⚠️ Stored temporarily — download as soon as possible.",
        )
        await msg.delete()
    except Exception as e:
        logger.error(f"Failed to send PDF for {doi}: {e}")
        if result.pdf_url:
            await msg.edit_text(
                f"📄 *Paper found*\n\n"
                f"DOI: `{doi}`\n\n"
                f"Could not send the file via Telegram. "
                f"Download it directly: [Open PDF]({result.pdf_url})",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        else:
            await msg.edit_text(
                f"Failed to send PDF: {e}\n\n"
                f"The PDF bytes were obtained but could not be delivered.",
            )


# ==================== ADMIN COMMANDS ====================


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Authenticate as admin via /admin <password>."""
    if not context.args:
        await update.message.reply_text("Usage: `/admin <password>`", parse_mode="Markdown")
        return
    if context.args[0] != ADMIN_PASSWORD:
        await update.message.reply_text("❌ Wrong password.")
        return
    user_id = update.effective_user.id
    data = _load_data()
    if "admins" not in data:
        data["admins"] = []
    if user_id not in data["admins"]:
        data["admins"].append(user_id)
        _save_data(data)
    await update.message.reply_text(
        "✅ Admin authenticated.\n\n"
        "`/stats` — show usage statistics\n"
        "`/ban <id|@username>` — ban a user\n"
        "`/unban <id|@username>` — unban a user\n"
        "`/broadcast <message>` — message all users",
        parse_mode="Markdown",
    )


def _is_admin(user_id: int) -> bool:
    data = _load_data()
    return user_id in data.get("admins", [])


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    data = _load_data()
    total = len(data.get("users", []))
    banned = len(data.get("banned", []))
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"Total users: `{total}`\n"
        f"Banned users: `{banned}`",
        parse_mode="Markdown",
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/ban <id|@username>`", parse_mode="Markdown")
        return

    target = context.args[0]
    data = _load_data()
    if "banned" not in data:
        data["banned"] = []

    # Try to resolve @username to user_id via a chat lookup
    if target.startswith("@"):
        try:
            chat = await context.bot.get_chat(target)
            target_id = chat.id
        except Exception:
            await update.message.reply_text(f"Could not resolve {target}. Use numeric ID instead.")
            return
    else:
        try:
            target_id = int(target)
        except ValueError:
            await update.message.reply_text("Invalid ID. Use a numeric ID or @username.")
            return

    if target_id not in data["banned"]:
        data["banned"].append(target_id)
        _save_data(data)
    await update.message.reply_text(f"✅ Banned `{target_id}`.", parse_mode="Markdown")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/unban <id|@username>`", parse_mode="Markdown")
        return

    target = context.args[0]
    data = _load_data()

    if target.startswith("@"):
        try:
            chat = await context.bot.get_chat(target)
            target_id = chat.id
        except Exception:
            await update.message.reply_text(f"Could not resolve {target}. Use numeric ID instead.")
            return
    else:
        try:
            target_id = int(target)
        except ValueError:
            await update.message.reply_text("Invalid ID. Use a numeric ID or @username.")
            return

    data["banned"] = [uid for uid in data.get("banned", []) if uid != target_id]
    _save_data(data)
    await update.message.reply_text(f"✅ Unbanned `{target_id}`.", parse_mode="Markdown")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/broadcast <message>`", parse_mode="Markdown")
        return

    text = " ".join(context.args)
    data = _load_data()
    sent = 0
    failed = 0

    await update.message.reply_text("📣 Broadcasting ...")

    for uid in data.get("users", []):
        if uid in data.get("banned", []):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # avoid flood limits

    await update.message.reply_text(
        f"📣 Broadcast done.\nSent: `{sent}`\nFailed: `{failed}`",
        parse_mode="Markdown",
    )


# ==================== HEALTH SERVER (for Render / UptimeRobot) ====================


def _run_health_server() -> None:
    """Minimal HTTP server so Render knows the process is alive."""
    import http.server
    import socketserver

    port = int(os.environ.get("PORT", 8080))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
                self.wfile.flush()
            except OSError:
                pass

        def log_message(self, format, *args) -> None:  # noqa: A002
            logger.debug("Health server: %s", format % args)

    socketserver.TCPServer.allow_reuse_address = True
    while True:
        try:
            with socketserver.TCPServer(("", port), Handler) as httpd:
                logger.info("Health server listening on port %d", port)
                httpd.serve_forever()
        except Exception as e:
            logger.warning("Health server error: %s", e)
            import time
            time.sleep(5)


# ==================== MAIN ====================


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or TELEGRAM_TOKEN
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.error(
            "TELEGRAM_BOT_TOKEN not set. "
            "Run: set TELEGRAM_BOT_TOKEN=your_token_here (Windows) or "
            "export TELEGRAM_BOT_TOKEN=your_token_here (Linux/Mac)"
        )
        return

    # Start health server in a daemon thread (Render / UptimeRobot support)
    import threading
    t = threading.Thread(target=_run_health_server, daemon=True)
    t.start()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("doi", doi_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot started. Polling for updates ...")
    app.run_polling()


if __name__ == "__main__":
    main()
