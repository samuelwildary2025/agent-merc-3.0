"""
Ferramentas HTTP para interação com a API do Supermercado (Versão SaaS Universal)
"""
import requests
import json
from typing import Dict, Any, List, Optional
from config.settings import settings
from config.logger import setup_logger

logger = setup_logger(__name__)


def get_auth_headers() -> Dict[str, str]:
    """Retorna os headers de autenticação para as requisições"""
    return {
        "Authorization": settings.supermercado_auth_token,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def _processar_produto_para_agente(produto_bruto: Dict[str, Any]) -> Dict[str, Any]:
    """
    MIDDLEWARE UNIVERSAL (SaaS):
    Traduz o JSON técnico do ERP para instruções de venda que o Agente entende.
    Classifica o produto entre PESÁVEL, PACOTE FECHADO ou UNITÁRIO.
    """
    # 1. Extração e Limpeza de Dados
    nome_sujo = produto_bruto.get("produto") or produto_bruto.get("nome") or produto_bruto.get("descricao") or "Produto sem nome"
    
    # Remove termos técnicos que poluem a fala da IA (opcional, mas recomendado)
    termos_tecnicos = ["CBOX", "RESF", "CONG", "VACUO", "VÁCUO", "C/OSSO", "S/OSSO", "RESFRIADO", "CONGELADO"]
    nome_limpo = nome_sujo
    for termo in termos_tecnicos:
        nome_limpo = nome_limpo.replace(termo, "").strip()

    # 2. Lógica de Melhor Preço (Promoção vs Normal)
    preco_normal = float(produto_bruto.get("vl_produto") or 0)
    preco_promo = float(produto_bruto.get("preco_fidelidade_promocao") or produto_bruto.get("vl_promocao") or 0)
    # Usa o promocional se for válido e menor que o normal
    preco_final = preco_promo if (preco_promo > 0 and preco_promo < preco_normal) else preco_normal

    # 3. Dados de Estoque
    qtd_estoque = (
        produto_bruto.get("qtd_produto") if "qtd_produto" in produto_bruto else 
        produto_bruto.get("estoque") if "estoque" in produto_bruto else 
        produto_bruto.get("quantidade") or 0
    )
    ativo = produto_bruto.get("ativo", True)
    disponivel = ativo and (float(qtd_estoque) > 0)

    # 4. CLASSIFICAÇÃO DE TIPO DE VENDA (A Lógica Universal)
    is_fracionado = bool(produto_bruto.get("fracionado", False))
    unidade_erp = str(produto_bruto.get("emb", "")).upper() # UN, KG, CX, PCT
    
    # Detecta se é PACOTE/CAIXA fechada (Industrializado)
    termos_pacote = ["PCT", "PACOTE", "EMB", "CX", "CAIXA", "FARDO", "FD"]
    eh_pacote = any(t in nome_sujo.split() for t in termos_pacote) or (unidade_erp in ["CX", "FD", "PCT"])

    # Detecta se é PESÁVEL / GRANEL (Açougue, Frios, Hortifrúti)
    # Regra: Unidade KG ou flag fracionado, e NÃO sendo um pacote fechado
    tem_kg_no_nome = "KG" in nome_sujo.upper()
    eh_pesavel = (is_fracionado or unidade_erp == "KG" or tem_kg_no_nome) and not eh_pacote

    # 5. GERAÇÃO DA INSTRUÇÃO PARA A IA
    tipo_venda = "UNITARIO"
    unidade_final = "UN"
    instrucao = "Venda normal por unidade. Preço fixo."

    if eh_pacote:
        tipo_venda = "EMBALAGEM_FECHADA"
        unidade_final = unidade_erp if unidade_erp not in ["", "None"] else "PCT"
        instrucao = (
            f"📦 EMBALAGEM FECHADA: Este é um pacote/caixa fechado. "
            f"Preço: R$ {preco_final:.2f} por {unidade_final}. "
            f"Se o cliente pediu 'uma unidade' (ex: 'uma salsicha'), PERGUNTE se ele quer o PACOTE FECHADO ou se prefere solto a granel."
        )

    elif eh_pesavel:
        tipo_venda = "PESAVEL"
        unidade_final = "KG"
        instrucao = (
            f"⚖️ PESO VARIÁVEL (Açougue/Frios/Hortifrúti): "
            f"O preço R$ {preco_final:.2f} é por QUILO (KG). "
            f"1. Se o cliente pedir por UNIDADE (ex: '2 calabresas', '3 maçãs'): "
            f"   - ACEITE o pedido. "
            f"   - AVISE que o valor é APROXIMADO e será confirmado na balança. "
            f"   - No campo 'observacao' do pedido, escreva a intenção original (ex: 'CLIENTE QUER 2 UNIDADES'). "
            f"2. Se pedir por VALOR (ex: '20 reais'): "
            f"   - Calcule o peso estimado e registre na observação."
        )

    # 6. Montagem do JSON Final
    return {
        "id": produto_bruto.get("id_produto") or produto_bruto.get("id"),
        "produto": nome_limpo,
        "preco": float(preco_final),
        "estoque_disponivel": disponivel,
        "tipo_venda": tipo_venda,           # UNITARIO, PESAVEL, EMBALAGEM_FECHADA
        "unidade": unidade_final,
        "INSTRUCAO_IA": instrucao,          # <--- A IA lerá isso para saber como agir
        
        # Metadados originais ocultos (para debug se precisar)
        "meta_original": nome_sujo,
        "meta_emb": unidade_erp
    }


def estoque(url: str) -> str:
    """
    Consulta o estoque (busca por nome) e aplica o middleware universal.
    """
    logger.info(f"Consultando estoque (SaaS): {url}")
    
    try:
        response = requests.get(
            url,
            headers=get_auth_headers(),
            timeout=10
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Normaliza para lista se vier objeto único
        if isinstance(data, dict):
            data = [data]
        elif not isinstance(data, list):
            data = []

        # Aplica o Middleware Universal em cada item
        itens_processados = [_processar_produto_para_agente(item) for item in data]
        
        logger.info(f"Estoque processado: {len(itens_processados)} produtos encontrados")
        return json.dumps(itens_processados, indent=2, ensure_ascii=False)
    
    except requests.exceptions.Timeout:
        msg = "Erro: Timeout ao consultar estoque. Tente novamente."
        logger.error(msg)
        return msg
    except Exception as e:
        logger.error(f"Erro em estoque: {e}")
        return f"Erro técnico ao consultar estoque: {str(e)}"


def estoque_preco(ean: str) -> str:
    """
    Consulta preço/estoque por EAN e aplica o middleware universal.
    """
    base = (settings.estoque_ean_base_url or "").strip().rstrip("/")
    if not base:
        return "Erro: URL de estoque EAN não configurada no .env"

    # Manter apenas dígitos do EAN
    ean_digits = "".join(filter(str.isdigit, ean))
    if not ean_digits:
        return "Erro: EAN inválido (informe apenas números)."

    url = f"{base}/{ean_digits}"
    logger.info(f"Consultando EAN (SaaS): {url}")

    try:
        response = requests.get(url, headers=get_auth_headers(), timeout=10)
        
        # Tratamento para APIs que retornam 404 quando produto não existe
        if response.status_code == 404:
            return "[]" 
            
        response.raise_for_status()
        
        data = response.json()
        
        # Normaliza para lista
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])

        # Aplica o Middleware Universal
        # Filtramos apenas itens disponíveis para limpar a visão da IA,
        # mas você pode remover o 'if' se quiser que a IA veja itens sem estoque.
        itens_processados = []
        for item in items:
            processado = _processar_produto_para_agente(item)
            if processado["estoque_disponivel"]: 
                itens_processados.append(processado)

        logger.info(f"EAN {ean_digits}: {len(itens_processados)} item(s) disponíveis")
        return json.dumps(itens_processados, indent=2, ensure_ascii=False)

    except requests.exceptions.Timeout:
        return "Erro: Demorou muito para consultar o código de barras."
    except Exception as e:
        logger.error(f"Erro ao consultar EAN: {e}")
        return f"Erro técnico ao buscar produto pelo código: {str(e)}"


