import random
import re
import time
from time import sleep
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import AVITO_BASE_URL, EXPLICIT_WAIT, VPS_LIGHT_MODE


class AvitoBlockedError(RuntimeError):
  pass


def _filters_to_excel_meta(filters):
  filters = filters or {}
  return {
    "avito_filter_memory": ", ".join(filters.get("memory", [])),
    "avito_filter_ram": ", ".join(filters.get("ram", [])),
    "avito_filter_sim": ", ".join(filters.get("sim", [])),
    "avito_filter_colors": ", ".join(filters.get("colors", [])),
    "avito_filter_condition": ", ".join(filters.get("condition", [])),
    "avito_filter_seller_type": (filters.get("seller_type") or "all"),
    "avito_filter_rating_4_plus": "yes" if filters.get("rating_4_plus") else "no",
  }


def _enrich_items_with_filter_meta(items, filter_meta):
  for item in items:
    item.update(filter_meta)
  return items


def _city_to_slug(city):
  """Преобразует название города в slug для Avito URL (например, 'Самара' -> 'samara')."""
  if not city:
    return ""
  text = city.strip().lower().replace("ё", "е")
  translit = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
  }
  out = []
  for ch in text:
    if ch in translit:
      out.append(translit[ch])
    elif ch.isalnum():
      out.append(ch)
    else:
      out.append("-")
  slug = "".join(out)
  while "--" in slug:
    slug = slug.replace("--", "-")
  return slug.strip("-")


def _precision_params(precision):
  """Поэтапно: 10 = очень медленно (обход лимитов), 1 = быстрее. Все задержки увеличены."""
  max_pages_map = [1, 2, 3, 5, 7, 10, 20, 50, 100, 999]
  max_pages = max_pages_map[min(precision - 1, 9)]
  scroll_passes = max(0, round((precision / 10) * 15))
  # Длинные паузы: скролл как человек (2–4 сек между прокрутками), между страницами 15–45 сек
  scroll_delay = 1.5 + (precision / 10) * 2.5
  page_delay = 15.0 + (precision / 10) * 30.0
  load_delay = 5.0 + (precision / 10) * 5.0
  if VPS_LIGHT_MODE:
    # Lower CPU/RAM pressure and overall runtime for weak VPS.
    max_pages = min(max_pages, 5)
    scroll_passes = max(0, round(scroll_passes * 0.45))
    scroll_delay = max(0.7, scroll_delay * 0.45)
    page_delay = max(4.0, page_delay * 0.25)
    load_delay = max(2.0, load_delay * 0.55)
  return {"max_pages": max_pages, "scroll_passes": scroll_passes, "scroll_delay": scroll_delay, "page_delay": page_delay, "load_delay": load_delay}


def _sleep_with_stop(stop_event, seconds: float, step: float = 0.25):
  """Сон, который прерывается, как только stop_event установлен."""
  if stop_event is None:
    sleep(seconds)
    return
  end = time.time() + float(seconds)
  while True:
    if stop_event.is_set():
      return
    remaining = end - time.time()
    if remaining <= 0:
      return
    sleep(min(step, remaining))


def build_avito_search_url(keyword, model, city, price_min, price_max, page=1):
  q_parts = []
  if keyword:
    q_parts.append(keyword)
  if model:
    q_parts.append(model)
  q = "+".join(q_parts) if q_parts else ""

  params = []
  if q:
    params.append(f"q={q}")
  if price_min is not None:
    params.append(f"pmin={price_min}")
  if price_max is not None:
    params.append(f"pmax={price_max}")
  if page > 1:
    params.append(f"p={page}")

  city_slug = _city_to_slug(city)
  base = AVITO_BASE_URL
  if city_slug:
    # Важно: для Avito город должен быть в пути URL, иначе выдача часто смешивается по регионам.
    base = f"{AVITO_BASE_URL}/{city_slug}"

  if params:
    return f"{base}/?{'&'.join(params)}"
  return base


def _scroll_page(driver, passes, delay, stop_event=None):
  """Скролл по одному шагу с паузой (поэтапно, как человек)."""
  for _ in range(passes):
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    d = random.uniform(delay * 0.8, delay * 1.5)
    _sleep_with_stop(stop_event, d)


