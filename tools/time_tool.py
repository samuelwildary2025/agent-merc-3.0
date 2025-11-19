"""
Ferramenta para obter data e hora atual
"""
import datetime
import pytz
from config.logger import setup_logger

logger = setup_logger(__name__)


def get_current_time(timezone: str = "America/Sao_Paulo") -> str:
    """
    Retorna a data e hora atual no fuso horário especificado.
    
    Args:
        timezone: Fuso horário (padrão: America/Sao_Paulo)
    
    Returns:
        String formatada com data e hora
    """
    try:
        tz = pytz.timezone(timezone)
        now = datetime.datetime.now(tz)
        
        # Formato amigável
        formatted_time = now.strftime("%d/%m/%Y às %H:%M:%S (%Z)")
        
        # Informações adicionais
        day_of_week = now.strftime("%A")
        day_names = {
            "Monday": "Segunda-feira",
            "Tuesday": "Terça-feira",
            "Wednesday": "Quarta-feira",
            "Thursday": "Quinta-feira",
            "Friday": "Sexta-feira",
            "Saturday": "Sábado",
            "Sunday": "Domingo"
        }
        day_pt = day_names.get(day_of_week, day_of_week)
        
        result = f"📅 {day_pt}, {formatted_time}"
        
        logger.info(f"Hora atual consultada: {result}")
        return result
    
    except pytz.exceptions.UnknownTimeZoneError:
        error_msg = f"❌ Erro: Fuso horário '{timezone}' desconhecido."
        logger.error(error_msg)
        return error_msg
    
    except Exception as e:
        error_msg = f"❌ Erro ao obter hora atual: {str(e)}"
        logger.error(error_msg)
        return error_msg
