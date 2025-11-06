# app.py
# Запуск локально:  uvicorn app:app --host 0.0.0.0 --port 8000
# Требуется: pip install fastapi uvicorn openai requests

import os, re, json, time, base64, io, csv
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import requests

# ---------- OpenAI ----------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- GitHub push (logs.csv) ----------
GH_TOKEN  = os.getenv("GITHUB_TOKEN")
GH_OWNER  = os.getenv("GH_OWNER")
GH_REPO   = os.getenv("GH_REPO")
GH_PATH   = os.getenv("GH_PATH", "logs.csv")
GH_BRANCH = os.getenv("GH_BRANCH", "main")

def _gh_headers():
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

def _get_contents():
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_PATH}?ref={GH_BRANCH}"
    return requests.get(url, headers=_gh_headers())

def _put_contents(content_b64: str, sha: Optional[str], message: str):
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_PATH}"
    payload = {
        "message": message,
        "content": content_b64,
        "branch": GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    return requests.put(url, headers=_gh_headers(), json=payload)
    
def _clean_field(val) -> str:
    """Убираем переводы строк, лишние пробелы и ; (чтоб не ломать CSV)."""
    if val is None:
        return ""
    s = str(val)
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace(";", ",")
    return s

def _stringify_tech31(val) -> str:
    """Принимает str | dict | list и всегда возвращает человекочитаемую строку."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        # Превратим разделы в маркированные строки
        parts = []
        for k, v in val.items():
            k_s = str(k).strip().capitalize()
            if isinstance(v, (list, tuple)):
                v_s = "; ".join(str(x).strip() for x in v if str(x).strip())
            elif isinstance(v, dict):
                v_s = "; ".join(f"{kk}: {vv}" for kk, vv in v.items())
            else:
                v_s = str(v).strip()
            if v_s:
                parts.append(f"- {k_s}: {v_s}")
        return "\n".join(parts)
    if isinstance(val, (list, tuple)):
        return "\n".join(f"- {str(x).strip()}" for x in val if str(x).strip())
    return str(val).strip()

def _normalize_alternatives(val):
    """Вернёт список словарей вида {'code': str, 'reason': str}."""
    out = []
    if isinstance(val, dict):
        # иногда модель присылает {код: причина}
        for k, v in val.items():
            out.append({"code": str(k), "reason": str(v)})
    elif isinstance(val, (list, tuple)):
        for it in val:
            if isinstance(it, dict):
                out.append({"code": str(it.get("code","") or it.get("код","") or ""), 
                            "reason": str(it.get("reason","") or it.get("обоснование","") or "")})
            else:
                out.append({"code": str(it), "reason": ""})
    elif val:
        out.append({"code": str(val), "reason": ""})
    return out

def _normalize_payments(val, fallback_duty: str, fallback_vat: str):
    """Гарантирует словарь со строками."""
    d = {"duty": fallback_duty, "vat": fallback_vat, "excise": "—", "fees": "—"}
    if isinstance(val, dict):
        for k in ("duty","vat","excise","fees"):
            if k in val and val[k] is not None:
                d[k] = str(val[k]).strip()
    return d

def _normalize_requirements(val):
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        # разбить по точкам/переводам строкам, если пришла строка
        import re as _re
        items = [s.strip(" -•\t") for s in _re.split(r"[\n;]+", val) if s.strip()]
        return items or [val.strip()]
    if val:
        return [str(val)]
    return []

def append_row_and_push_to_github(row: list[str]) -> None:
    """Простая реализация: читаем текущий файл, дописываем строку, кладём обратно."""
    if not (GH_TOKEN and GH_OWNER and GH_REPO):
        return

    # 1) забираем текущее содержимое
    rget = _get_contents()
    old = ""
    sha = None
    if rget.status_code == 200:
        js = rget.json()
        sha = js.get("sha")
        old_b64 = js.get("content", "")
        if old_b64:
            old = base64.b64decode(old_b64).decode("utf-8", "ignore")
    elif rget.status_code not in (404, 409):
        # не валим основной поток
        print("[logs->github] GET error:", rget.status_code, rget.text[:200])
        return

    # 2) формируем новый CSV
    out = io.StringIO()
    if old:
        out.write(old)
    else:
        out.write("ts_iso;ip;manufacturer;product;extra;code;duty;vat;user_agent\n")
    csv.writer(out, delimiter=";").writerow(row)
    content_b64 = base64.b64encode(out.getvalue().encode("utf-8")).decode("ascii")

    # 3) кладём
    rput = _put_contents(content_b64, sha, message=f"append log {row[0]}")
    if rput.status_code in (200, 201):
        return
    if rput.status_code == 409:
        # попробуем ещё раз с актуальным sha
        rget2 = _get_contents()
        if rget2.status_code == 200:
            sha2 = rget2.json().get("sha")
            rput2 = _put_contents(content_b64, sha2, message=f"append log {row[0]} (retry)")
            if rput2.status_code in (200, 201):
                return
            print("[logs->github] PUT retry error:", rput2.status_code, rput2.text[:200])
            return
    print("[logs->github] PUT error:", rput.status_code, rput.text[:200])

# ---------- FastAPI ----------
app = FastAPI()

# Разрешим фронту с GitHub Pages/домена ходить к API
# При желании подставь конкретные домены вместо "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Модели ----------
class DetectIn(BaseModel):
    manufacturer: str
    product: str
    extra: Optional[str] = None

class AltItem(BaseModel):
    code: Optional[str] = None
    reason: Optional[str] = None

class Payments(BaseModel):
    duty: Optional[str] = None
    vat: Optional[str] = None
    excise: Optional[str] = None
    fees: Optional[str] = None

class DetectOut(BaseModel):
    code: str
    duty: str
    vat: str
    raw: Optional[str] = None
    # дополнительные поля для твоих пяти секций:
    description: Optional[str] = None
    tech31: Optional[str] = None
    classification_reason: Optional[str] = None
    alternatives: Optional[List[AltItem]] = None
    payments: Optional[Payments] = None
    requirements: Optional[List[str]] = None

# ---------- Хелперы парсинга ----------
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
    m = re.search(r"(\d+(\.\d+)?)\s*%?", s)
    return (m.group(1) + "%") if m else ""

def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Пытаемся вытащить JSON из ответа модели (на случай, если он с преамбулой/код-блоком)."""
    if not text:
        return None
    # чаще всего JSON идёт с первого '{' до последней '}'
    try:
        start = text.index("{")
        end = text.rfind("}")
        blob = text[start:end+1]
        return json.loads(blob)
    except Exception:
        return None

# ---------- Эндпоинты ----------
@app.post("/tnved/detect", response_model=DetectOut)
def detect(inp: DetectIn, request: Request):
    # Собираем "полное имя" как в твоей логике
    full = (inp.product or "").strip()
    if inp.extra and inp.extra.strip().lower() != "null":
        full += f" ({inp.extra.strip()})"
    if inp.manufacturer and inp.manufacturer.strip().lower() != "null":
        full += f" — Производитель: {inp.manufacturer.strip()}"

    if not full:
        raise HTTPException(status_code=400, detail="Поля пустые")

    # Формируем жёсткий промпт: просим СТРОГИЙ JSON
    # Заполняем поля для всех твоих секций
    system_msg = "Ты — эксперт по классификации товаров по ТН ВЭД ЕАЭС."
    user_msg = (
        "Определи 10-значный код ТН ВЭД для товара и верни результат СТРОГО в виде JSON.\n"
        "Вход:\n"
        f"{json.dumps({'Наименование': full}, ensure_ascii=False)}\n\n"
        "При формировании description и tech31:\n"
        "- обязательно укажи, что товар НЕ военного назначения, НЕ двойного назначения, НЕ лом электрооборудования;\n"
        "- объём tech31 не меньше 50 слов;\n"
        "- оформи tech31 структурированно: назначение; состав; материалы; электрические параметрыусловия эксплуатации; комплектация.\n"
        "- если нет точных данных, делай осторожные формулировки («по предоставленному описанию») и не выдумывай марки/модели.\n\n"
        "Требуемый JSON-ответ на русском:\n"
        "{\n"
        '  "code": "10-значный код или UNKNOWN",\n'
        '  "duty": "проценты или UNKNOWN",\n'
        '  "vat": "проценты или UNKNOWN",\n'
        '  "description": "краткое описание с явными фразами: не военного, не двойного назначения, не лом электрооборудования",\n'
        '  "tech31": "подробное структурированное техописание (см. чек-лист выше)",\n'
        '  "classification_reason": "обоснование выбора позиции ТН ВЭД (ОПИ, примечания, пояснения)",\n'
        '  "alternatives": [ {"code":"возможный_код","reason":"когда может применяться"} ],\n'
        '  "payments": {"duty":"%","vat":"%","excise":"— или значение","fees":"— или значение"},\n'
        '  "requirements": ["ТР ЕАЭС/лицензии/сертификация по необходимости"]\n'
        "}\n"
        "Не добавляй никаких пояснений вне JSON."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"system","content":system_msg},{"role":"user","content":user_msg}],
            temperature=0.2,
            top_p=1.0,
            frequency_penalty=0.2,
            max_tokens=1800,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка GPT API: {e}")

    text = (resp.choices[0].message.content or "").strip()

    # 1) Пытаемся распарсить JSON как просили
    data = _extract_json_block(text) or {}

    # 2) Фолбэк: если код не пришёл — пытаемся выдернуть из всего текста
    code = (data.get("code") or "").strip()
    if not _take_10digits(code):
        guessed = _take_10digits(text)
        code = guessed or (code if code.upper().startswith("UNKNOWN") else "")

    duty = _norm_percent(data.get("duty") or "")
    vat  = _norm_percent(data.get("vat") or "")

    # Значения по умолчанию (чтобы фронт не падал)
    code = code or "UNKNOWN"
    duty = duty or "UNKNOWN"
    vat  = vat or "UNKNOWN"
    tech31 = _stringify_tech31(data.get("tech31"))
    alternatives = _normalize_alternatives(data.get("alternatives"))
    payments = _normalize_payments(data.get("payments"), fallback_duty=duty, fallback_vat=vat)
    requirements = _normalize_requirements(data.get("requirements"))

    # Подготовим расширенный ответ под фронт
    out = DetectOut(
        code=code,
        duty=duty,
        vat=vat,
        raw=text,
        description=(data.get("description") or ""),
        tech31=tech31,
        classification_reason=(data.get("classification_reason") or ""),
        alternatives=alternatives,
        payments=payments,
        requirements=requirements,
    )

    # ЛОГИ в GitHub (не критично к ошибкам)
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        
        row = [
            ts,
            _clean_field(ip),
            _clean_field(inp.manufacturer),
            _clean_field(inp.product),
            _clean_field(inp.extra),
            _clean_field(code),
            _clean_field(duty),
            _clean_field(vat),
            _clean_field(ua),
        ]
        append_row_and_push_to_github(row)
    except Exception as e:
        print("[logs] error:", e)

    return out

@app.get("/")
def root():
    return {"status": "ok", "service": "tnved-api", "time": time.strftime("%Y-%m-%d %H:%M:%S")}








