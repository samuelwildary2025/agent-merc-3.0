"""
Servidor FastAPI para receber mensagens do WhatsApp e processar com o agente
# touch: reload marker
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import requests
from datetime import datetime
import time
import threading
import re

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

# ============================================
# Inicialização do FastAPI
# ============================================

app = FastAPI(
    title="Agente de Supermercado",
    description="API para atendimento automatizado via WhatsApp",
    version="1.2.1"
)


# ============================================
# Modelos de Dados
# ============================================

class WhatsAppMessage(BaseModel):
    """Modelo de mensagem recebida do WhatsApp"""
    telefone: str = Field(..., description="Número de telefone do cliente")
    mensagem: str = Field(..., description="Texto da mensagem")
    message_id: Optional[str] = Field(None, description="ID da mensagem")
    timestamp: Optional[str] = Field(None, description="Timestamp da mensagem")
    message_type: Optional[str] = Field("text", description="Tipo de mensagem (text, audio, image)")


class AgentResponse(BaseModel):
    """Modelo de resposta do agente"""
    success: bool
    response: str
    telefone: str
    timestamp: str
    error: Optional[str] = None


class PresenceRequest(BaseModel):
    """Modelo para atualização de presença"""
    number: str = Field(..., description="Número no formato internacional, ex: 5511999999999")
    presence: str = Field(..., description="Tipo de presença: composing, recording, paused")
    delay: Optional[int] = Field(None, description="Duração em ms (máx 300000). Reenvio a cada 10s")


# ============================================
# Funções Auxiliares
# ============================================

def get_api_base_url() -> str:
    """
    Retorna a URL base da API, priorizando UAZ_API_URL.
    """
    # Prioridade: UAZ_API_URL > WHATSAPP_API_URL > String vazia
    url = (settings.uaz_api_url or settings.whatsapp_api_url or "").strip().rstrip("/")
    return url

def transcribe_audio_uaz(message_id: str) -> Optional[str]:
    """
    Solicita a transcrição de áudio para a API da UAZ.
    Usa o endpoint específico /message/download
    """
    if not message_id:
        return None

    base = get_api_base_url()
    if not base:
        logger.error("❌ Nenhuma URL de API configurada.")
        return None

    # Montar URL do endpoint de download
    # Remove qualquer path existente (como /send/text) para pegar a raiz
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        base_domain = f"{parsed.scheme}://{parsed.netloc}"
        url = f"{base_domain}/message/download"
    except Exception:
        # Fallback simples
        clean_base = base.split("/send")[0].split("/message")[0]
        url = f"{clean_base}/message/download"

    headers = {
        "Content-Type": "application/json",
        "token": (settings.whatsapp_token or "").strip()
    }
    
    # Payload conforme documentação UAZ
    payload = {
        "id": message_id,
        "transcribe": True,
        "return_base64": False,
        "return_link": False,
        "openai_apikey": settings.openai_api_key
    }
    
    logger.info(f"🎧 Solicitando transcrição na UAZ: {message_id}")
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=25)
        
        if response.status_code == 200:
            data = response.json()
            texto = data.get("transcription")
            if texto:
                logger.info(f"✅ Áudio transcrito: '{texto}'")
                return texto
            else:
                logger.warning(f"⚠️ API retornou 200 mas sem 'transcription': {data}")
        else:
            logger.error(f"❌ Falha na transcrição. Status: {response.status_code} Body: {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Erro ao conectar na API de transcrição: {e}")
        
    return None


def _extract_incoming(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza payloads de diferentes provedores para um formato comum.
    """
    def _sanitize_phone(raw: Any) -> Optional[str]:
        if raw is None: return None
        s = str(raw)
        if "@" in s: s = s.split("@")[0]
        if ":" in s: s = s.split(":")[-1]
        return re.sub(r"\D", "", s) or None

    chat = payload.get("chat") or {}
    message_any = payload.get("message") or {}
    
    # Tratamento para webhooks legados que enviam lista
    if isinstance(payload.get("messages"), list):
        try:
            m0 = payload["messages"][0]
            message_any = m0
            chat = {"wa_id": m0.get("sender") or m0.get("chatid")}
        except: pass

    mensagem_texto = payload.get("text")
    message_type = payload.get("messageType") or "text"
    message_id = payload.get("id") or payload.get("messageid")
    from_me = False

    if isinstance(message_any, dict):
        message_type = message_any.get("type") or message_type
        message_id = message_any.get("messageid") or message_any.get("id") or message_id
        from_me = bool(message_any.get("fromMe") or message_any.get("wasSentByApi") or False)
        
        content = message_any.get("content")
        if isinstance(content, str) and not mensagem_texto:
            mensagem_texto = content
        elif isinstance(content, dict):
            mensagem_texto = content.get("text") or mensagem_texto
        
        txt = message_any.get("text")
        if mensagem_texto is None:
            if isinstance(txt, dict):
                mensagem_texto = txt.get("body")
            else:
                mensagem_texto = txt or message_any.get("body")

    # Extração do telefone
    if from_me:
        telefone_candidates = [chat.get("wa_id"), chat.get("phone"), payload.get("sender")]
    else:
        telefone_candidates = [
            payload.get("from"), 
            chat.get("wa_id"), 
            message_any.get("sender") if isinstance(message_any, dict) else None
        ]

    telefone: Optional[str] = None
    for cand in telefone_candidates:
        cand_digits = _sanitize_phone(cand)
        if cand_digits:
            telefone = cand_digits
            break

    # Ajuste de tipos
    msg_type_lower = str(message_type).lower()
    if msg_type_lower in ("textmessage", "conversation", "extendedtextmessage"):
        message_type = "text"
    elif "audio" in msg_type_lower:
        message_type = "audio"
    elif "image" in msg_type_lower:
        message_type = "image"

    # Lógica de Áudio / Imagem
    if isinstance(message_any, dict):
        if message_type == "image" and not mensagem_texto:
            img = message_any.get("image")
            if isinstance(img, dict):
                mensagem_texto = img.get("caption") or "[Imagem recebida]"
            else:
                mensagem_texto = "[Imagem recebida]"
        
        # Se for áudio, tenta transcrever se não tiver texto
        elif message_type == "audio" and not mensagem_texto:
            if message_id:
                transcricao = transcribe_audio_uaz(message_id)
                if transcricao:
                    mensagem_texto = transcricao
                    # Prefixo para o agente identificar contexto
                    mensagem_texto = f"[Áudio]: {mensagem_texto}"
                else:
                    mensagem_texto = "[Mensagem de áudio recebida, mas não consegui ouvir]"
            else:
                mensagem_texto = "[Mensagem de áudio sem ID]"

    return {
        "telefone": telefone,
        "mensagem_texto": mensagem_texto,
        "message_type": message_type,
        "message_id": message_id,
        "from_me": from_me,
    }

