"""
Configurações do Agente de Supermercado
Carrega variáveis de ambiente usando Pydantic Settings
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Configurações da aplicação carregadas do .env"""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # OpenAI
    openai_api_key: str
    llm_model: str = "gpt-5-mini"
    llm_temperature: float = 0.0
    llm_provider: str = "openai"
    moonshot_api_key: Optional[str] = None
    moonshot_api_url: str = "https://api.moonshot.ai/anthropic"
    
    # Supabase
    # Removido: campos de Supabase (não utilizados)
    
    # Cohere (opcional, não utilizado atualmente)
    cohere_api_key: str | None = None
    
    # Postgres
    postgres_connection_string: str
    postgres_table_name: str = "memoria"  # Nome da tabela para histórico de mensagens (padrão: memoria)
    postgres_message_limit: int = 12  # Número de mensagens recentes usadas pelo agente (0 = ilimitado)
    
    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_db: int = 0
    
    # API do Supermercado
    supermercado_base_url: str
    supermercado_auth_token: str

    # Consulta de EAN (estoque/preço) via endpoint externo
    estoque_ean_base_url: str = "http://45.178.95.233:5001/api/Produto/GetProdutosEAN"

    # EAN Smart Responder (Supabase Functions)
    smart_responder_url: str = ""
    # Backwards compatibility: existing single token
    smart_responder_token: str = ""
    # Preferred: separate auth and apikey, aligning with n8n setup
    smart_responder_auth: str = ""
    smart_responder_apikey: str = ""
    # Pré-resolvedor: desativado por padrão (fluxo removido)
    pre_resolver_enabled: bool = False
    
    # WhatsApp API
    whatsapp_api_url: str
    whatsapp_token: str
    whatsapp_method: str = "POST"
    # Número do WhatsApp do próprio agente (para filtrar mensagens auto-enviadas)
    whatsapp_agent_number: str | None = None
    
    # Servidor
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    debug_mode: bool = False

    # Logging
    log_level: str = "INFO"
    log_file: str = "logs/agente.log"

    # Prompt do agente (caminho opcional para arquivo externo)
    agent_prompt_path: str | None = None
    
    # Config V1 legacy removida; usando model_config (Pydantic v2)


# Instância global de configurações
settings = Settings()
