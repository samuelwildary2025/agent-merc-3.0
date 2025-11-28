from typing import List, Optional, Dict, Any
import json
import logging
from langchain_community.chat_message_histories import PostgresChatMessageHistory
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict, SystemMessage
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.language_models import BaseChatModel

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    # Fallback para psycopg 3.x
    import psycopg as psycopg2
    from psycopg import sql

# Configurar logger
logger = logging.getLogger(__name__)

class LimitedPostgresChatMessageHistory(BaseChatMessageHistory):
    """
    Histórico de chat PostgreSQL com suporte a Resumo Deslizante (Rolling Summary).
    """
    
    def __init__(
        self,
        session_id: str,
        connection_string: str,
        table_name: str = "memoria",
        max_messages: int = 20,
        **kwargs
    ):
        self.session_id = session_id
        self.connection_string = connection_string
        self.table_name = table_name
        self.max_messages = max_messages
        
        try:
            self._postgres_history = PostgresChatMessageHistory(
                session_id=session_id,
                connection_string=connection_string,
                table_name=table_name,
                **kwargs
            )
        except Exception as e:
            logger.warning(f"Erro ao iniciar PostgresChatMessageHistory padrão: {e}")
            self._postgres_history = None
    
    @property
    def messages(self) -> List[BaseMessage]:
        """Obtém mensagens (contexto otimizado)."""
        return self.get_optimized_context()
    
    def add_message(self, message: BaseMessage) -> None:
        """
        Adiciona uma mensagem ao banco de dados com SQL manual e COMMIT explícito.
        """
        conn = None
        try:
            msg_dict = message_to_dict(message)
            msg_json = json.dumps(msg_dict)
            
            conn = psycopg2.connect(self.connection_string)
            cursor = conn.cursor()
            
            query = f"""
                INSERT INTO {self.table_name} (session_id, message)
                VALUES (%s, %s)
            """
            
            cursor.execute(query, (self.session_id, msg_json))
            conn.commit()
            
            logger.info(f"📝 Mensagem persistida manualmente no DB para {self.session_id}")
            cursor.close()
            
        except Exception as e:
            logger.error(f"❌ Erro CRÍTICO ao salvar mensagem no Postgres: {e}")
            if conn:
                conn.rollback()
            if self._postgres_history:
                self._postgres_history.add_message(message)
        finally:
            if conn:
                conn.close()
    
    def clear(self) -> None:
        """Limpa todas as mensagens da sessão."""
        try:
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {self.table_name} WHERE session_id = %s", (self.session_id,))
                    conn.commit()
        except Exception as e:
            logger.error(f"Erro ao limpar histórico: {e}")
    
    def get_optimized_context(self) -> List[BaseMessage]:
        """
        Obtém contexto otimizado lendo diretamente do banco.
        """
        try:
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        SELECT message FROM {self.table_name} 
                        WHERE session_id = %s 
                        ORDER BY created_at ASC
                    """, (self.session_id,))
                    
                    rows = cursor.fetchall()
                    messages = []
                    for row in rows:
                        msg_data = row[0]
                        if isinstance(msg_data, str):
                            msg_data = json.loads(msg_data)
                        msgs = messages_from_dict([msg_data])
                        messages.extend(msgs)
                    
                    return messages # Retorna tudo (a otimização agora é feita pelo manage_rolling_summary)
                    
        except Exception as e:
            logger.error(f"Erro ao ler mensagens manualmente: {e}")
            return []

    def get_message_count(self) -> int:
        try:
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) FROM {self.table_name} WHERE session_id = %s", (self.session_id,))
                    result = cursor.fetchone()
                    return result[0] if result else 0
        except Exception:
            return 0

    def manage_rolling_summary(self, llm: BaseChatModel, group_size: int = 6):
        """
        Estratégia: Resumo Deslizante (Rolling Summary).
        A cada 'group_size' mensagens novas, incorpora as antigas ao resumo.
        MANTÉM sempre as últimas 'group_size' mensagens vivas (texto bruto).
        """
        messages = self.get_optimized_context()
        
        # Só ativa se tivermos mensagens suficientes (recente + margem para resumir)
        if len(messages) < (group_size + 3):
            return

        # Separa o que fica vivo (recente) do que será resumido (antigo)
        msgs_to_keep = messages[-group_size:]
        msgs_to_summarize = messages[:-group_size]

        # Verifica se já existe um resumo anterior para atualizar
        existing_summary = ""
        if isinstance(msgs_to_summarize[0], SystemMessage) and "RESUMO DO CONTEXTO:" in str(msgs_to_summarize[0].content):
            existing_summary = msgs_to_summarize[0].content
            # Remove o resumo antigo da lista para não duplicar
            msgs_to_summarize = msgs_to_summarize[1:]
        
        if not msgs_to_summarize:
            return

        # Gera o texto para o LLM processar
        conversation_text = "\n".join([f"{m.type}: {m.content}" for m in msgs_to_summarize])
        
        prompt = f"""
        Atualize o resumo da conversa com as novas informações.
        
        RESUMO ANTERIOR:
        {existing_summary}
        
        NOVAS MENSAGENS ANTIGAS PARA INCORPORAR:
        {conversation_text}
        
        Gere um novo resumo consolidado mantendo OBRIGATORIAMENTE:
        1. Nome do cliente, endereço e telefone (se houver).
        2. LISTA DE ITENS/PEDIDOS CONFIRMADOS (Produto, Qtd, Valor).
        3. Status do pagamento/entrega.
        
        IGNORE saudações e conversas irrelevantes.
        Seja técnico e direto.
        """
        
        try:
            # Chama o LLM para gerar o novo resumo
            new_summary_text = llm.invoke(prompt).content
            final_summary_msg = SystemMessage(content=f"RESUMO DO CONTEXTO: {new_summary_text}")
            
            # ATUALIZA O BANCO (Transação única)
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor() as cursor:
                    # 1. Limpa tudo dessa sessão
                    cursor.execute(f"DELETE FROM {self.table_name} WHERE session_id = %s", (self.session_id,))
                    
                    # 2. Insere o Novo Resumo
                    cursor.execute(
                        f"INSERT INTO {self.table_name} (session_id, message) VALUES (%s, %s)",
                        (self.session_id, json.dumps(message_to_dict(final_summary_msg)))
                    )
                    
                    # 3. Reinsere as mensagens recentes
                    for msg in msgs_to_keep:
                        cursor.execute(
                            f"INSERT INTO {self.table_name} (session_id, message) VALUES (%s, %s)",
                            (self.session_id, json.dumps(message_to_dict(msg)))
                        )
                    conn.commit()
            
            logger.info(f"🔄 Resumo atualizado! {len(msgs_to_summarize)} msgs antigas foram compactadas.")

        except Exception as e:
            logger.error(f"Erro ao atualizar resumo: {e}")
