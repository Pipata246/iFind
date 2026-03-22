import random
import re
from datetime import datetime
from pathlib import Path
from time import sleep
import requests

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from browser_helpers import wait_for_document_ready
from config import (
    DOCUMENT_READY_TIMEOUT,
    EXPLICIT_WAIT,
    WB_BASE_URL,
    VPS_LIGHT_MODE,
    USE_MOBILE_PROXY,
    MOBILE_PROXY_HOST,
    MOBILE_PROXY_PORT,
    MOBILE_PROXY_USER,
    MOBILE_PROXY_PASS,
)


def build_wb_search_url(keyword, model, price_min=None, price_max=None, page=1):
    query_parts = []
    if keyword:
        query_parts.append(keyword.strip())
    if model:
        query_parts.append(model.strip())
    query = "+".join([p for p in query_parts if p])
    if not query:
        return WB_BASE_URL

    base = f"{WB_BASE_URL}/catalog/0/search.aspx?page={page}&sort=popular&search={query}"
    if price_min is not None and price_max is not None:
        base += f"&priceU={int(price_min) * 100}%3B{int(price_max) * 100}"
    elif price_min is not None:
        base += f"&priceU={int(price_min) * 100}%3B999999999"
    elif price_max is not None:
        base += f"&priceU=0%3B{int(price_max) * 100}"
    return base


def _precision_params(precision):
    max_pages_map = [1, 1, 2, 3, 5, 7, 10, 20, 50, 100]
    max_pages = max_pages_map[min(precision - 1, 9)]
    scroll_passes = max(1, round((precision / 10) * 10))
    scroll_delay = 1.0 + (precision / 10) * 2.0
    page_delay = 10.0 + (precision / 10) * 20.0
    load_delay = 3.0 + (precision / 10) * 4.0
    if VPS_LIGHT_MODE:
        max_pages = min(max_pages, 4)
        scroll_passes = max(1, round(scroll_passes * 0.5))
        scroll_delay = max(0.6, scroll_delay * 0.5)
        page_delay = max(3.0, page_delay * 0.3)
        load_delay = max(1.5, load_delay * 0.6)
    return {
        "max_pages": max_pages,
        "scroll_passes": scroll_passes,
        "scroll_delay": scroll_delay,
        "page_delay": page_delay,
        "load_delay": load_delay,
    }


def _scroll_page(driver, passes, delay):
    for _ in range(passes):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        sleep(random.uniform(delay * 0.8, delay * 1.5))


def _is_blocked(driver):
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        return False
    markers = (
        "подозрительная активность",
        "что-то не так",
        "captcha",
        "новая попытка через",
        "access denied",
    )
    return any(m in body_text for m in markers)


def _extract_block_details(driver):
    """Достает полезные поля из антибот-страницы WB для логов."""
    try:
        html = driver.page_source or ""
    except Exception:
        html = ""
    req_id = ""
    req_ip = ""
    retry_after = ""
    id_match = re.search(r'data-req-uuid="([^"]+)"', html)
    ip_match = re.search(r'data-req-ip="([^"]+)"', html)
    retry_match = re.search(r"Новая попытка через\s*([0-9:]+)", html)
    if id_match:
        req_id = id_match.group(1)
    if ip_match:
        req_ip = ip_match.group(1)
    if retry_match:
        retry_after = retry_match.group(1)
    return req_id, req_ip, retry_after


def _save_debug_snapshot(driver, tag):
    """Сохраняет HTML текущей страницы для диагностики проблем WB."""
    try:
        html = driver.page_source or ""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path.cwd() / f"wb_debug_{tag}_{ts}.html"
        out_path.write_text(html, encoding="utf-8", errors="ignore")
        print(f"[WB] Диагностика сохранена: {out_path}")
    except Exception:
        pass


def _get_proxy_outbound_ip():
    if not (USE_MOBILE_PROXY and MOBILE_PROXY_HOST and str(MOBILE_PROXY_HOST).strip()):
        return ""
    proxy_url = f"http://{MOBILE_PROXY_USER}:{MOBILE_PROXY_PASS}@{MOBILE_PROXY_HOST}:{MOBILE_PROXY_PORT}"
    try:
        r = requests.get(
            "https://api.ipify.org",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=15,
        )
        if r.ok:
            return (r.text or "").strip()
    except Exception:
        return ""
    return ""


def _parse_cards_bs(html):
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.find_all("article")

    items = []
    for article in articles:
        try:
            name_tag = article.find("h2")
            if not name_tag:
                name_tag = article.find("span", class_=lambda c: c and "name" in c.lower())
            name = name_tag.get_text(strip=True) if name_tag else ""
            if not name:
                continue

            price = None
            price_tag = article.find("ins")
            if not price_tag:
                price_tag = article.find("span", class_=lambda c: c and "lower-price" in c.lower())
            if price_tag:
                digits = "".join(ch for ch in price_tag.get_text(strip=True) if ch.isdigit())
                if digits:
                    price = int(digits)

            url = ""
            link_tag = article.find("a", href=True)
            if link_tag:
                href = link_tag.get("href", "")
                url = f"https://www.wildberries.ru{href}" if href.startswith("/") else href

            date_text = ""
            date_candidates = article.find_all(
                ["span", "div", "p"],
                class_=lambda c: c and ("time" in c.lower() or "date" in c.lower()),
            )
            for d in date_candidates:
                t = (d.get_text(" ", strip=True) or "").strip()
                if t:
                    date_text = t
                    break

            items.append({
                "platform": "wb",
                "title": name,
                "price": price,
                "url": url,
                "city": None,
                "date_text": date_text or None,
            })
        except Exception:
            continue

    return items


