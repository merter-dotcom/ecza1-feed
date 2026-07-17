"""
FARMAZON.COM.TR (TÜM MAĞAZALAR) -> QUKASOFT XML FEED
========================================================================
1. /api/v1/account/SignIn ile giriş yapıp JWT token alır (konusaneczane)
2. Her sellerId için /api/v1/Listings/GetSellerListingsV3 ile ürünleri çeker
3. Her mağaza için AYRI bir XML dosyası üretir (docs/farmazon-store-{sellerId}.xml)

Yeni bir mağaza eklemek için: SELLER_IDS listesine bir satır eklemen yeterli.

Kimlik bilgileri ortam değişkeninden okunur: FARMAZON_USER, FARMAZON_PASS
"""

import requests
import logging
import os
import sys
import json
import uuid

# ------------------------------------------------------------------
# AYARLAR
# ------------------------------------------------------------------
SIGNIN_URL = "https://lab.farmazon.com.tr/api/v1/account/SignIn"
LISTINGS_URL = "https://lab.farmazon.com.tr/api/v1/Listings/GetSellerListingsV3"

USERNAME = os.environ.get("FARMAZON_USER", "")
PASSWORD = os.environ.get("FARMAZON_PASS", "")

# Network'te görülen sabit değerler (herkes için aynı, "web" istemcisine ait)
CLIENT_NAME = "web"
CLIENT_SECRET_KEY = "aG9ZcTvuYAR66jZz39mD8AMmRTQ6mDUBu3gcXcsmdQix8YidFMuHiioF4VfWCwGZ"

# Tüm mağaza sellerId'leri. Yeni bir mağaza eklemek için buraya
# bir satır daha ekle, başka hiçbir şeyi değiştirmen gerekmez.
SELLER_IDS = [
    "29840",   # konusaneczane ana hesap
    "28752",
    "38552",
    "32899",
    "33302",
    "24084",
    "33517",
    "27424",
    "20275",
]

LOG_PATH = "sync_farmazon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def sign_in() -> str:
    """Farmazon'a giriş yapıp JWT token döner (Bearer olarak kullanılacak)."""
    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "FARMAZON_USER veya FARMAZON_PASS boş geldi - GitHub Secrets doğru "
            "isimlerle tanımlanmamış olabilir."
        )

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": "https://www.farmazon.com.tr",
        "Referer": "https://www.farmazon.com.tr/",
    })

    # visitorId sabit olmak zorunda değil, her çalıştırmada rastgele üretebiliriz
    visitor_id = str(uuid.uuid4())

    payload = {
        "clientName": CLIENT_NAME,
        "clientSecretKey": CLIENT_SECRET_KEY,
        "username": USERNAME,
        "password": PASSWORD,
        "visitorId": visitor_id,
    }

    resp = session.post(SIGNIN_URL, json=payload, timeout=30)

    if resp.status_code != 200:
        logging.error(f"SignIn başarısız (HTTP {resp.status_code}). Sunucu yanıtı: {resp.text[:1000]}")

    resp.raise_for_status()

    try:
        data = resp.json()
    except json.JSONDecodeError:
        logging.error(f"SignIn yanıtı JSON değil. Ham yanıt: {resp.text[:500]}")
        raise RuntimeError("SignIn yanıtı parse edilemedi.")

    token = data.get("token")
    if not token:
        raise RuntimeError(
            f"SignIn yanıtında 'token' alanı bulunamadı. Gelen alanlar: {list(data.keys())}"
        )

    logging.info("Giriş başarılı, token alındı.")
    return token


def fetch_all_products(token: str, seller_id: str) -> list:
    """Belirtilen seller_id için GetSellerListingsV3 endpoint'ini sayfa sayfa çağırır."""
    all_products = []
    page = 1
    count = 50

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    while True:
        params = {
            "sellerId": seller_id,
            "count": count,
            "page": page,
            "sorting": 3,
        }
        resp = requests.get(LISTINGS_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()

        try:
            data = resp.json()
        except json.JSONDecodeError:
            logging.error(f"JSON parse edilemedi. Ham yanıt: {resp.text[:500]}")
            raise

        if isinstance(data, list):
            products = data
            total = None
        else:
            products = data.get("data") or data.get("Data") or data.get("items") or []
            total = data.get("totalCount") or data.get("TotalCount")

        if not products:
            break

        all_products.extend(products)
        logging.info(f"[sellerId={seller_id}] Sayfa {page}: {len(products)} ürün alındı. (Toplam: {total})")

        if total and len(all_products) >= total:
            break
        if len(products) < count:
            break

        page += 1

    logging.info(f"[sellerId={seller_id}] Toplam {len(all_products)} ürün çekildi.")
    return all_products


def _cdata(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("]]>", "]]&gt;")
    return f"<![CDATA[ {text} ]]>"


def generate_qukasoft_xml(products: list, output_path: str):
    """
    NOT: Farmazon ürün objesinin gerçek alan adlarını (barkod, isim, stok,
    fiyat hangi isimlerle geliyor) henüz doğrulamadık - ilk çalıştırmada
    hata/boş XML alırsan, sync_farmazon.log dosyasındaki örnek objeyi
    paylaş, alan adlarını netleştirip düzeltelim.
    """
    DEFAULT_KATEGORI = "Genel"
    KDV_ORANI = "1"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<products>"]

    for p in products:
        lines.append("<product>")
        lines.append(f"<sku>{_cdata(p.get('barcode') or p.get('Barcode'))}</sku>")
        lines.append(f"<name>{_cdata(p.get('title') or p.get('name') or p.get('Title'))}</name>")
        lines.append(f"<quantity>{_cdata(p.get('stock') or p.get('quantity') or 0)}</quantity>")
        lines.append(f"<price>{_cdata(p.get('price') or p.get('Price'))}</price>")
        lines.append(f"<kdv>{_cdata(KDV_ORANI)}</kdv>")
        lines.append("<miat/>")
        lines.append("<miattr/>")
        lines.append(f"<kat1>{_cdata(DEFAULT_KATEGORI)}</kat1>")
        lines.append("<kat2/>")
        lines.append("<kat3/>")
        lines.append("<kat4/>")
        lines.append("<kat5/>")
        lines.append(f"<marka>{_cdata(DEFAULT_KATEGORI)}</marka>")
        lines.append(f"<resim1>{_cdata(p.get('image') or p.get('imageUrl') or p.get('Image'))}</resim1>")
        lines.append("</product>")

    lines.append("</products>")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logging.info(f"XML feed yazıldı: {output_path} ({len(products)} ürün)")

    # Debug için: ilk ürünün ham halini log'a yaz, alan adlarını
    # doğrulamak/düzeltmek gerekirse buradan bakabiliriz.
    if products:
        logging.info(f"İlk ürünün ham verisi (alan adlarını kontrol için): {json.dumps(products[0], ensure_ascii=False)[:1000]}")


def main():
    try:
        token = sign_in()

        for seller_id in SELLER_IDS:
            try:
                products = fetch_all_products(token, seller_id)
            except Exception as e:
                logging.error(f"[sellerId={seller_id}] çekilirken hata oluştu, bu mağaza atlanıyor: {e}")
                continue

            output_path = f"docs/farmazon-store-{seller_id}.xml"
            generate_qukasoft_xml(products, output_path)

        logging.info("Tüm mağazalar için senkronizasyon tamamlandı.")
    except Exception as e:
        logging.exception(f"Senkronizasyon hatası: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