def _parse_price_text(price_text):
  if not price_text:
    return None
  digits = re.sub(r"\D", "", price_text)
  return int(digits) if digits else None


def _is_avito_blocked(driver):
  try:
    body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
  except Exception:
    return False, ""
  blocked_markers = (
    "капча",
    "captcha",
    "подтвердите, что вы не робот",
    "доступ ограничен",
    "проблема с ip",
    "слишком много запросов",
    "подозрительная активность",
  )
  for marker in blocked_markers:
    if marker in body_text:
      return True, marker
  return False, ""


def _click_text_option(driver, text, must_be_checkbox=False):
  if not text:
    return False

  def _norm(s):
    if s is None:
      return ""
    return (
      str(s)
      .replace("\u00a0", " ")
      .replace("ё", "е")
      .replace("Ё", "Е")
      .strip()
      .lower()
      .replace(" ", "")
    )

  normalized_target = _norm(text)
  normalized_target = normalized_target.replace("gb", "гб")
  variants = [text, text.replace("+", " + "), text.replace("ё", "е")]
  xpaths = []
  for variant in variants:
    xpaths.extend(
      [
        f"//label[.//*[normalize-space()='{variant}'] or normalize-space()='{variant}']",
        f"//*[self::span or self::div or self::button][normalize-space()='{variant}']",
      ]
    )
  for xpath in xpaths:
    try:
      elems = driver.find_elements(By.XPATH, xpath)
      for elem in elems:
        try:
          driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
          sleep(0.25)
          if must_be_checkbox:
            role = (elem.get_attribute("role") or "").lower()
            tag = (elem.tag_name or "").lower()
            if role == "button" and tag == "button":
              continue
          try:
            elem.click()
          except Exception:
            driver.execute_script("arguments[0].click();", elem)
          return True
        except Exception:
          continue
    except Exception:
      continue

  # Fallback: сравнение текста в Python, чтобы пережить nbsp и мелкие отличия формата.
  try:
    candidates = driver.find_elements(By.CSS_SELECTOR, "label, button, span, div")
  except Exception:
    candidates = []
  for elem in candidates:
    try:
      elem_text = elem.text or ""
      norm_elem = _norm(elem_text).replace("gb", "гб")
      if not norm_elem:
        continue
      if norm_elem == normalized_target:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        sleep(0.2)
        try:
          elem.click()
        except Exception:
          driver.execute_script("arguments[0].click();", elem)
        return True
    except Exception:
      continue
  return False


def _click_show_results_button(driver):
  """Надежно нажимает кнопку 'Показать N объявлений' после фильтров."""
  xpaths = [
    "//button[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
    "//a[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
    "//*[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
  ]
  for xpath in xpaths:
    try:
      elems = driver.find_elements(By.XPATH, xpath)
      for elem in elems:
        try:
          driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
          sleep(0.25)
          try:
            elem.click()
          except Exception:
            driver.execute_script("arguments[0].click();", elem)
          return True
        except Exception:
          continue
    except Exception:
      continue
  return False


def _build_page_url(base_url, page):
  if not base_url:
    return ""
  parsed = urlparse(base_url)
  query = dict(parse_qsl(parsed.query, keep_blank_values=True))
  if page <= 1:
    query.pop("p", None)
  else:
    query["p"] = str(page)
  new_query = urlencode(query, doseq=True)
  return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _detect_total_pages(driver):
  """Определяет число страниц в текущей выдаче Avito после применения фильтров."""
  max_page = 1
  try:
    page_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'p=') or @data-marker]")
  except Exception:
    page_links = []
  for link in page_links:
    try:
      text = (link.text or "").strip()
      href = (link.get_attribute("href") or "").strip()
      if text.isdigit():
        max_page = max(max_page, int(text))
      m = re.search(r"[?&]p=(\d+)", href)
      if m:
        max_page = max(max_page, int(m.group(1)))
    except Exception:
      continue
  return max_page