def _is_today_text(text):
    s = (text or "").strip().lower()
    if not s:
        return False
    return "сегодня" in s or "today" in s


def parse_wb(driver, keyword, model, price_min=None, price_max=None, precision=7, wb_url=None, wb_today_only=False):
    params = _precision_params(precision)
    max_pages = params["max_pages"]
    scroll_passes = params["scroll_passes"]
    scroll_delay = params["scroll_delay"]
    page_delay = params["page_delay"]
    load_delay = params["load_delay"]

    all_items = []
    page = 1
    seen_keys = set()
    # "Без перебоев": не падаем сразу, а пробуем страницу несколько раз с длинными паузами.
    max_page_attempts = 3

    first_delay = random.uniform(2.0, 5.0)
    print(f"[WB] Старт через {first_delay:.0f} сек…")
    sleep(first_delay)

    while page <= max_pages:
        if USE_MOBILE_PROXY:
            ip = _get_proxy_outbound_ip()
            if ip:
                print(f"[WB] Текущий внешний IP через прокси: {ip}")

        if wb_url:
            url = wb_url
        else:
            url = build_wb_search_url(keyword, model, price_min=price_min, price_max=price_max, page=page)
        print(f"[WB] Страница {page}/{max_pages}: загрузка…")

        page_items = []
        page_ready = False
        for page_attempt in range(1, max_page_attempts + 1):
            loaded = False
            for attempt in range(1, 4):
                try:
                    driver.get(url)
                    print(f"[WB] Ожидание готовности страницы (до {DOCUMENT_READY_TIMEOUT} сек)…")
                    if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT):
                        raise TimeoutException("document.readyState не достиг готовности")
                    loaded = True
                    break
                except TimeoutException:
                    print(f"[WB] Таймаут загрузки (попытка {attempt}/3). Пауза 10 сек…")
                    sleep(10)
                except (WebDriverException, OSError) as e:
                    err = str(e).lower()
                    if "connection" in err or "tunnel" in err or "reset" in err:
                        print(f"[WB] Проблема с соединением (попытка {attempt}/3). Пауза 10 сек…")
                    else:
                        print(f"[WB] Ошибка загрузки: {e}")
                    sleep(10)

            if not loaded:
                continue

            delay_after_load = random.uniform(load_delay * 0.8, load_delay * 1.2)
            print(f"[WB] Ожидание {delay_after_load:.0f} сек после загрузки…")
            sleep(delay_after_load)

            if _is_blocked(driver):
                req_id, req_ip, retry_after = _extract_block_details(driver)
                print(f"[WB] Антибот на странице {page}, попытка {page_attempt}/{max_page_attempts}.")
                if req_id or req_ip or retry_after:
                    print(
                        f"[WB] Детали блока: req_id={req_id or 'n/a'}, "
                        f"ip={req_ip or 'n/a'}, retry_after={retry_after or 'n/a'}"
                    )
                _save_debug_snapshot(driver, f"blocked_page{page}")
                if page_attempt < max_page_attempts:
                    if USE_MOBILE_PROXY:
                        cooldown = 130
                        print("[WB] Жду 130 сек для ротации мобильного прокси и пробую снова…")
                    else:
                        cooldown = random.uniform(45, 90)
                        print(f"[WB] Жду {cooldown:.0f} сек и пробую снова…")
                    sleep(cooldown)
                    if USE_MOBILE_PROXY:
                        new_ip = _get_proxy_outbound_ip()
                        if new_ip:
                            print(f"[WB] Новый внешний IP после ожидания: {new_ip}")
                    continue
                print("[WB] Обнаружена защита от ботов. Парсинг WB остановлен.")
                return all_items

            if scroll_passes > 0:
                _scroll_page(driver, scroll_passes, scroll_delay)

            wait = WebDriverWait(driver, EXPLICIT_WAIT, poll_frequency=1)
            try:
                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.product-card-list, section.product-card-list")
                ))
            except TimeoutException:
                print(f"[WB] Не дождался контейнера карточек (попытка {page_attempt}/{max_page_attempts}).")
                _save_debug_snapshot(driver, f"timeout_page{page}")
                if page_attempt < max_page_attempts:
                    retry_pause = random.uniform(20, 40)
                    print(f"[WB] Пауза {retry_pause:.0f} сек перед повтором…")
                    sleep(retry_pause)
                    continue
                return all_items

            try:
                container = driver.find_element(
                    By.CSS_SELECTOR, "div.product-card-list, section.product-card-list"
                )
                html = container.get_attribute("outerHTML")
            except Exception:
                print("[WB] Не удалось получить HTML контейнера карточек.")
                _save_debug_snapshot(driver, f"container_error_page{page}")
                if page_attempt < max_page_attempts:
                    sleep(random.uniform(15, 30))
                    continue
                return all_items

            page_items = _parse_cards_bs(html)
            page_ready = True
            break

        if not page_ready:
            return all_items

        added = 0
        for item in page_items:
            if wb_today_only and not _is_today_text(item.get("date_text")):
                continue
            key = (item.get("url") or "", item.get("title") or "")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_items.append(item)
            added += 1
        print(f"[WB] Страница {page}: найдено {len(page_items)}, добавлено новых {added}")

        if wb_url or len(page_items) < 10:
            break

        page += 1
        if page <= max_pages:
            delay = random.uniform(page_delay * 0.8, page_delay * 1.3)
            print(f"[WB] Пауза {delay:.0f} сек перед следующей страницей…")
            sleep(delay)

    print(f"[WB] Всего товаров: {len(all_items)}")
    return all_items
