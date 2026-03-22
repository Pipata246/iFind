"""Ожидания DOM при page_load_strategy=none (driver.get не ждёт событие load)."""

import time


def wait_for_document_ready(driver, timeout_sec: float, stop_event=None) -> bool:
  """Ждём появления body и document.readyState interactive или complete.

  Не ждём загрузки всех картинок/аналитики — этого достаточно для парсинга выдачи.
  """
  deadline = time.monotonic() + float(timeout_sec)
  while time.monotonic() < deadline:
    if stop_event is not None and stop_event.is_set():
      return False
    try:
      ok = driver.execute_script(
        "return document.body && (document.readyState === 'complete' || document.readyState === 'interactive');"
      )
      if ok:
        return True
    except Exception:
      pass
    time.sleep(0.25)
  return False