def _apply_avito_ui_filters(driver, filters):
  if not filters:
    return

  print("[AVITO] Применяю расширенные фильтры в интерфейсе…")
  applied = {
    "memory": 0,
    "ram": 0,
    "sim": 0,
    "colors": 0,
    "condition": 0,
    "seller_type": 0,
    "rating_4_plus": 0,
  }

  for value in filters.get("memory", []):
    if _click_text_option(driver, value, must_be_checkbox=True):
      applied["memory"] += 1
      sleep(0.2)

  for value in filters.get("ram", []):
    if _click_text_option(driver, value, must_be_checkbox=True):
      applied["ram"] += 1
      sleep(0.2)

  for value in filters.get("sim", []):
    if _click_text_option(driver, value, must_be_checkbox=True):
      applied["sim"] += 1
      sleep(0.2)

  for value in filters.get("colors", []):
    if _click_text_option(driver, value, must_be_checkbox=True):
      applied["colors"] += 1
      sleep(0.2)

  for value in filters.get("condition", []):
    if _click_text_option(driver, value, must_be_checkbox=True):
      applied["condition"] += 1
      sleep(0.2)

  seller_type = (filters.get("seller_type") or "all").lower()
  seller_label = {"all": "Все", "private": "Частные", "company": "Компании"}.get(seller_type, "Все")
  if _click_text_option(driver, seller_label, must_be_checkbox=False):
    applied["seller_type"] += 1

  if filters.get("rating_4_plus"):
    if _click_text_option(driver, "4 звезды и выше", must_be_checkbox=True):
      applied["rating_4_plus"] += 1

  # На Avito после выбора фильтров часто требуется кнопка "Показать N объявлений".
  clicked_show = _click_show_results_button(driver)
  if clicked_show:
    print("[AVITO] Нажал кнопку применения фильтров: 'Показать ... объявлений'.")
  else:
    print("[AVITO] Кнопка 'Показать ... объявлений' не найдена. Продолжаю без принудительного клика.")
  sleep(1.5)
  print(
    "[AVITO] Фильтры применены: "
    f"memory={applied['memory']}, ram={applied['ram']}, sim={applied['sim']}, colors={applied['colors']}, "
    f"condition={applied['condition']}, seller={applied['seller_type']}, "
    f"rating4+={applied['rating_4_plus']}"
  )


def _get_cards(driver, wait):
  card_selectors = [
    "[data-marker='item']",
    "div[data-marker='catalog-serp'] div[class*='iva-item-root-']",
    "div[class*='iva-item-root-']",
    "div[class*='iva-item-content-']",
    ".iva-item-content-fRmzq",
  ]
  cards = []
  for selector in card_selectors:
    try:
      wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
      cards = driver.find_elements(By.CSS_SELECTOR, selector)
      if cards:
        return cards
    except Exception:
      continue
  try:
    wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[data-marker='item-title']")))
    title_links = driver.find_elements(By.CSS_SELECTOR, "a[data-marker='item-title']")
    for link in title_links:
      try:
        card = link.find_element(By.XPATH, "./ancestor::*[@data-marker='item'][1]")
        if card:
          cards.append(card)
          continue
      except Exception:
        pass
      try:
        card = link.find_element(By.XPATH, "./ancestor::div[contains(@class,'iva-item-root')][1]")
        if card:
          cards.append(card)
      except Exception:
        pass
    if cards:
      return cards
  except Exception:
    pass
  return []


def _parse_cards_from_html(driver):
  try:
    html = driver.page_source
  except Exception:
    return []
  soup = BeautifulSoup(html, "html.parser")
  items = []
  seen_urls = set()
  for link in soup.select("a[data-marker='item-title'][href]"):
    title = link.get_text(" ", strip=True)
    href = (link.get("href") or "").strip()
    if not href:
      continue
    if href.startswith("/"):
      href = f"{AVITO_BASE_URL}{href}"
    if href in seen_urls:
      continue

    container = link.find_parent(attrs={"data-marker": "item"})
    if container is None:
      container = link.find_parent("div")
    price = None
    city_text = ""
    if container is not None:
      price_tag = container.select_one(
        "[data-marker='item-price'] [data-marker='item-price-value'], "
        "[data-marker='item-price'] span, [data-marker='item-price']"
      )
      if price_tag:
        price = _parse_price_text(price_tag.get_text(" ", strip=True))
      city_tag = container.select_one("[data-marker='item-location']")
      if city_tag:
        city_text = city_tag.get_text(" ", strip=True)

    items.append(
      {
        "platform": "avito",
        "title": title,
        "price": price,
        "url": href,
        "city": city_text or None,
      }
    )
    seen_urls.add(href)
  return items


