import os, re, json, time
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
import base64, requests

GH_TOKEN = os.getenv("GITHUB_TOKEN")
GH_OWNER = os.getenv("GH_OWNER")
GH_REPO  = os.getenv("GH_REPO")
GH_PATH  = os.getenv("GH_PATH", "logs.csv")
GH_BRANCH = os.getenv("GH_BRANCH", "main")

def _gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json"}

def _get_current_sha():
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_PATH}"
    r = requests.get(url, headers=_gh_headers())
    if r.status_code == 200:
        return r.json().get("sha")
    return None

def append_row_and_push_to_github(row: list[str]):
    if not (GH_TOKEN and GH_OWNER and GH_REPO):
        return  # нет настроек — пропускаем
    # 1) получаем текущее содержимое файла (если есть)
    url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_PATH}"
    sha = _get_current_sha()
    old = ""
    if sha:
        rget = requests.get(url, headers=_gh_headers())
        if rget.ok:
            content_b64 = rget.json()["content"]
            old = base64.b64decode(content_b64).decode("utf-8", "ignore")

    import io, csv
    output = io.StringIO()
    output.write(old if old else "ts_iso;ip;manufacturer;product;extra;code;duty;vat;user_agent\n")
    w = csv.writer(output, delimiter=";")
    w.writerow(row)
    new_content_b64 = base64.b64encode(output.getvalue().encode("utf-8")).decode("ascii")

    # 3) коммитим обратно
    payload = {
        "message": f"append log {row[0]}",
        "content": new_content_b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    rput = requests.put(url, headers=_gh_headers(), json=payload)
    rput.raise_for_status()
    
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_methods=["*"],
    allow_headers=["*"],
)

class DetectIn(BaseModel):
    manufacturer: str
    product: str
    extra: Optional[str] = None

class DetectOut(BaseModel):
    code: str
    duty: str
    vat: str
    raw: Optional[str] = None 

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

@app.post("/tnved/detect", response_model=DetectOut)
def detect(inp: DetectIn):
    full = (inp.product or "").strip()
    if inp.extra and inp.extra.strip().lower() != "null":
        full += f" ({inp.extra.strip()})"
    if inp.manufacturer and inp.manufacturer.strip().lower() != "null":
        full += f" — Производитель: {inp.manufacturer.strip()}"

    if not full:
        raise HTTPException(status_code=400, detail="Поля пустые")
    payload = {"Товары": [{"Наименование": full}]}
    system_msg = "Ты — эксперт по классификации товаров по ТН ВЭД ЕАЭС."
    user_msg = (
        "Определи 10-значные коды ТН ВЭД для следующего товара,"
        "а также размер пошлины(%) при импорте в РФ и размер НДС(%).\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "Верни СТРОГО в формате одной строки:\n"
        "<Наименование товара из входных данных> ; <10-значный код ТНВЭД>; <Размер пошлины, %>; <Размер НДС, %>\n"
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
    code, duty, vat = "", "", ""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) >= 2:
            code = _take_10digits(parts[1]) or ("UNKNOWN" if parts[1].upper().startswith("UNKNOWN") else "")
            duty = _norm_percent(parts[2]) if len(parts) >= 3 else ""
            vat  = _norm_percent(parts[3]) if len(parts) >= 4 else ""
            break
    code = code or "UNKNOWN"
    duty = duty or "UNKNOWN"
    vat  = vat or "UNKNOWN"
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        row = [ts, ip, inp.manufacturer, inp.product, inp.extra or "", code, duty, vat, ua]
        # и/или в GitHub:
        append_row_and_push_to_github(row)
    except Exception:
        pass
        
    return DetectOut(code=code, duty=duty, vat=vat, raw=text)
@app.get("/")
def root():
    return {"status": "ok", "service": "tnved-api", "time": time.strftime("%Y-%m-%d %H:%M:%S")}






