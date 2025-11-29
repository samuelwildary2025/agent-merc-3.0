"""
Servidor FastAPI para Agente de Supermercado
Versão: 1.6.0 (Com Pausas Naturais e Buffer)
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
import requests
from datetime import datetime
import time
import random
import threading
import re
import io
import logging  # <--- Import necessário para o filtro

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

# --- Filtro de Logs Customizado ---
class HealthCheckFilter(logging.Filter):
    """
    Filtra logs de health check para limpar o console.
    Retorna False se a mensagem contiver "GET /health", bloqueando o log.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("GET /health") == -1

app = FastAPI(title="Agente de Supermercado", version="1.6.0")

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
            return data.get("fileURL") or data.get("url")
    except Exception as e:
        logger.error(f"Erro link mídia: {e}")
    return None

def process_pdf_uaz(message_id: str) -> Optional[str]:
    if not PdfReader: return "[PDF não suportado]"
    url = get_media_url_uaz(message_id)
    if not url: return None
    try:
        response = requests.get(url, timeout=20)
        f = io.BytesIO(response.content)
        reader = PdfReader(f)
        text = "\n".join([p.extract_text() for p in reader.pages])
        return re.sub(r'\s+', ' ', text).strip()
    except Exception:
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
    def _clean_number(jid: Any) -> Optional[str]:
        if not jid or not isinstance(jid, str) or "@lid" in jid or "@g.us" in jid: return None
        if "@" in jid: jid = jid.split("@")[0]
        num = re.sub(r"\D", "", jid)
        return num if 10 <= len(num) <= 15 else None

    chat = payload.get("chat") or {}
    message_any = payload.get("message") or {}
    if isinstance(payload.get("messages"), list):
        try:
            message_any = payload["messages"][0]
            chat = {"wa_id": message_any.get("sender")}
        except: pass

    telefone = None
    candidates = [message_any.get("sender"), message_any.get("chatid"), chat.get("id"), chat.get("wa_id"), payload.get("from")]
    for c in candidates:
        t = _clean_number(c)
        if t: 
            telefone = t
            break
            
    if not telefone and payload.get("from") and "@lid" not in str(payload.get("from")):
        telefone = re.sub(r"\D", "", str(payload.get("from")))

    # Conteúdo
    mensagem_texto = payload.get("text")
    message_id = payload.get("id") or payload.get("messageid")
    raw_type = str(message_any.get("messageType") or "").lower()
    mimetype = str(message_any.get("mimetype") or "").lower()
    
    message_type = "text"
    if "audio" in raw_type or "ptt" in raw_type: message_type = "audio"
    elif "image" in raw_type: message_type = "image"
    elif "document" in raw_type or "pdf" in mimetype: message_type = "document"

    if isinstance(message_any, dict):
        message_id = message_any.get("messageid") or message_any.get("id") or message_id
        content = message_any.get("content")
        if isinstance(content, str) and not mensagem_texto: mensagem_texto = content
        elif isinstance(content, dict): mensagem_texto = content.get("text") or content.get("caption")
        if not mensagem_texto:
            txt = message_any.get("text")
            mensagem_texto = txt.get("body") if isinstance(txt, dict) else txt

    # Tratamento de Mídia
    if message_type == "audio" and not mensagem_texto:
        trans = transcribe_audio_uaz(message_id)
        mensagem_texto = f"[Áudio]: {trans}" if trans else "[Áudio inaudível]"
    elif message_type == "image":
        url = get_media_url_uaz(message_id)
        caption = mensagem_texto or ""
        mensagem_texto = f"{caption} [MEDIA_URL: {url}]" if url else f"{caption} [Imagem]"
    elif message_type == "document" and "pdf" in mimetype:
        url = get_media_url_uaz(message_id)
        text = process_pdf_uaz(message_id) or ""
        mensagem_texto = f"PDF Recebido. {text[:1000]} [MEDIA_URL: {url}]" if url else "[PDF]"

    return {
        "telefone": telefone,
        "mensagem_texto": mensagem_texto,
        "message_type": message_type,
        "from_me": bool(message_any.get("fromMe"))
    }