def _parse_cards_to_items(cards, city, price_min, price_max):
  items = []
  stats = {
    "cards_total": len(cards),
    "parsed_ok": 0,
    "skipped_no_title_or_url": 0,
    "skipped_city": 0,
    "skipped_price": 0,
    "skipped_error": 0,
  }
  for card in cards:
    try:
      title_el = card.find_element(By.CSS_SELECTOR, "a[data-marker='item-title']")
      title = title_el.text.strip()
      href = title_el.get_attribute("href") or ""
      if not title and not href:
        stats["skipped_no_title_or_url"] += 1
        continue

      price = None
      try:
        price_el = card.find_element(
          By.CSS_SELECTOR,
          "[data-marker='item-price'] [data-marker='item-price-value'], "
          "[data-marker='item-price'] span, [data-marker='item-price']",
        )
        price = _parse_price_text(price_el.text)
      except Exception:
        pass

      city_text = ""
      try:
        city_el = card.find_element(By.CSS_SELECTOR, "[data-marker='item-location']")
        city_text = city_el.text.strip()
      except Exception:
        pass

      # Фильтр по городу делаем на уровне URL (/samara/...), а не по тексту карточки.
      # В карточках город может быть указан как район/пригород и давать ложные отсеивания.
      if price_min is not None and price is not None and price < price_min:
        stats["skipped_price"] += 1
        continue
      if price_max is not None and price is not None and price > price_max:
        stats["skipped_price"] += 1
        continue

      items.append({
        "platform": "avito",
        "title": title,
        "price": price,
        "url": href,
        "city": city_text or None,
      })
      stats["parsed_ok"] += 1
    except Exception:
      stats["skipped_error"] += 1
      continue
  return items, stats