def send_whatsapp_message(telefone: str, mensagem: str) -> bool:
    """
    Envia mensagem de resposta para o WhatsApp via API UAZ
    Corrige o endpoint para /send/text
    """
    base = get_api_base_url()
    if not base:
        logger.error("❌ Nenhuma URL de API configurada para envio.")
        return False

    # Montar URL correta para envio de texto
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        base_domain = f"{parsed.scheme}://{parsed.netloc}"
        # Endpoint corrigido baseado nos logs de sucesso
        url = f"{base_domain}/send/text"
    except Exception:
        # Fallback
        clean_base = base.split("/message")[0]
        url = f"{clean_base}/send/text"
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "token": (settings.whatsapp_token or "").strip(),
    }
    
    # Divisão de mensagens longas
    max_length = 4000
    mensagens = []
    
    if len(mensagem) > max_length:
        paragrafos = mensagem.split('\n\n')
        mensagem_atual = ""
        for paragrafo in paragrafos:
            if len(mensagem_atual) + len(paragrafo) + 2 <= max_length:
                mensagem_atual += paragrafo + "\n\n"
            else:
                if mensagem_atual: mensagens.append(mensagem_atual.strip())
                mensagem_atual = paragrafo + "\n\n"
        if mensagem_atual: mensagens.append(mensagem_atual.strip())
    else:
        mensagens = [mensagem]
    
    try:
        for i, msg in enumerate(mensagens):
            numero_sanitizado = re.sub(r"\D", "", telefone or "")
            
            # Payload correto para o endpoint /send/text
            payload = {"number": numero_sanitizado, "text": msg, "openTicket": "1"}
            
            logger.info(f"Enviando para API (POST): url={url}")
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            
            if response.status_code >= 400:
                logger.error(f"Erro envio: {response.status_code} - {response.text}")
                return False
            
            logger.info(f"Mensagem {i+1}/{len(mensagens)} enviada para {telefone}")
        return True
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem para WhatsApp: {e}")
        return False