def send_whatsapp_message(telefone: str, mensagem: str) -> bool:
    """
    Envia mensagem com suporte a pausas naturais (picotado).
    Divide a mensagem onde houver quebra de linha dupla (\n\n).
    """
    base = get_api_base_url()
    if not base: return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/send/text"
    except:
        url = f"{base.split('/message')[0]}/send/text"
    
    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    
    # LÓGICA DE PICOTAR: Divide por \n\n e remove espaços vazios
    msgs = [m.strip() for m in mensagem.split('\n\n') if m.strip()]
    
    if not msgs: return True
    
    try:
        for i, msg_part in enumerate(msgs):
            payload = {"number": re.sub(r"\D", "", telefone or ""), "text": msg_part, "openTicket": "1"}
            requests.post(url, headers=headers, json=payload, timeout=10)
            
            # Se não for a última parte, espera um pouco (Humanização)
            # Tempo varia de 1.5s a 4s dependendo do tamanho do texto lido
            if i < len(msgs) - 1:
                tempo_leitura = min(1.5 + (len(msg_part) / 45), 4.0)
                time.sleep(tempo_leitura)
                
        return True
    except Exception as e:
        logger.error(f"Erro envio: {e}")
        return False

# --- Buffer & Process ---
presence_sessions = {}
buffer_sessions = {}

def send_presence(num, type_):
    try:
        base = get_api_base_url()
        url = f"{base}/message/presence"
        requests.post(url, headers={"token": settings.whatsapp_token}, 
                     json={"number": re.sub(r"\D","",num), "presence": type_}, timeout=3)
    except: pass

def process_async(tel, msg):
    try:
        num = re.sub(r"\D", "", tel)
        # 1. Delay leitura
        time.sleep(random.uniform(1.5, 3.0))
        
        # 2. Digitando...
        send_presence(num, "composing")
        
        # 3. Processa IA
        res = run_agent(tel, msg)
        txt = res.get("output", "Erro no sistema.")
        
        # 4. Pausa antes de enviar
        send_presence(num, "paused")
        time.sleep(0.5)
        
        # 5. Envia (agora com suporte a picotar)
        send_whatsapp_message(tel, txt)

    except Exception as e:
        logger.error(f"Erro async: {e}")
    finally:
        send_presence(re.sub(r"\D","",tel), "paused")
        presence_sessions.pop(re.sub(r"\D","",tel), None)

def buffer_loop(tel):
    try:
        n = re.sub(r"\D","",tel)
        prev = get_buffer_length(n)
        stall = 0
        while stall < 3:
            time.sleep(3.5)
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
async def root(): return {"status":"online", "ver":"1.6.0"}

@app.get("/health")
async def health(): return {"status":"healthy", "ts":datetime.now().isoformat()}

@app.post("/")
@app.post("/webhook/whatsapp")
async def webhook(req: Request, tasks: BackgroundTasks):
    try:
        pl = await req.json()
        data = _extract_incoming(pl)
        tel, txt = data["telefone"], data["mensagem_texto"]

        if not tel or not txt or data["from_me"]: 
            return JSONResponse(content={"status":"ignored"})
        
        num = re.sub(r"\D","",tel)
        active, _ = is_agent_in_cooldown(num)
        
        if active:
            push_message_to_buffer(num, txt)
            return JSONResponse(content={"status":"cooldown"})

        presence_sessions[num] = True
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

if __name__ == "__main__":
    import uvicorn
    
    # 1. Carrega configuração padrão de log do Uvicorn
    log_config = uvicorn.config.LOGGING_CONFIG
    
    # 2. Registra nosso filtro customizado
    # 'server.HealthCheckFilter' funciona porque o módulo principal ao rodar 'python server.py'
    # é mapeado como 'server' quando passamos "server:app" para o uvicorn.
    log_config["filters"]["health_filter"] = {
        "()": "server.HealthCheckFilter"
    }
    
    # 3. Aplica o filtro ao handler de 'access' (que mostra os GET/POST)
    if "filters" not in log_config["handlers"]["access"]:
        log_config["handlers"]["access"]["filters"] = []
    log_config["handlers"]["access"]["filters"].append("health_filter")
    
    # 4. Inicia o servidor com a nova configuração
    uvicorn.run(
        "server:app", 
        host=settings.server_host, 
        port=settings.server_port, 
        log_level=settings.log_level.lower(),
        log_config=log_config
    )