def parse_avito(
  driver,
  keyword,
  model,
  city,
  price_min,
  price_max,
  precision=7,
  filters=None,
  stop_event=None,
  raise_on_block=False,
):
  """Поэтапно: одна страница → пауза → скролл по шагам → сбор → длинная пауза → следующая страница."""
  params = _precision_params(precision)
  max_pages = params["max_pages"]
  scroll_passes = params["scroll_passes"]
  scroll_delay = params["scroll_delay"]
  page_delay = params["page_delay"]
  load_delay = params["load_delay"]

  all_items = []
  page = 1
  filter_meta = _filters_to_excel_meta(filters)
  seen_item_keys = set()
  filtered_base_url = ""
  effective_max_pages = max_pages

  # Небольшая пауза перед первым запросом, чтобы не бить сайт сразу
  if page == 1:
    first_delay = random.uniform(3.0, 6.0)
    print(f"[AVITO] Старт через {first_delay:.0f} сек…")
    _sleep_with_stop(stop_event, first_delay)

  while page <= max_pages:
    if stop_event is not None and stop_event.is_set():
      print("[AVITO] Остановка парсинга по запросу пользователя.")
      break
    if filtered_base_url:
      url = _build_page_url(filtered_base_url, page)
    else:
      url = build_avito_search_url(keyword, model, city, price_min, price_max, page=page)
    print(f"[AVITO] Страница {page}/{max_pages}: загрузка…")

    loaded = False
    for attempt in range(1, 4):
      try:
        driver.get(url)
        loaded = True
        break
      except TimeoutException:
        print(f"[AVITO] Таймаут загрузки (попытка {attempt}/3). Пауза 10 сек…")
        _sleep_with_stop(stop_event, 10)
      except (WebDriverException, OSError, Exception) as e:
        err = str(e).lower()
        if "connection" in err or "reset" in err or "10054" in err or "econnreset" in err or "tcp" in err:
          print(f"[AVITO] Обрыв соединения с прокси/сайтом (попытка {attempt}/3). Пауза 10 сек…")
        else:
          print(f"[AVITO] Ошибка загрузки: {e}")
        _sleep_with_stop(stop_event, 10)
    if not loaded:
      print("[AVITO] Не удалось загрузить страницу после 3 попыток. Проверьте прокси и сеть.")
      break

    # Если Avito не открыл нужную страницу p=..., дальше идти бессмысленно
    # (обычно это означает, что страниц больше нет и сайт вернул первую/последнюю).
    current_url = (driver.current_url or "").lower()
    if page > 1 and f"p={page}" not in current_url:
      print(
        f"[AVITO] Страница p={page} не открылась (URL: {driver.current_url}). "
        "Похоже, страниц больше нет."
      )
      break

    delay_after_load = random.uniform(load_delay * 0.8, load_delay * 1.2)
    print(f"[AVITO] Ожидание {delay_after_load:.0f} сек после загрузки…")
    _sleep_with_stop(stop_event, delay_after_load)

    if stop_event is not None and stop_event.is_set():
      print("[AVITO] Остановка парсинга после загрузки страницы.")
      break

    blocked, reason = _is_avito_blocked(driver)
    if blocked:
      msg = (
        f"[AVITO] Обнаружена блокировка/проверка ({reason}). "
        "Подождите 30–60 мин или смените IP/прокси."
      )
      print(msg)
      if raise_on_block:
        raise AvitoBlockedError(msg)
      break

    wait = WebDriverWait(driver, EXPLICIT_WAIT)
    if page == 1 and filters:
      try:
        _apply_avito_ui_filters(driver, filters)
        filtered_base_url = driver.current_url or ""
        detected_pages = _detect_total_pages(driver)
        effective_max_pages = min(max_pages, max(1, detected_pages))
        print(f"[AVITO] Страниц по текущим фильтрам: {detected_pages}. Буду парсить до {effective_max_pages}.")
      except Exception as e:
        print(f"[AVITO] Не удалось применить часть фильтров: {e}")
    if scroll_passes > 0:
      _scroll_page(driver, scroll_passes, scroll_delay, stop_event=stop_event)

    cards = _get_cards(driver, wait)
    used_html_fallback = False
    if cards:
      print(f"[AVITO] Страница {page}: найдено карточек {len(cards)}")
      items, page_stats = _parse_cards_to_items(cards, city, price_min, price_max)
      print(
        f"[AVITO] Страница {page}: parsed={page_stats['parsed_ok']}/{page_stats['cards_total']}, "
        f"added={len(items)}, "
        f"skip_city={page_stats['skipped_city']}, skip_price={page_stats['skipped_price']}, "
        f"skip_other={page_stats['skipped_no_title_or_url'] + page_stats['skipped_error']}"
      )
    else:
      items = _parse_cards_from_html(driver)
      used_html_fallback = True
      if page == 1 and not items:
        print("[AVITO] Не удалось найти объявления ни селекторами, ни через HTML fallback.")
        break
      print(f"[AVITO] Страница {page}: fallback HTML, найдено {len(items)} карточек")

    if price_min is not None or price_max is not None:
      filtered = []
      for item in items:
        price = item.get("price")
        if price_min is not None and price is not None and price < price_min:
          continue
        if price_max is not None and price is not None and price > price_max:
          continue
        filtered.append(item)
      items = filtered

    unique_items = []
    for item in items:
      key = ((item.get("url") or "").strip(), (item.get("title") or "").strip())
      if key in seen_item_keys:
        continue
      seen_item_keys.add(key)
      unique_items.append(item)

    if page > 1 and not unique_items:
      print("[AVITO] На следующей странице нет новых объявлений. Останавливаюсь.")
      break

    all_items.extend(_enrich_items_with_filter_meta(unique_items, filter_meta))
    if not used_html_fallback and len(cards) < 20:
      break
    if used_html_fallback and len(items) < 15:
      break
    page += 1
    if page > effective_max_pages:
      print("[AVITO] Достиг конец страниц по примененным фильтрам.")
      break
    if page <= effective_max_pages:
      delay = random.uniform(page_delay * 0.8, page_delay * 1.3)
      print(f"[AVITO] Пауза {delay:.0f} сек перед следующей страницей (обход лимитов)…")
      _sleep_with_stop(stop_event, delay)

  print(f"[AVITO] Всего объявлений (с повторами): {len(all_items)}")
  return all_items
