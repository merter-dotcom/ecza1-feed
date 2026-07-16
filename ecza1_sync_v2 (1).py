"""
ECZA1 -> QUKASOFT XML FEED (GitHub Actions üzerinde otomatik çalışır)
========================================================================

Bu script GitHub Actions tarafından saatte bir otomatik çalıştırılır:
1. Ecza1'e giriş yapar
2. Tüm ürünleri (GetUserProductList JSON endpoint'i ile) çeker
3. Qukasoft'un XML İçeri Aktar formatında bir dosya üretir (docs/ecza1-feed.xml)
4. GitHub Actions bu dosyayı repoya commit'ler
5. GitHub Pages bu dosyayı herkese açık bir URL'de yayınlar
6. Qukasoft, panelde tanımlı Zamanlama'ya göre bu URL'i kendi çeker

Kimlik bilgileri (ECZA1_USER, ECZA1_PASS) GitHub repo Secrets üzerinden
ortam değişkeni olarak gelir - koda hiçbir zaman yazılmaz.
"""

import requests
import logging
import os
import sys
import json

# ------------------------------------------------------------------
# AYARLAR
# ------------------------------------------------------------------
LOGIN_URL = "https://ecza1.com/Home/Login"
PRODUCT_LIST_URL = "https://ecza1.com/Product/GetUserProductList"

USERNAME = os.environ.get("ECZA1_USER", "")
PASSWORD = os.environ.get("ECZA1_PASS", "")

USER_ID = "7982"   # Network'te görülen UserId (hesabına özel, sabit kalır)

# GitHub Actions içinde script, repo'nun kök dizininde çalışır.
# docs/ klasörü GitHub Pages tarafından yayınlanacak klasördür.
XML_OUTPUT_PATH = "docs/ecza1-feed.xml"
LOG_PATH = "sync.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),  # Actions loglarında da görünsün
    ],
)