# ============================================
# Presença (digitando/gravação/pausa)
# ============================================

presence_sessions: Dict[str, Dict[str, Any]] = {}
buffer_sessions: Dict[str, Dict[str, Any]] = {}

def _sanitize_number(num: Optional[str]) -> Optional[str]:
    if not num: return None
    s = str(num)
    if "@" in s: s = s.split("@")[0]
    if ":" in s: s = s.split(":")[-1]
    return re.sub(r"\D", "", s) or None

def send_presence_signal(number: str, presence: str) -> bool:
    base = get_api_base_url()
    if not base: return False

    # Montar URL para presença (/message/presence)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        base_domain = f"{parsed.scheme}://{parsed.netloc}"
        url = f"{base_domain}/message/presence"
    except Exception:
        url = f"{base}/message/presence"

    headers = {
        "Content-Type": "application/json",
        "token": (settings.whatsapp_token or "").strip(),
    }
    payload = {"number": _sanitize_number(number), "presence": presence}
    
    try:
        requests.post(url, headers=headers, json=payload, timeout=5)
        return True
    except Exception:
        return False

def cancel_presence(number: str):
    n = _sanitize_number(number) or number
    sess = presence_sessions.get(n)
    if not sess:
        presence_sessions[n] = {"cancel": True}
    else:
        sess["cancel"] = True
    send_presence_signal(n, "paused")

def presence_loop(number: str, presence: str, delay_ms: Optional[int] = None):
    n = _sanitize_number(number) or number
    max_ms = 300000
    duration_ms = min(delay_ms or max_ms, max_ms)

    if str(presence).lower() == "paused":
        presence_sessions.setdefault(n, {"cancel": False})
        presence_sessions[n]["cancel"] = True
        send_presence_signal(n, "paused")
        return

    existing = presence_sessions.get(n)
    if existing and existing.get("cancel"):
        presence_sessions.pop(n, None)
        return

    presence_sessions.setdefault(n, {"cancel": False})
    end_time = time.time() + (duration_ms / 1000.0)
    
    send_presence_signal(n, presence)
    while time.time() < end_time:
        if presence_sessions.get(n, {}).get("cancel"): break
        time.sleep(10)
        if presence_sessions.get(n, {}).get("cancel"): break
        send_presence_signal(n, presence)

    send_presence_signal(n, "paused")
    presence_sessions.pop(n, None)

def process_message_async(telefone: str, mensagem: str, message_id: Optional[str] = None):
    logger.info(f"Processando mensagem assíncrona de {telefone}")
    try:
        result = run_agent(telefone, mensagem)
        final_text = result.get("output") if isinstance(result, dict) else None
        
        if not isinstance(final_text, str) or not final_text.strip():
            final_text = "Desculpe, não consegui processar sua mensagem."

        success = send_whatsapp_message(telefone, final_text)
        if success:
            logger.info(f"✅ Resposta enviada com sucesso para {telefone}")
        else:
            logger.error(f"❌ Falha ao enviar resposta para {telefone}")

    except Exception as e:
        logger.error(f"Erro no processamento assíncrono: {e}", exc_info=True)
    finally:
        try:
            cancel_presence(telefone)
        except Exception:
            pass

