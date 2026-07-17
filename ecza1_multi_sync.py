"""
ECZA1 (TÜM MAĞAZALAR) -> QUKASOFT XML FEED
========================================================================
Aynı Ecza1 giriş bilgileriyle (ECZA1_USER/ECZA1_PASS), birden fazla
UserId (mağaza/profil) için sırayla ürün listesi çeker.

Her mağaza için AYRI bir XML dosyası üretir (docs/ecza1-store-{UserId}.xml).
Böylece aynı barkodlu bir ürün farklı mağazalarda farklı miat/fiyatla
bulunsa bile hiçbiri kaybolmaz - her mağaza Qukasoft'ta ayrı bir "Kaynak"
olarak tanımlanır (bir kereliğine kategori/marka = "Genel" eşleştirmesi
yapman yeterli, sonrası otomatik).

Yeni bir mağaza eklemek için: aşağıdaki STORE_USER_IDS listesine
UserId'sini eklemen yeterli - script otomatik olarak onun için de
yeni bir XML dosyası üretecek, sen de Qukasoft'ta bir kereliğine
o dosya linkiyle yeni bir Kaynak tanımlarsın.
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

# Tüm mağaza/profil UserId'leri. Yeni bir mağaza eklemek için buraya
# bir satır daha ekle, başka hiçbir şeyi değiştirmen gerekmez.
STORE_USER_IDS = [
    "7982",    # Ana mağaza (ilk kurduğumuz)
    "16982",
    "19598",
    "21144",
    "184",
    "1735",
    "11197",
    "759",
]

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


def fetch_all_products(session: requests.Session, user_id: str) -> list:
    """
    Belirtilen user_id (mağaza/profil) için GetUserProductList endpoint'ini
    sayfa sayfa çağırıp tüm ürünleri toplar.
    """
    all_products = []
    page_number = 1
    page_size = 50

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
            "UserId": user_id,
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

        logging.info(f"[UserId={user_id}] Sayfa {page_number}: {len(products)} ürün alındı. (Toplam: {total_items})")

        if total_items and len(all_products) >= total_items:
            break
        if len(products) < page_size:
            break  # son sayfa

        page_number += 1

    logging.info(f"[UserId={user_id}] Toplam {len(all_products)} ürün çekildi.")
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
      - Qukasoft, kat1 alanının tamamen boş olmasını kabul etmiyor
        ("Kategori bulunamadı" hatası veriyor). Bu yüzden tüm ürünlere
        DEFAULT_KATEGORI ile sabit, genel bir kategori adı veriyoruz.
        İçeri aktarımdan sonra Qukasoft panelinde bu kategoriyi kendi
        sitendeki gerçek bir kategoriyle eşleştirebilir, ya da ürünleri
        tek tek elle doğru kategorilere taşıyabilirsin.
      - Marka alanı da Ecza1 yanıtında sadece ID olarak geldiği (isim yok)
        için şimdilik boş bırakıldı. İsim eşlemesi istenirse Ecza1'in
        GetFilterMenu endpoint'inden ID->isim tablosu ayrıca çekilmeli.
      - kdv: yüzde oranı olarak gönderiliyor (örn. "1" = %1 KDV).
    """
    KDV_ORANI = "1"        # % olarak KDV oranı
    DEFAULT_KATEGORI = "Genel"  # Qukasoft boş kategori kabul etmediği için sabit değer

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
        lines.append(f"<kat1>{_cdata(DEFAULT_KATEGORI)}</kat1>")
        lines.append("<kat2/>")
        lines.append("<kat3/>")
        lines.append("<kat4/>")
        lines.append("<kat5/>")
        lines.append(f"<marka>{_cdata(DEFAULT_KATEGORI)}</marka>")
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

        for user_id in STORE_USER_IDS:
            try:
                store_products = fetch_all_products(session, user_id)
            except Exception as e:
                logging.error(f"[UserId={user_id}] çekilirken hata oluştu, bu mağaza atlanıyor: {e}")
                continue

            output_path = f"docs/ecza1-store-{user_id}.xml"
            generate_qukasoft_xml(store_products, output_path=output_path)

        logging.info("Tüm mağazalar için senkronizasyon tamamlandı.")
    except Exception as e:
        logging.exception(f"Senkronizasyon hatası: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
