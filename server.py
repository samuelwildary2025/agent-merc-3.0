"""
Servidor FastAPI para receber mensagens do WhatsApp e processar com o agente
Suporta: Texto, Áudio (Transcrição), Imagem (Visão) e PDF (Extração de Texto + Link)
# touch: reload marker
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import requests
from datetime import datetime
import time
import random
import threading
import re
import io

# Tenta importar pypdf
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from config.settings import settings
from config.logger import setup_logger
from agent_langgraph_simple import run_agent_langgraph as run_agent, get_session_history
from tools.redis_tools import (
    push_message_to_buffer,
    get_buffer_length,
    pop_all_messages,
    set_agent_cooldown,
    is_agent_in_cooldown,
)

logger = setup_logger(__name__)

app = FastAPI(title="Agente de Supermercado", version="1.5.4")

# --- Models ---
class WhatsAppMessage(BaseModel):
    telefone: str
    mensagem: str
    message_id: Optional[str] = None
    timestamp: Optional[str] = None
    message_type: Optional[str] = "text"

class AgentResponse(BaseModel):
    success: bool
    response: str
    telefone: str
    timestamp: str
    error: Optional[str] = None

# --- Helpers ---

def get_api_base_url() -> str:
    return (settings.uaz_api_url or settings.whatsapp_api_url or "").strip().rstrip("/")

def get_media_url_uaz(message_id: str) -> Optional[str]:
    if not message_id: return None
    base = get_api_base_url()
    if not base: return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/message/download"
    except:
        url = f"{base.split('/message')[0]}/message/download"
    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    payload = {"id": message_id, "return_link": True, "return_base64": False}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            link = data.get("fileURL") or data.get("url")
            if link: return link
    except Exception as e:
        logger.error(f"Erro ao obter link mídia: {e}")
    return None

def process_pdf_uaz(message_id: str) -> Optional[str]:
    if not PdfReader:
        return "[Erro: sistema não suporta leitura de PDF]"
    url = get_media_url_uaz(message_id)
    if not url: return None
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        f = io.BytesIO(response.content)
        reader = PdfReader(f)
        text_content = []
        for page in reader.pages:
            text_content.append(page.extract_text())
        full_text = "\n".join(text_content)
        return re.sub(r'\s+', ' ', full_text).strip()
    except Exception as e:
        logger.error(f"Erro ao ler PDF: {e}")
        return None

def transcribe_audio_uaz(message_id: str) -> Optional[str]:
    if not message_id: return None
    base = get_api_base_url()
    if not base: return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/message/download"
    except:
        url = f"{base.split('/message')[0]}/message/download"
    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    payload = {"id": message_id, "transcribe": True, "return_link": False, "openai_apikey": settings.openai_api_key}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
        if resp.status_code == 200:
            return resp.json().get("transcription")
    except: pass
    return None

def _extract_incoming(payload: Dict[str, Any]) -> Dict[str, Any]:
    def _sanitize_phone(raw: Any) -> Optional[str]:
        if not raw: return None
        s = str(raw)
        if "@" in s: s = s.split("@")[0]
        if ":" in s: s = s.split(":")[-1]
        return re.sub(r"\D", "", s) or None

    chat = payload.get("chat") or {}
    message_any = payload.get("message") or {}
    if isinstance(payload.get("messages"), list):
        try:
            m0 = payload["messages"][0]
            message_any = m0
            chat = {"wa_id": m0.get("sender") or m0.get("chatid")}
        except: pass

    mensagem_texto = payload.get("text")
    message_id = payload.get("id") or payload.get("messageid")
    from_me = False
    
    raw_type = str(message_any.get("messageType") or "").lower()
    media_type = str(message_any.get("mediaType") or "").lower()
    base_type = str(message_any.get("type") or "").lower()
    mimetype = str(message_any.get("mimetype") or "").lower()
    
    message_type = "text"
    if "audio" in raw_type or "ptt" in media_type or "audio" in base_type:
        message_type = "audio"
    elif "image" in raw_type or "image" in media_type or "image" in base_type:
        message_type = "image"
    elif "document" in raw_type or "document" in base_type or "application/pdf" in mimetype:
        message_type = "document"

    if isinstance(message_any, dict):
        message_id = message_any.get("messageid") or message_any.get("id") or message_id
        from_me = bool(message_any.get("fromMe") or message_any.get("wasSentByApi") or False)
        content = message_any.get("content")
        if isinstance(content, str) and not mensagem_texto:
            mensagem_texto = content
        elif isinstance(content, dict):
            mensagem_texto = content.get("text") or content.get("caption") or mensagem_texto
        if not mensagem_texto:
            txt = message_any.get("text")
            if isinstance(txt, dict):
                mensagem_texto = txt.get("body")
            else:
                mensagem_texto = txt or message_any.get("body")

    if from_me:
        candidates = [chat.get("wa_id"), chat.get("phone"), payload.get("sender")]
    else:
        candidates = [payload.get("from"), chat.get("wa_id"), message_any.get("sender") if isinstance(message_any, dict) else None]
    telefone = next((_sanitize_phone(c) for c in candidates if _sanitize_phone(c)), None)

    if message_type == "audio" and not mensagem_texto:
        if message_id:
            trans = transcribe_audio_uaz(message_id)
            mensagem_texto = f"[Áudio]: {trans}" if trans else "[Áudio inaudível]"
        else:
            mensagem_texto = "[Áudio sem ID]"
    elif message_type == "image":
        caption = mensagem_texto or ""
        if message_id:
            url = get_media_url_uaz(message_id)
            mensagem_texto = f"{caption} [MEDIA_URL: {url}]".strip() if url else f"{caption} [Imagem recebida]".strip()
        else:
            mensagem_texto = f"{caption} [Imagem recebida]".strip()
    elif message_type == "document":
        if "pdf" in mimetype or (mensagem_texto and ".pdf" in str(mensagem_texto).lower()):
            pdf_url = get_media_url_uaz(message_id) if message_id else None
            pdf_text = ""
            if message_id:
                extracted = process_pdf_uaz(message_id)
                if extracted: pdf_text = f"\n[Conteúdo PDF]: {extracted[:1200]}..."
            mensagem_texto = f"Comprovante/PDF Recebido. {pdf_text} [MEDIA_URL: {pdf_url}]" if pdf_url else f"[PDF sem link] {pdf_text}"

    return {"telefone": telefone, "mensagem_texto": mensagem_texto, "message_type": message_type, "message_id": message_id, "from_me": from_me}

def send_whatsapp_message(telefone: str, mensagem: str) -> bool:
    base = get_api_base_url()
    if not base: return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/send/text"
    except:
        url = f"{base.split('/message')[0]}/send/text"
    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    try:
        requests.post(url, headers=headers, json={"number": re.sub(r"\D", "", telefone or ""), "text": mensagem, "openTicket": "1"}, timeout=10)
        return True
    except Exception as e:
        logger.error(f"Erro envio: {e}")
        return False

# --- Presença & Buffer ---
presence_sessions = {}
buffer_sessions = {}

def send_presence(num, type_):
    base = get_api_base_url()
    if not base: return
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/message/presence"
    except:
        url = f"{base}/message/presence"
    try:
        requests.post(url, headers={"Content-Type": "application/json", "token": settings.whatsapp_token}, 
                     json={"number": re.sub(r"\D","",num), "presence": type_}, timeout=5)
    except: pass

def process_async(tel, msg, mid=None):
    """Processa: Espera Leitura -> Digita -> IA -> Para -> Envia"""
    try:
        num = re.sub(r"\D", "", tel)
        
        # 1. Delay Humano (Leitura)
        # Mantive entre 2 e 4s conforme o código original que você gostou
        tempo_leitura = random.uniform(2.0, 4.0) 
        time.sleep(tempo_leitura)

        # 2. "Digitando..."
        send_presence(num, "composing")
        
        # 3. Processa
        res = run_agent(tel, msg)
        txt = res.get("output", "Erro ao processar.")
        
        # 4. Para presença
        send_presence(num, "paused")
        time.sleep(0.5)
        
        # 5. Envia
        send_whatsapp_message(tel, txt)

    except Exception as e:
        logger.error(f"Erro async: {e}")
    finally:
        send_presence(tel, "paused")
        presence_sessions.pop(re.sub(r"\D", "", tel), None)

def buffer_loop(tel):
    """
    Buffer Configurado conforme solicitado:
    - 3 Ciclos
    - 3.5 Segundos cada
    Total espera base: ~10.5 segundos
    """
    try:
        n = re.sub(r"\D","",tel)
        prev = get_buffer_length(n)
        stall = 0
        
        # [CONFIGURAÇÃO SOLICITADA]
        while stall < 3:  # 3 Ciclos
            time.sleep(3.5) # 3.5 Segundos cada
            curr = get_buffer_length(n)
            if curr > prev: prev, stall = curr, 0
            else: stall += 1
        
        msgs = pop_all_messages(n)
        final = " ".join([m for m in msgs if m.strip()])
        if final: process_async(n, final)
    except: pass
    finally: buffer_sessions.pop(re.sub(r"\D","",tel), None)

# --- Endpoints ---
@app.get("/")
async def root(): return {"status":"online", "ver":"1.5.4"}

@app.get("/health")
async def health(): return {"status":"healthy", "ts":datetime.now().isoformat()}

@app.post("/")
@app.post("/webhook/whatsapp")
async def webhook(req: Request, tasks: BackgroundTasks):
    try:
        pl = await req.json()
        data = _extract_incoming(pl)
        tel, txt, from_me = data["telefone"], data["mensagem_texto"], data["from_me"]

        if not tel or not txt: return JSONResponse(content={"status":"ignored"})
        logger.info(f"In: {tel} | {data['message_type']} | {txt[:50]}")

        if from_me:
            try: get_session_history(tel).add_ai_message(txt)
            except: pass
            return JSONResponse(content={"status":"ignored_self"})

        num = re.sub(r"\D","",tel)
        
        # NOTA: Removi o 'send_presence' imediato para evitar o comportamento "robótico".
        # O 'Digitando...' aparecerá apenas após o ciclo do buffer e tempo de leitura.

        active, _ = is_agent_in_cooldown(num)
        if active:
            push_message_to_buffer(num, txt)
            return JSONResponse(content={"status":"cooldown"})

        try:
            if not presence_sessions.get(num):
                presence_sessions[num] = True
        except: pass

        if push_message_to_buffer(num, txt):
            if not buffer_sessions.get(num):
                buffer_sessions[num] = True
                threading.Thread(target=buffer_loop, args=(num,), daemon=True).start()
        else:
            tasks.add_task(process_async, tel, txt)

        return JSONResponse(content={"status":"buffering"})
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return JSONResponse(status_code=500, detail=str(e))

@app.post("/message")
async def direct_msg(msg: WhatsAppMessage):
    try:
        res = run_agent(msg.telefone, msg.mensagem)
        return AgentResponse(success=True, response=res["output"], telefone=msg.telefone, timestamp="")
    except Exception as e:
        return AgentResponse(success=False, response="", telefone="", error=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=settings.server_host, port=settings.server_port, log_level=settings.log_level.lower())