def buffer_loop(telefone: str):
    try:
        numero = _sanitize_number(telefone) or telefone
        prev_len = get_buffer_length(numero)
        consecutive_no_new = 0
        while consecutive_no_new < 3:
            time.sleep(5)
            cur_len = get_buffer_length(numero)
            if cur_len > prev_len:
                prev_len = cur_len
                consecutive_no_new = 0
            else:
                consecutive_no_new += 1

        msgs = pop_all_messages(numero)
        combined = " ".join([m for m in msgs if isinstance(m, str) and m.strip()])
        if combined:
            process_message_async(numero, combined)
    except Exception as e:
        logger.error(f"Erro no buffer_loop: {e}", exc_info=True)
    finally:
        try:
            n = _sanitize_number(telefone) or telefone
            buffer_sessions.pop(n, None)
        except Exception:
            pass


# ============================================
# Endpoints
# ============================================

@app.get("/")
async def root():
    return {"status": "online", "service": "Agente de Supermercado", "version": "1.2.1"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/")
async def root_post(request: Request, background_tasks: BackgroundTasks):
    return await webhook_whatsapp(request, background_tasks)

@app.post("/webhook/whatsapp")
async def webhook_whatsapp(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
        logger.info(f"Webhook recebido: {payload}")

        normalized = _extract_incoming(payload)
        
        telefone = normalized.get("telefone")
        mensagem_texto = normalized.get("mensagem_texto")
        message_type = normalized.get("message_type")
        message_id = normalized.get("message_id")
        from_me = bool(normalized.get("from_me") or False)

        if not telefone:
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "no_phone"})
        
        if not mensagem_texto:
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "no_text_or_audio"})

        logger.info(f"Normalizado: telefone={telefone} type={message_type} texto={mensagem_texto[:50]}")

        if from_me:
            try:
                hist = get_session_history(telefone)
                hist.add_ai_message(mensagem_texto or "")
            except: pass
            return JSONResponse(status_code=200, content={"status": "ignored", "reason": "from_me"})

        numero = _sanitize_number(telefone) or telefone
        active, ttl = is_agent_in_cooldown(numero)
        if active:
            logger.info(f"Cooldown ativo para {numero}. Empilhando.")
            push_message_to_buffer(numero, mensagem_texto)
            return JSONResponse(status_code=200, content={"status": "cooldown"})

        try:
            sess = presence_sessions.get(numero)
            if (not sess) or sess.get("cancel"):
                presence_sessions[numero] = {"cancel": False, "running": True}
                threading.Thread(target=presence_loop, args=(numero, "composing", 30000), daemon=True).start()
        except: pass

        try:
            ok_push = push_message_to_buffer(numero, mensagem_texto)
            if not ok_push:
                background_tasks.add_task(process_message_async, telefone, mensagem_texto, message_id)
            else:
                if not buffer_sessions.get(numero):
                    buffer_sessions[numero] = {"running": True}
                    threading.Thread(target=buffer_loop, args=(numero,), daemon=True).start()
        except Exception:
            background_tasks.add_task(process_message_async, telefone, mensagem_texto, message_id)

        return JSONResponse(status_code=200, content={"status": "buffering"})

    except Exception as e:
        logger.error(f"Erro webhook: {e}", exc_info=True)
        return JSONResponse(status_code=500, detail=str(e))

@app.post("/message")
async def send_message(message: WhatsAppMessage) -> AgentResponse:
    logger.info(f"Mensagem direta recebida de {message.telefone}")
    try:
        result = run_agent(message.telefone, message.mensagem)
        return AgentResponse(
            success=result["error"] is None,
            response=result["output"],
            telefone=message.telefone,
            timestamp=datetime.now().isoformat(),
            error=result["error"]
        )
    except Exception as e:
        return AgentResponse(success=False, response="Erro", telefone=message.telefone, timestamp="", error=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=settings.server_host, port=settings.server_port, reload=settings.debug_mode, log_level=settings.log_level.lower())
