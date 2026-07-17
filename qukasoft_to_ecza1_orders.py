"""
QUKASOFT SİPARİŞLERİ -> ECZA1 SEPETİNE OTOMATİK EKLEME
========================================================================
Akış:
1. Qukasoft'tan "hazırlanan siparişler" listesini çeker (Cookie ile,
   çünkü SMS doğrulaması olduğu için otomatik giriş yapılamıyor -
   QUKASOFT_COOKIE secret'ı elle alınan bir oturum çerezidir).
2. Daha önce işlenmemiş (processed_orders.json'da olmayan) siparişleri bulur.
3. Her yeni siparişin detayını açıp içindeki ürünleri (barkod + adet) okur.
4. Bu barkodları, Ecza1'in TÜM mağazalarından çektiğimiz ürün listesiyle
   eşleştirir (hangi mağazada, hangi SiteProductId ile satılıyor).
5. Eşleşen her ürün için Ecza1'de doğru mağazaya geçip (UserProfile),
   sepete ekler (AddBasket).
6. İşlenen sipariş ID'lerini processed_orders.json'a yazar (tekrar
   işlenmesin diye), GitHub Actions bu dosyayı commit'ler.

ÖNEMLİ: Bu script sepete EKLER ama siparişi TAMAMLAMAZ - ödeme/onay adımı
bilinçli olarak insana (sana) bırakılmıştır. Amaç: hazırlık işini
otomatikleştirip, son kontrolü senin yapman.

Kimlik bilgileri:
    ECZA1_USER, ECZA1_PASS       - Ecza1 giriş bilgileri (otomatik login)
    QUKASOFT_COOKIE              - Qukasoft admin paneline elle giriş
                                    yapıp Network sekmesinden alınan tam
                                    Cookie header'ı (SMS doğrulaması
                                    olduğu için otomatik login yapılamıyor,
                                    bu çerez süresi dolunca yenilenmeli)
"""

import requests
import logging
import os
import sys
import json
import re

# ------------------------------------------------------------------
# AYARLAR
# ------------------------------------------------------------------
ECZA1_LOGIN_URL = "https://ecza1.com/Home/Login"
ECZA1_PRODUCT_LIST_URL = "https://ecza1.com/Product/GetUserProductList"
ECZA1_SWITCH_STORE_URL = "https://ecza1.com/Profile/UserProfile"
ECZA1_ADD_BASKET_URL = "https://ecza1.com/Basket/AddBasket"

QUKASOFT_ORDERS_URL = "https://www.eczaneihtiyaclari.com/admin/table/fetch"
QUKASOFT_ORDER_DETAIL_URL = "https://www.eczaneihtiyaclari.com/admin/modal/native"

ECZA1_USER = os.environ.get("ECZA1_USER", "")
ECZA1_PASS = os.environ.get("ECZA1_PASS", "")
QUKASOFT_COOKIE = os.environ.get("QUKASOFT_COOKIE", "")
QUKASOFT_CSRF_TOKEN = os.environ.get("QUKASOFT_CSRF_TOKEN", "")

STORE_USER_IDS = [
    "7982", "16982", "19598", "21144", "184", "1735", "11197", "759", "18",
]

PROCESSED_ORDERS_PATH = "processed_orders.json"
LOG_PATH = "sync_orders.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


# ------------------------------------------------------------------
# ECZA1: GİRİŞ, ÜRÜN LİSTESİ, MAĞAZA DEĞİŞTİRME, SEPETE EKLEME
# ------------------------------------------------------------------

def ecza1_login() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    payload = {"UserName": ECZA1_USER, "Password": ECZA1_PASS}
    resp = session.post(ECZA1_LOGIN_URL, data=payload, allow_redirects=False, timeout=30)

    if resp.status_code == 302:
        logging.info("Ecza1 girişi başarılı.")
        session.get(f"https://ecza1.com{resp.headers.get('Location', '/')}", timeout=30)
    else:
        raise RuntimeError(f"Ecza1 girişi başarısız (HTTP {resp.status_code}).")

    return session


