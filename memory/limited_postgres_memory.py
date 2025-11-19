from typing import List, Optional
from langchain_community.chat_message_histories import PostgresChatMessageHistory
from langchain_core.messages import BaseMessage
from langchain_core.chat_history import BaseChatMessageHistory
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    # Fallback para psycopg 3.x
    import psycopg as psycopg2
    from psycopg import sql
from config.settings import settings


class LimitedPostgresChatMessageHistory(BaseChatMessageHistory):
    """PostgreSQL chat message history that stores all messages but limits agent context to recent messages."""
    
    def __init__(
        self,
        session_id: str,
        connection_string: str,
        table_name: str = "message_store",
        max_messages: int = 20,
        **kwargs
    ):
        """
        Initialize limited PostgreSQL chat history.
        
        Args:
            session_id: Unique identifier for the chat session
            connection_string: PostgreSQL connection string
            table_name: Name of the table to store messages
            max_messages: Maximum number of recent messages to return to the agent (default: 20)
        """
        self.session_id = session_id
        self.connection_string = connection_string
        self.table_name = table_name
        self.max_messages = max_messages
        
        # Initialize the base PostgreSQL history (stores all messages)
        self._postgres_history = PostgresChatMessageHistory(
            session_id=session_id,
            connection_string=connection_string,
            table_name=table_name,
            **kwargs
        )
    
    @property
    def messages(self) -> List[BaseMessage]:
        """Get optimized messages for the agent context."""
        return self.get_optimized_context()
    
    def add_message(self, message: BaseMessage) -> None:
        """Add a message to the database (all messages are stored)."""
        self._postgres_history.add_message(message)
        # No limit enforcement - all messages are stored for reporting
    
    def clear(self) -> None:
        """Clear all messages for this session."""
        self._postgres_history.clear()
    
    def _enforce_message_limit(self) -> None:
        """Keep only the most recent max_messages messages."""
        try:
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor() as cursor:
                    # Get message IDs ordered by ID (oldest first)
                    cursor.execute(f"""
                        SELECT id FROM {self.table_name}
                        WHERE session_id = %s
                        ORDER BY id ASC
                    """, (self.session_id,))
                    
                    message_ids = cursor.fetchall()
                    
                    # If we have more messages than the limit, delete the oldest ones
                    if len(message_ids) > self.max_messages:
                        messages_to_delete = len(message_ids) - self.max_messages
                        ids_to_delete = [msg[0] for msg in message_ids[:messages_to_delete]]
                        
                        cursor.execute(f"""
                            DELETE FROM {self.table_name}
                            WHERE id = ANY(%s)
                        """, (ids_to_delete,))
                        
                        conn.commit()
                        
                        print(f"Limited messages for session {self.session_id}: "
                              f"deleted {messages_to_delete} oldest messages, "
                              f"keeping {self.max_messages} most recent")
                              
        except Exception as e:
            print(f"Error enforcing message limit: {e}")
    
    def get_message_count(self) -> int:
        """Get the current number of messages for this session."""
        try:
            with psycopg2.connect(self.connection_string) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        SELECT COUNT(*) FROM {self.table_name}
                        WHERE session_id = %s
                    """, (self.session_id,))
                    
                    return cursor.fetchone()[0]
        except Exception as e:
            print(f"Error getting message count: {e}")
            return 0
    
    def get_session_info(self) -> dict:
        """Get information about the current session."""
        return {
            "session_id": self.session_id,
            "message_count": self.get_message_count(),
            "max_messages": self.max_messages,
            "table_name": self.table_name
        }
    
    def should_clear_context(self, recent_messages: List[BaseMessage]) -> bool:
        """
        Determine if context should be cleared based on recent messages.
        Returns True if agent is struggling to identify products.
        """
        if len(recent_messages) < 3:
            return False
            
        # Check if last few messages show agent confusion
        confusion_patterns = [
            "não identifiquei",
            "não consegui identificar",
            "informar o nome principal",
            "desculpe, não",
            "pode informar"
        ]
        
        recent_text = " ".join([msg.content.lower() for msg in recent_messages[-3:]])
        
        confusion_count = sum(1 for pattern in confusion_patterns if pattern in recent_text)
        
        # If 2+ confusion patterns in last 3 messages, suggest clearing
        return confusion_count >= 2
    
    def get_optimized_context(self) -> List[BaseMessage]:
        """
        Get optimized context for product identification.
        Focuses on recent product-related messages.
        """
        all_messages = self._postgres_history.messages
        
        if len(all_messages) <= self.max_messages:
            return all_messages
        
        # Get recent messages
        recent_messages = all_messages[-self.max_messages:]
        
        # Check if we should clear context due to confusion
        if self.should_clear_context(recent_messages):
            print(f"🔄 Detectada confusão do agente. Recomendação: limpar contexto para {self.session_id}")
            # Return only the very last messages to reset context
            return recent_messages[-3:]  # Only last 3 messages
        
        return recent_messages