"""
TOPTANCOZUM.COM (B2B) -> QUKASOFT XML FEED
=============================================
İkinci tedarikçi entegrasyonu. Ecza1 scriptiyle aynı mantık, farklı kaynak.

Akış:
1. Login sayfasını GET eder, gizli CSRF token'ı (__RequestVerificationToken) okur
2. Email + Password + token ile POST login yapar (302 bekleniyor)
3. /Store/GetProducts endpoint'ini DataTables formatında sayfa sayfa çağırır
4. Qukasoft XML formatında docs/toptancozum-feed.xml üretir

Kimlik bilgileri ortam değişkeninden okunur: TOPTANCOZUM_EMAIL, TOPTANCOZUM_PASS
"""

import requests
import re
import logging
import os
import sys
import json

# ------------------------------------------------------------------
# AYARLAR
# ------------------------------------------------------------------
LOGIN_PAGE_URL = "https://b2b.toptancozum.com/account/login"   # GET: token okumak için
LOGIN_POST_URL = "https://b2b.toptancozum.com/account/login"   # POST: giriş için (aynı URL)
PRODUCTS_URL = "https://b2b.toptancozum.com/Store/GetProducts"
IMAGE_BASE_URL = "https://b2b.toptancozum.com"

EMAIL = os.environ.get("TOPTANCOZUM_EMAIL", "")
PASSWORD = os.environ.get("TOPTANCOZUM_PASS", "")

XML_OUTPUT_PATH = "docs/toptancozum-feed.xml"
LOG_PATH = "sync_toptancozum.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def login_and_get_session() -> requests.Session:
    """Login sayfasından CSRF token okuyup giriş yapar, session döner."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    # 1) Login sayfasını aç, gizli token'ı regex ile çek
    page = session.get(LOGIN_PAGE_URL, timeout=60)
    page.raise_for_status()

    match = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        page.text,
    )
    if not match:
        raise RuntimeError(
            "CSRF token bulunamadı - login sayfasının HTML yapısı değişmiş olabilir."
        )
    token = match.group(1)
    logging.info("CSRF token okundu.")

    # 2) Token + kimlik bilgileriyle POST
    payload = {
        "Email": EMAIL,
        "Password": PASSWORD,
        "__RequestVerificationToken": token,
        "RememberMe": "false",
    }
    resp = session.post(LOGIN_POST_URL, data=payload, allow_redirects=False, timeout=60)

    if resp.status_code == 302:
        logging.info(f"Giriş başarılı (302 -> {resp.headers.get('Location')}).")
        session.get(f"{IMAGE_BASE_URL}{resp.headers.get('Location', '/')}", timeout=60)
    else:
        raise RuntimeError(
            f"Giriş başarısız - beklenen 302, gelen: {resp.status_code}. "
            f"E-posta/şifre yanlış olabilir ya da form yapısı değişmiş olabilir."
        )

    return session


def fetch_all_products(session: requests.Session) -> list:
    """
    /Store/GetProducts endpoint'ini DataTables formatında sayfa sayfa çağırıp
    tüm ürünleri toplar.
    """
    all_products = []
    start = 0
    length = 100  # tek seferde çekilecek ürün sayısı

    # DataTables'ın beklediği columns[i] tanımları (Network'te görülenler).
    # 'data'/'name' aynı alan adını taşıyor, searchable/orderable bayrakları
    # sitenin orijinal isteğiyle birebir aynı.
    columns = [
        ("data", True, False),
        ("code", True, True),
        ("description", True, True),
        ("boxQuantity", True, False),
        ("packageQuantity", True, True),
        ("stock", True, False),        # tahmini index, hata olursa düzeltilecek
        ("discount1", True, False),
        ("currentPrice", True, True),
        ("vatPercent", True, False),
    ]

    while True:
        logging.info(f"GetProducts isteği gönderiliyor (start={start})...")
        payload = {
            "draw": "1",
            "start": str(start),
            "length": str(length),
            "search[value]": "",
            "search[regex]": "false",
            "order[0][column]": "2",
            "order[0][dir]": "asc",
            "productName": "",
            "brand": "",
            "min": "",
            "max": "",
            "stock": "false",
        }
        for i, (name, searchable, orderable) in enumerate(columns):
            payload[f"columns[{i}][data]"] = name
            payload[f"columns[{i}][name]"] = name
            payload[f"columns[{i}][searchable]"] = str(searchable).lower()
            payload[f"columns[{i}][orderable]"] = str(orderable).lower()
            payload[f"columns[{i}][search][value]"] = ""
            payload[f"columns[{i}][search][regex]"] = "false"

        resp = session.post(
            PRODUCTS_URL,
            data=payload,
            timeout=60,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{IMAGE_BASE_URL}/Store/Index",
            },
        )
        resp.raise_for_status()

        try:
            result = resp.json()
        except json.JSONDecodeError:
            logging.error(f"JSON parse edilemedi. Ham yanıt: {resp.text[:500]}")
            raise

        products = result.get("data", [])
        total = result.get("recordsFiltered") or result.get("recordsTotal")

        if not products:
            break

        all_products.extend(products)
        logging.info(f"start={start}: {len(products)} ürün alındı. (Toplam: {total})")

        if total and len(all_products) >= total:
            break
        if len(products) < length:
            break

        start += length

    logging.info(f"Toplam {len(all_products)} ürün çekildi.")
    return all_products


def _cdata(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("]]>", "]]&gt;")
    return f"<![CDATA[ {text} ]]>"


def generate_qukasoft_xml(products: list, output_path: str = XML_OUTPUT_PATH):
    """
    toptancozum ürünlerini Qukasoft XML formatına çevirir.
    Bu tedarikçide gerçek marka adı (producerDesc) ve gerçek KDV oranı
    (vatPercent) geldiği için onları direkt kullanıyoruz. Kategori için
    yine de Ecza1'deki gibi sabit "Genel" kullanıyoruz (kategori hiyerarşisi
    sadece ID olarak geliyor, isim yok).
    """
    DEFAULT_KATEGORI = "Genel"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<products>"]

    for p in products:
        image_path = p.get("thumbnailUrl") or ""
        full_image_url = f"{IMAGE_BASE_URL}{image_path}" if image_path else ""

        lines.append("<product>")
        lines.append(f"<sku>{_cdata(p.get('barcode'))}</sku>")
        lines.append(f"<name>{_cdata(p.get('description'))}</name>")
        lines.append(f"<quantity>{_cdata(p.get('stock', 0))}</quantity>")
        lines.append(f"<price>{_cdata(p.get('currentPrice'))}</price>")
        lines.append(f"<kdv>{_cdata(p.get('vatPercent'))}</kdv>")
        lines.append("<miat/>")
        lines.append("<miattr/>")
        lines.append(f"<kat1>{_cdata(DEFAULT_KATEGORI)}</kat1>")
        lines.append("<kat2/>")
        lines.append("<kat3/>")
        lines.append("<kat4/>")
        lines.append("<kat5/>")
        lines.append(f"<marka>{_cdata(p.get('producerDesc'))}</marka>")
        lines.append(f"<resim1>{_cdata(full_image_url)}</resim1>")
        lines.append("</product>")

    lines.append("</products>")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logging.info(f"XML feed yazıldı: {output_path} ({len(products)} ürün)")


def main():
    try:
        session = login_and_get_session()
        products = fetch_all_products(session)
        generate_qukasoft_xml(products)
        logging.info("Senkronizasyon tamamlandı.")
    except Exception as e:
        logging.exception(f"Senkronizasyon hatası: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