def ecza1_fetch_products_for_store(session: requests.Session, user_id: str) -> list:
    """Tek bir mağazanın tüm ürünlerini çeker (barkod eşleştirmesi için)."""
    all_products = []
    page_number = 1
    page_size = 50

    while True:
        payload = {
            "IsEqualUserId": "", "PageSize": page_size, "PageNumber": page_number,
            "KategoriStr": "0", "AltKategoriStr": "", "MarkaStr": "", "SearchText": "",
            "Menu": "", "EczaciStr": "", "Sira": "7", "UserId": user_id,
            "ListName": "productgridlistTemp3", "OrderBy": "",
        }
        resp = session.post(ECZA1_PRODUCT_LIST_URL, data=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        products = data.get("Data", [])
        total_items = data.get("TotalItems")
        if not products:
            break

        all_products.extend(products)
        if total_items and len(all_products) >= total_items:
            break
        if len(products) < page_size:
            break
        page_number += 1

    return all_products


def build_barcode_index(session: requests.Session) -> dict:
    """
    TÜM mağazaları tarayıp {barkod: {"user_id":..., "site_product_id":..., "stock":...}}
    şeklinde bir arama tablosu oluşturur. Aynı barkod birden fazla mağazada varsa,
    STOĞU EN YÜKSEK olan tercih edilir (sepete eklerken başarısız olma ihtimalini azaltmak için).
    """
    index = {}
    for user_id in STORE_USER_IDS:
        try:
            products = ecza1_fetch_products_for_store(session, user_id)
        except Exception as e:
            logging.warning(f"[UserId={user_id}] ürünler çekilemedi, atlanıyor: {e}")
            continue

        for p in products:
            barcode = p.get("Barcode")
            stock = p.get("Stock", 0)
            site_product_id = p.get("SiteProductId")
            if not barcode or not site_product_id or stock <= 0:
                continue

            existing = index.get(barcode)
            if existing is None or stock > existing["stock"]:
                index[barcode] = {
                    "user_id": user_id,
                    "site_product_id": site_product_id,
                    "stock": stock,
                }

        logging.info(f"[UserId={user_id}] barkod indeksine eklendi ({len(products)} ürün tarandı).")

    logging.info(f"Toplam {len(index)} benzersiz barkod indekslendi.")
    return index


def ecza1_switch_store(session: requests.Session, user_id: str):
    resp = session.get(ECZA1_SWITCH_STORE_URL, params={"sira": 7, "userId": user_id}, timeout=30)
    resp.raise_for_status()
    logging.info(f"Ecza1 aktif mağaza değiştirildi: UserId={user_id}")


def ecza1_add_to_basket(session: requests.Session, site_product_id: str, quantity: int) -> bool:
    payload = {"productId": site_product_id, "quantity": quantity}
    resp = session.post(ECZA1_ADD_BASKET_URL, data=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    if result.get("IsSuccess"):
        msg = result.get("Data", {}).get("CustomMessage", "")
        logging.info(f"Sepete eklendi: productId={site_product_id}, adet={quantity} -> {msg}")
        return True
    else:
        logging.error(f"Sepete eklenemedi: productId={site_product_id} -> {result}")
        return False


# ------------------------------------------------------------------
# QUKASOFT: SİPARİŞ LİSTESİ VE DETAYI (elle alınan Cookie ile)
# ------------------------------------------------------------------

def _refresh_csrf_if_present(session: requests.Session, response_text: str):
    """
    Bazı Qukasoft yanıtları (özellikle HTML dönenler) yeni bir CSRF token
    içerebilir - bulursak session'ı güncelleyip bir sonraki istek için
    taze tutuyoruz (token'ın her istekte rotasyona girme ihtimaline karşı).
    """
    match = re.search(r"updateCsrf\('([^']+)'\)", response_text)
    if match:
        session.headers.update({"Accept-Content-Token": match.group(1)})


def qukasoft_session() -> requests.Session:
    if not QUKASOFT_COOKIE:
        raise RuntimeError("QUKASOFT_COOKIE boş - GitHub Secrets'a eklenmemiş olabilir.")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": QUKASOFT_COOKIE,
        "X-Requested-With": "XMLHttpRequest",
    })

    # CSRF token her sayfa yüklemesinde yenileniyor (rotating token) -
    # bu yüzden statik bir secret olarak saklamak yerine, her çalıştırmada
    # siparişler sayfasını ziyaret edip HTML içindeki updateCsrf('...')
    # çağrısından TAZE token'ı kendimiz çıkarıyoruz.
    page_resp = session.get("https://www.eczaneihtiyaclari.com/admin/orders/pending", timeout=30)
    page_resp.raise_for_status()

    match = re.search(r"updateCsrf\('([^']+)'\)", page_resp.text)
    if not match:
        raise RuntimeError(
            "CSRF token sayfa HTML'inde bulunamadı - Cookie süresi dolmuş olabilir "
            "(SMS ile tekrar giriş yapıp Cookie'yi yenilemen gerekebilir)."
        )

    csrf_token = match.group(1)
    session.headers.update({"Accept-Content-Token": csrf_token})
    logging.info("Taze CSRF token alındı.")

    return session


