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
FILTER_MENU_URL = "https://ecza1.com/Product/GetFilterMenu"

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


def fetch_category_and_brand_maps(session: requests.Session):
    """
    GetFilterMenu endpoint'i, sonuçları 'menu' parametresine göre farklı
    kategori setleri döndürüyor (tek çağrı tüm kategorileri kapsamıyor).
    Bu yüzden makul bir menu aralığını (0-150) tarayıp bulduğumuz tüm
    kategori (Type=1) ve marka (Type=0) ID->isim eşlemelerini birleştiriyoruz.
    Bu işlem sadece çalıştırma başına BİR KEZ yapılır (her ürün için değil).
    """
    category_map = {}
    brand_map = {}

    for menu_id in range(0, 150):
        payload = {
            "categoryId": 0,
            "menu": menu_id,
            "categoryIds": "",
            "brandIds": "",
            "userIds": "",
            "siteIdList": "",
            "searchKey": "",
        }
        try:
            resp = session.post(FILTER_MENU_URL, data=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.warning(f"[menu={menu_id}] GetFilterMenu çekilemedi, atlanıyor: {e}")
            continue

        for item in data.get("Data", []):
            item_id = item.get("Id")
            item_type = item.get("Type")
            item_name = item.get("Name")
            if item_id is None or item_name is None:
                continue
            if item_type == 1:
                category_map[item_id] = item_name
            elif item_type == 0:
                brand_map[item_id] = item_name

    logging.info(f"Kategori/marka tablosu hazır: {len(category_map)} kategori, {len(brand_map)} marka bulundu.")
    return category_map, brand_map



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


def generate_qukasoft_xml(products: list, output_path: str, category_map: dict, brand_map: dict):
    """
    Ecza1'den çekilen ürünleri Qukasoft'un XML İçeri Aktar formatına çevirip
    verilen yola yazar. (Eski, tek-mağaza sürümü - artık ana akışta
    kullanılmıyor, generate_variant_xml() kullanılıyor, ama referans için
    burada bırakıldı.)
    """
    KDV_ORANI = "1"
    DEFAULT_KATEGORI = "Genel"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<products>"]

    for p in products:
        miat, miattr = _parse_miat(p.get("PostMatureDateStr"))
        kategori_adi = category_map.get(p.get("CategoryId"), DEFAULT_KATEGORI)
        marka_adi = brand_map.get(p.get("BrandId"), DEFAULT_KATEGORI)

        lines.append("<product>")
        lines.append(f"<sku>{_cdata(p.get('Barcode'))}</sku>")
        lines.append(f"<name>{_cdata(p.get('Title'))}</name>")
        lines.append(f"<quantity>{_cdata(p.get('Stock', 0))}</quantity>")
        lines.append(f"<price>{_cdata(p.get('UnitPrice'))}</price>")
        lines.append(f"<kdv>{_cdata(KDV_ORANI)}</kdv>")
        lines.append(f"<miat>{_cdata(miat)}</miat>")
        lines.append(f"<miattr>{_cdata(miattr)}</miattr>")
        lines.append(f"<kat1>{_cdata(kategori_adi)}</kat1>")
        lines.append("<kat2/>")
        lines.append("<kat3/>")
        lines.append("<kat4/>")
        lines.append("<kat5/>")
        lines.append(f"<marka>{_cdata(marka_adi)}</marka>")
        lines.append(f"<resim1>{_cdata(p.get('OriginalImageUrl'))}</resim1>")
        lines.append("</product>")

    lines.append("</products>")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logging.info(f"XML feed yazıldı: {output_path} ({len(products)} ürün)")


def generate_variant_xml(products_by_store: dict, output_path: str, category_map: dict, brand_map: dict):
    """
    TÜM mağazaları TEK bir XML dosyasında, Qukasoft'un varyant mantığıyla birleştirir.

    Aynı barkodlu ürün 8 mağazada farklı miat/fiyat/stokla bulunabiliyor.
    Qukasoft'un "Kaynak Etiketleri Eşleştirme" ekranında gördüğümüz gibi:
      - "Ürün Kodu" (sku) AYNI kalırsa, Qukasoft bu satırları TEK ürünün
        FARKLI VARYANTLARI olarak gruplar.
      - Varyant'a özel alanlar (miat, fiyat, stok, görsel, barkod) ek
        <varyantX> etiketleriyle taşınır.

    Bu yüzden burada HER mağazadaki HER ürün için ayrı bir <product> satırı
    yazıyoruz (barkod tekrar etse bile SİLİNMİYOR/BİRLEŞTİRİLMİYOR - önceki
    sürümde yaptığımız "son gelen kazanır" mantığından farklı olarak burada
    her mağazanın kaydı ayrı bir varyant olarak korunuyor).

    products_by_store: {user_id: [ürün, ürün, ...], ...}
    """
    KDV_ORANI = "1"
    DEFAULT_KATEGORI = "Genel"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<products>"]
    total_written = 0

    for user_id, products in products_by_store.items():
        for p in products:
            barcode = p.get("Barcode")
            if not barcode:
                continue  # barkodsuz ürün gruplanamaz, atla

            miat, miattr = _parse_miat(p.get("PostMatureDateStr"))
            kategori_adi = category_map.get(p.get("CategoryId"), DEFAULT_KATEGORI)
            marka_adi = brand_map.get(p.get("BrandId"), DEFAULT_KATEGORI)
            price = p.get("UnitPrice")
            stock = p.get("Stock", 0)
            image = p.get("OriginalImageUrl")
            title = p.get("Title")

            # Varyantı ayırt eden etiket: miat bilgisi (örn. "Aralık 2028"
            # veya "Miadsız Ürün"). Aynı mağazadan aynı miatlı iki farklı
            # kayıt gelmeyeceği için bu, pratikte benzersiz bir ayraç olur.
            varyant_degeri = miattr or "Belirtilmemiş"

            lines.append("<product>")
            # "Ürün Kodu" + "Barkod" -> ikisi de sku'ya eşleniyor, gruplama
            # anahtarı bu: aynı barkod = aynı ana ürün, farklı varyantlar.
            lines.append(f"<sku>{_cdata(barcode)}</sku>")
            lines.append(f"<name>{_cdata(title)}</name>")
            lines.append(f"<quantity>{_cdata(stock)}</quantity>")
            lines.append(f"<price>{_cdata(price)}</price>")
            lines.append(f"<kdv>{_cdata(KDV_ORANI)}</kdv>")
            lines.append(f"<miat>{_cdata(miat)}</miat>")
            lines.append(f"<miattr>{_cdata(miattr)}</miattr>")
            lines.append(f"<kat1>{_cdata(kategori_adi)}</kat1>")
            lines.append("<kat2/>")
            lines.append("<kat3/>")
            lines.append("<kat4/>")
            lines.append("<kat5/>")
            lines.append(f"<marka>{_cdata(marka_adi)}</marka>")
            lines.append(f"<resim1>{_cdata(image)}</resim1>")

            # --- Varyant alanları (Qukasoft'un Kaynak Etiketleri
            #     Eşleştirme ekranındaki "1. Ürün Varyant ..." satırlarıyla
            #     eşleştirilecek yeni etiketler) ---
            lines.append("<varyantbaslik1><![CDATA[ Miat ]]></varyantbaslik1>")
            lines.append(f"<varyantdeger1>{_cdata(varyant_degeri)}</varyantdeger1>")
            lines.append(f"<varyantstok>{_cdata(stock)}</varyantstok>")
            lines.append(f"<varyantbarkod>{_cdata(barcode)}</varyantbarkod>")
            lines.append(f"<varyantfiyat>{_cdata(price)}</varyantfiyat>")
            lines.append(f"<varyantresim>{_cdata(image)}</varyantresim>")

            lines.append("</product>")
            total_written += 1

    lines.append("</products>")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logging.info(f"Varyantlı XML feed yazıldı: {output_path} ({total_written} satır / varyant)")


def main():
    try:
        session = login_and_get_session()

        category_map, brand_map = fetch_category_and_brand_maps(session)

        products_by_store = {}
        for user_id in STORE_USER_IDS:
            try:
                products_by_store[user_id] = fetch_all_products(session, user_id)
            except Exception as e:
                logging.error(f"[UserId={user_id}] çekilirken hata oluştu, bu mağaza atlanıyor: {e}")
                products_by_store[user_id] = []

        generate_variant_xml(products_by_store, XML_OUTPUT_PATH, category_map, brand_map)

        logging.info("Tüm mağazalar için senkronizasyon tamamlandı.")
    except Exception as e:
        logging.exception(f"Senkronizasyon hatası: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