def pedidos(json_body: str) -> str:
    """
    Envia um pedido finalizado para o painel.
    """
    url = f"{settings.supermercado_base_url}/pedidos/"
    logger.info(f"Enviando pedido: {url}")
    
    try:
        # Validar se é JSON válido antes de enviar
        data = json.loads(json_body)
        
        response = requests.post(
            url,
            headers=get_auth_headers(),
            json=data,
            timeout=10
        )
        response.raise_for_status()
        
        # Tenta extrair ID do pedido para confirmação
        resp_json = response.json()
        pedido_id = resp_json.get('id') or resp_json.get('numero_pedido') or 'N/A'
        
        return f"✅ Pedido enviado com sucesso! (ID: {pedido_id})"
        
    except json.JSONDecodeError:
        return "Erro: O formato do pedido está incorreto (JSON inválido)."
    except Exception as e:
        logger.error(f"Erro ao enviar pedido: {e}")
        return f"Erro ao enviar pedido para o sistema: {str(e)}"


def alterar(telefone: str, json_body: str) -> str:
    """
    Atualiza um pedido existente (ex: adicionar item esquecido).
    """
    telefone_limpo = "".join(filter(str.isdigit, telefone))
    url = f"{settings.supermercado_base_url}/pedidos/telefone/{telefone_limpo}"
    
    logger.info(f"Atualizando pedido telefone {telefone_limpo}")
    
    try:
        data = json.loads(json_body)
        response = requests.put(
            url,
            headers=get_auth_headers(),
            json=data,
            timeout=10
        )
        response.raise_for_status()
        return "✅ Pedido atualizado com sucesso!"
        
    except Exception as e:
        logger.error(f"Erro ao atualizar pedido: {e}")
        return f"Erro ao atualizar pedido: {str(e)}"


def ean_lookup(query: str) -> str:
    """
    Consulta Smart Responder (Base de Conhecimento / RAG via Supabase).
    Usa IA para identificar produtos por descrição ou imagem.
    """
    url = (settings.smart_responder_url or "").strip().replace("`", "")
    token = (settings.smart_responder_auth or settings.smart_responder_token or "").strip()
    apikey = (settings.smart_responder_apikey or "").strip()
    
    if not url or not token:
        return "Erro: Configuração de IA (Smart Responder) não encontrada no .env"

    # Normaliza token Bearer
    auth_header = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    if apikey:
        headers["apikey"] = apikey

    try:
        logger.info(f"Consultando IA RAG: {query[:50]}...")
        resp = requests.post(url, headers=headers, json={"query": query}, timeout=15)
        
        # Tenta formatar se for JSON, senão devolve texto bruto
        try:
            return json.dumps(resp.json(), indent=2, ensure_ascii=False)
        except:
            return resp.text
            
    except requests.exceptions.Timeout:
        return "Erro: A consulta à base de conhecimento demorou muito."
    except Exception as e:
        logger.error(f"Erro RAG: {e}")
        return "Não consegui consultar a base de conhecimento no momento."