def login_and_get_session() -> requests.Session:
    """Ecza1'e giriş yapıp oturum (cookie) taşıyan bir session döner."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    payload = {
        "UserName": USERNAME,
        "Password": PASSWORD,
    }

    # allow_redirects=False: başarılı giriş 302 (yönlendirme) döner,
    # bunu net bir başarı sinyali olarak kullanıyoruz.
    resp = session.post(LOGIN_URL, data=payload, allow_redirects=False)

    if resp.status_code == 302:
        logging.info(f"Giriş başarılı (302 yönlendirme -> {resp.headers.get('Location')}).")
        # Yönlendirmeyi manuel takip ederek session cookie'lerin tamamlanmasını sağla
        session.get(f"https://ecza1.com{resp.headers.get('Location', '/')}")
    else:
        raise RuntimeError(
            f"Giriş başarısız görünüyor - beklenen 302, gelen: {resp.status_code}. "
            f"Kullanıcı adı/şifre yanlış olabilir ya da form alanları değişmiş olabilir."
        )

    return session


def fetch_all_products(session: requests.Session) -> list:
    """
    GetUserProductList endpoint'ini sayfa sayfa çağırıp tüm ürünleri toplar.
    PageSize boş geldiğinde sunucu muhtemelen bir default kullanıyor;
    TotalCount alanına bakarak kaç sayfa çekmemiz gerektiğini anlıyoruz.
    """
    all_products = []
    page_number = 1
    page_size = 50  # deneme için makul bir değer; sonuçlara göre ayarlanabilir

    while True:
        payload = {
            "IsEqualUserId": "",
            "PageSize": page_size,
            "PageNumber": page_number,
            "KategoriStr": "0",
            "AltKategoriStr": "",
            "MarkaStr": "",
            "SearchText": "",
            "Menu": "",
            "EczaciStr": "",
            "Sira": "7",
            "UserId": USER_ID,
            "ListName": "productgridlistTemp3",
            "OrderBy": "",
        }

        resp = session.post(PRODUCT_LIST_URL, data=payload)
        resp.raise_for_status()

        try:
            data = resp.json()
        except json.JSONDecodeError:
            logging.error(f"JSON parse edilemedi. Ham yanıt: {resp.text[:500]}")
            raise

        # Gerçek yanıt yapısı: {"TotalItems": 650, "CurrentPage": 1, "Data": [...]}
        products = data.get("Data", [])
        total_items = data.get("TotalItems")

        if not products:
            break

        all_products.extend(products)

        logging.info(f"Sayfa {page_number}: {len(products)} ürün alındı. (Toplam: {total_items})")

        if total_items and len(all_products) >= total_items:
            break
        if len(products) < page_size:
            break  # son sayfa

        page_number += 1

    logging.info(f"Toplam {len(all_products)} ürün çekildi.")
    return all_products


TURKISH_MONTHS = {
    1: "01", 2: "02", 3: "03", 4: "04", 5: "05", 6: "06",
    7: "07", 8: "08", 9: "09", 10: "10", 11: "11", 12: "12",
}
TURKISH_MONTH_NAMES = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5,
    "mayis": 5, "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8,
    "eylül": 9, "eylul": 9, "ekim": 10, "kasım": 11, "kasim": 11,
    "aralık": 12, "aralik": 12,
}

# XML feed'in kaydedileceği yer (repo içinde docs/ klasörü, GitHub Pages
# tarafından otomatik yayınlanır).


def _cdata(value) -> str:
    """Değeri CDATA bloğuna sarar, Qukasoft örnek formatındaki gibi boşluklarla."""
    text = "" if value is None else str(value)
    text = text.replace("]]>", "]]&gt;")  # CDATA içinde kapanış dizisini kaçır
    return f"<![CDATA[ {text} ]]>"


def _parse_miat(post_mature_date_str: str):
    """
    Ecza1'in 'PostMatureDateStr' alanı ('Eylül 2026' gibi) Qukasoft'un
    beklediği 'miat' (MMYY, örn: '0926') ve 'miattr' (orijinal metin) alanlarına çevrilir.
    """
    if not post_mature_date_str or "miadsız" in post_mature_date_str.lower():
        return "", post_mature_date_str or ""

    parts = post_mature_date_str.strip().split()
    if len(parts) != 2:
        return "", post_mature_date_str

    month_name, year = parts[0].lower(), parts[1]
    month_num = TURKISH_MONTH_NAMES.get(month_name)
    if not month_num or not year.isdigit():
        return "", post_mature_date_str

    yy = year[-2:]
    return f"{TURKISH_MONTHS[month_num]}{yy}", post_mature_date_str


def generate_qukasoft_xml(products: list, output_path: str = XML_OUTPUT_PATH):
    """
    Ecza1'den çekilen ürünleri Qukasoft'un XML İçeri Aktar formatına çevirip
    verilen yola yazar. Qukasoft, panelde tanımladığın "Dosya Linki" üzerinden
    bu dosyayı kendi zamanlamasına göre (örn. saatlik) çeker.

    NOT (sınırlılıklar):
      - Kategori (kat1-5) kullanıcı tarafından Qukasoft panelinde manuel
        yönetileceği için burada boş bırakılıyor.
      - Marka alanı da Ecza1 yanıtında sadece ID olarak geldiği (isim yok)
        için şimdilik boş bırakıldı. İsim eşlemesi istenirse Ecza1'in
        GetFilterMenu endpoint'inden ID->isim tablosu ayrıca çekilmeli.
      - kdv: yüzde oranı olarak gönderiliyor (örn. "1" = %1 KDV).
    """
    KDV_ORANI = "1"  # % olarak KDV oranı

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<products>"]

    for p in products:
        miat, miattr = _parse_miat(p.get("PostMatureDateStr"))

        lines.append("<product>")
        lines.append(f"<sku>{_cdata(p.get('Barcode'))}</sku>")
        lines.append(f"<name>{_cdata(p.get('Title'))}</name>")
        lines.append(f"<quantity>{_cdata(p.get('Stock', 0))}</quantity>")
        lines.append(f"<price>{_cdata(p.get('UnitPrice'))}</price>")
        lines.append(f"<kdv>{_cdata(KDV_ORANI)}</kdv>")
        lines.append(f"<miat>{_cdata(miat)}</miat>")
        lines.append(f"<miattr>{_cdata(miattr)}</miattr>")
        lines.append("<kat1/>")
        lines.append("<kat2/>")
        lines.append("<kat3/>")
        lines.append("<kat4/>")
        lines.append("<kat5/>")
        lines.append("<marka/>")
        lines.append(f"<resim1>{_cdata(p.get('OriginalImageUrl'))}</resim1>")
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
