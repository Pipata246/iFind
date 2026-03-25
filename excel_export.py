import os
from datetime import datetime

from openpyxl import Workbook


BASE_HEADERS = [
  "platform",
  "title",
  "price",
  "url",
  "city",
]

AVITO_FILTER_HEADERS = [
  "avito_filter_memory",
  "avito_filter_seller_type",
  "avito_filter_applied_mode",
  "avito_ui_applied_note",
]


def export_to_excel(items, filename_prefix="results"):
  if not items:
    print("Нет данных для экспорта в Excel.")
    return None

  ts = datetime.now().strftime("%Y%m%d_%H%M%S")
  filename = f"{filename_prefix}_{ts}.xlsx"
  filepath = os.path.join(os.getcwd(), filename)

  wb = Workbook()
  ws = wb.active

  dynamic_headers = []
  for row in items:
    for key in row.keys():
      if key not in BASE_HEADERS and key not in AVITO_FILTER_HEADERS and key not in dynamic_headers:
        dynamic_headers.append(key)
  headers = BASE_HEADERS + AVITO_FILTER_HEADERS + dynamic_headers
  ws.append(headers)

  for row in items:
    ws.append([row.get(h, "") for h in headers])

  wb.save(filepath)
  print(f"Excel сохранен: {filepath}")
  return filepath

