# app.py
# Запуск локально:  uvicorn app:app --host 0.0.0.0 --port 8000
# Требуется: pip install fastapi uvicorn pydantic openai

import os, re, json, time
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ====== OpenAI клиент ======
# Установи ключ:  setx OPENAI_API_KEY "sk-XXXX"  (Windows) или export OPENAI_API_KEY=...
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

# Разреши фронту (GitHub Pages и т.д.) ходить к API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # лучше заменить на конкретные домены фронта
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====== Входные/выходные модели ======
class DetectIn(BaseModel):
    manufacturer: str
    product: str
    extra: Optional[str] = None

class DetectOut(BaseModel):
    code: str
    duty: str
    vat: str
    raw: Optional[str] = None  # для отладки

# ====== Вспомогательные функции ======
TEN_DIGITS = re.compile(r"\b\d{10}\b")

def _take_10digits(s: str) -> str:
    if not s:
        return ""
    m = TEN_DIGITS.search(s.replace(" ", ""))
    return m.group(0) if m else ""

def _norm_percent(s: str) -> str:
    if not s:
        return ""
    s = s.strip().replace(",", ".")
    # вытащим число или число%
    m = re.search(r"(\d+(\.\d+)?)\s*%?", s)
    return (m.group(1) + "%") if m else ""

# ====== Основной эндпоинт ======
@app.post("/tnved/detect", response_model=DetectOut)
def detect(inp: DetectIn):
    """
    Получает 3 поля от фронта, вызывает GPT и возвращает: code, duty, vat.
    """

    # Сформируем "полное имя" как в твоей функции (name_map = [full])
    full = (inp.product or "").strip()
    if inp.extra and inp.extra.strip().lower() != "null":
        full += f" ({inp.extra.strip()})"
    if inp.manufacturer and inp.manufacturer.strip().lower() != "null":
        full += f" — Производитель: {inp.manufacturer.strip()}"

    if not full:
        raise HTTPException(status_code=400, detail="Поля пустые")

    # payload в стиле прежней функции (но с одним товаром)
    payload = {"Товары": [{"Наименование": full}]}

    # ⚠️ ВАЖНО: чтобы снизить «выдумывание» кодов,чуть ужесточим инструкцию:
    system_msg = "Ты — эксперт по классификации товаров по ТН ВЭД ЕАЭС."
    user_msg = (
        "Определи 10-значные коды ТН ВЭД для следующих товаров (возможно несколько вариантов), "
        "а также размер пошлины(%) при импорте в РФ и размер НДС(%).\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "Верни СТРОГО в формате одной строки:\n"
        "<Наименование товара из входных данных> ; <10-значный код ТНВЭД или UNKNOWN>; <Размер пошлины, % или UNKNOWN>; <Размер НДС, % или UNKNOWN>\n"
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=500,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка GPT API: {e}")

    text = (resp.choices[0].message.content or "").strip()

    # Разбор одной строки "name ; code ; duty ; vat"
    code, duty, vat = "", "", ""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) >= 2:
            code = _take_10digits(parts[1]) or ("UNKNOWN" if parts[1].upper().startswith("UNKNOWN") else "")
            duty = _norm_percent(parts[2]) if len(parts) >= 3 else ""
            vat  = _norm_percent(parts[3]) if len(parts) >= 4 else ""
            break

    # Значения по умолчанию (чтобы фронт не падал)
    code = code or "UNKNOWN"
    duty = duty or "UNKNOWN"
    vat  = vat or "UNKNOWN"

    return DetectOut(code=code, duty=duty, vat=vat, raw=text)

# ====== Проверка, что сервер жив ======
@app.get("/")
def root():
    return {"status": "ok", "service": "tnved-api", "time": time.strftime("%Y-%m-%d %H:%M:%S")}