def qukasoft_fetch_pending_orders(session: requests.Session) -> list:
    payload = {
        "table": "orders",
        "action": "hazirlanan-siparisler",
        "pagination[page]": 1,
        "pagination[perpage]": 50,
        "query": "",
    }
    resp = session.post(QUKASOFT_ORDERS_URL, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "data" not in data:
        raise RuntimeError(
            f"Qukasoft sipariş listesi beklenmedik formatta döndü - Cookie süresi dolmuş "
            f"olabilir, yeniden alman gerekebilir. Ham yanıt: {resp.text[:300]}"
        )

    return data["data"]


def qukasoft_fetch_order_detail(session: requests.Session, order_id: str) -> list:
    """
    Sipariş detay HTML'ini çeker, içindeki ürünleri (barkod + adet) regex ile
    ayıklar. Örnek satır: '#15502 - GHSQDNEND0 - 3939926960860' (barkod son parça,
    her zaman olmayabilir - sadece uzun/tamamen sayısal olan parça barkod sayılır).
    """
    resp = session.get(QUKASOFT_ORDER_DETAIL_URL, params={"modal": "order", "id": order_id}, timeout=30)
    resp.raise_for_status()
    html = resp.text
    _refresh_csrf_if_present(session, html)

    items = []

    # Her ürün bloğunu isim + desc-1 (kod/barkod) + miktar üzerinden ayıkla
    name_pattern = re.compile(r'class="name[^"]*"[^>]*>([^<]+)<')
    desc_pattern = re.compile(r'class=desc-1>([^<]+)<')
    qty_pattern = re.compile(r'<td class=td-quantity>\s*(\d+)\s*<br')

    names = name_pattern.findall(html)
    descs = desc_pattern.findall(html)
    qtys = qty_pattern.findall(html)

    for name, desc, qty in zip(names, descs, qtys):
        # desc örneği: "#15502 - GHSQDNEND0 - 3939926960860 "
        parts = [p.strip() for p in desc.split(" - ")]
        barcode = None
        for part in parts:
            digits_only = re.sub(r"\D", "", part)
            if len(digits_only) >= 10:  # barkodlar genelde 10+ haneli
                barcode = digits_only
                break

        if barcode:
            items.append({
                "name": name.strip(),
                "barcode": barcode,
                "quantity": int(qty),
            })

    return items


# ------------------------------------------------------------------
# İŞLENEN SİPARİŞLERİ TAKİP ETME (tekrar işlememek için)
# ------------------------------------------------------------------

def load_processed_orders() -> set:
    if not os.path.exists(PROCESSED_ORDERS_PATH):
        return set()
    try:
        with open(PROCESSED_ORDERS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_processed_orders(processed: set):
    with open(PROCESSED_ORDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(processed), f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# ANA AKIŞ
# ------------------------------------------------------------------

def main():
    try:
        processed_orders = load_processed_orders()
        logging.info(f"Daha önce işlenmiş {len(processed_orders)} sipariş var.")

        quka_session = qukasoft_session()
        orders = qukasoft_fetch_pending_orders(quka_session)
        logging.info(f"Qukasoft'ta {len(orders)} hazırlanan sipariş bulundu.")

        new_orders = [o for o in orders if o["ID"] not in processed_orders]
        logging.info(f"{len(new_orders)} yeni (işlenmemiş) sipariş var.")

        if not new_orders:
            logging.info("İşlenecek yeni sipariş yok, çıkılıyor.")
            return

        ecza1_session = ecza1_login()
        barcode_index = build_barcode_index(ecza1_session)

        current_active_store = None

        for order in new_orders:
            order_id = order["ID"]
            order_id2 = order["ID2"]
            logging.info(f"--- Sipariş işleniyor: #{order_id2} (ID={order_id}) ---")

            try:
                items = qukasoft_fetch_order_detail(quka_session, order_id)
            except Exception as e:
                logging.error(f"Sipariş #{order_id2} detayı okunamadı, atlanıyor: {e}")
                continue

            any_added = False
            for item in items:
                match = barcode_index.get(item["barcode"])
                if not match:
                    logging.info(
                        f"  Eşleşme yok (Ecza1'de bulunamadı, muhtemelen kendi ürününüz): "
                        f"{item['name']} ({item['barcode']})"
                    )
                    continue

                target_store = match["user_id"]
                if target_store != current_active_store:
                    ecza1_switch_store(ecza1_session, target_store)
                    current_active_store = target_store

                success = ecza1_add_to_basket(
                    ecza1_session, match["site_product_id"], item["quantity"]
                )
                any_added = any_added or success

            if any_added:
                logging.info(f"Sipariş #{order_id2}: en az bir ürün sepete eklendi.")
            else:
                logging.info(f"Sipariş #{order_id2}: Ecza1'de eşleşen ürün bulunamadı.")

            # Eşleşme olsun olmasın, bu siparişi tekrar denemeyelim
            # (istersen bu satırı kaldırıp sadece başarılı olanları işaretleyebiliriz)
            processed_orders.add(order_id)

        save_processed_orders(processed_orders)
        logging.info("Tüm yeni siparişler işlendi.")

    except Exception as e:
        logging.exception(f"Genel hata: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
