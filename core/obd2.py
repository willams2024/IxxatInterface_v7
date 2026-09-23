"""
OBD-II (SAE J1979) — banco de PIDs e codec de request/resposta sobre CAN.

Diferente do J1939 (que é broadcast passivo), o OBD-II é um protocolo de
PERGUNTA/RESPOSTA sobre o transporte ISO-TP (ISO 15765-2/4):

  1) O testador ENVIA um request na ID 0x7DF (funcional/broadcast) ou
     0x7E0..0x7E7 (físico, endereçado a uma ECU específica).
  2) A ECU RESPONDE na ID 0x7E8..0x7EF.

Este módulo trata apenas o caso mais comum: PIDs do MODO 01 (dados atuais)
que cabem em UM único quadro CAN (ISO-TP "Single Frame"). PIDs multi-frame
(VIN, DTCs) exigiriam Flow Control e não são cobertos aqui.

Formato ISO-TP Single Frame (SF):
    byte 0 = 0x0L  (nibble alto 0 = SF, nibble baixo L = nº de bytes úteis)
    Request  modo 01:  [0x02, 0x01, PID, padding...]
    Response modo 01:  [len,  0x41, PID, A, B, C, D]   (0x41 = 0x01 + 0x40)

Fórmulas de conversão conforme SAE J1979 (A = 1º byte de dados, B = 2º, ...).
"""

from dataclasses import dataclass
from typing import Callable, Optional


# ── IDs padrão OBD-II sobre CAN 11-bit (ISO 15765-4) ────────────────────────
OBD_REQUEST_FUNCTIONAL = 0x7DF   # request funcional (TODAS as ECUs respondem)
OBD_REQUEST_PHYSICAL_BASE = 0x7E0  # request físico: 0x7E0+n endereça só a ECU n
OBD_RESP_MIN = 0x7E8             # faixa de respostas físicas: 0x7E8..0x7EF
OBD_RESP_MAX = 0x7EF

# Nomes usuais das ECUs por ID de resposta (ISO 15765-4). A ECU n responde em
# 0x7E8+n quando recebe um request em 0x7E0+n (ou o broadcast 0x7DF).
ECU_NAMES = {
    0x7E8: "ECU 1 (Motor/ECM)",
    0x7E9: "ECU 2 (Transmissão/TCM)",
    0x7EA: "ECU 3",
    0x7EB: "ECU 4",
    0x7EC: "ECU 5",
    0x7ED: "ECU 6",
    0x7EE: "ECU 7",
    0x7EF: "ECU 8",
}


def ecu_name(resp_id: int) -> str:
    """Nome legível da ECU a partir da ID de resposta (0x7E8..0x7EF)."""
    return ECU_NAMES.get(resp_id, f"0x{resp_id:03X}")


@dataclass
class PID:
    """
    Descreve um PID (Parameter ID) do OBD-II modo 01.

    Campos:
      pid      — número do PID (ex.: 0x0C = RPM).
      name     — nome legível do parâmetro (em português).
      n_bytes  — quantos bytes de dados a resposta traz (A, B, ...).
      unit     — unidade de engenharia (ex.: "rpm", "km/h", "°C").
      formula  — função que recebe a lista de bytes [A, B, ...] e devolve o
                 valor físico já convertido, conforme a norma SAE J1979.
      min_val  — valor mínimo típico (informativo).
      max_val  — valor máximo típico (informativo).
    """
    pid: int
    name: str
    n_bytes: int
    unit: str
    formula: Callable
    min_val: float = 0.0
    max_val: float = 0.0


# ── Banco de PIDs do MODO 01 (dados atuais) — SAE J1979 ─────────────────────
# A, B, C, D são os bytes de dados da resposta (payload após [len, 0x41, PID]).
PID_DATABASE: dict[int, PID] = {
    0x04: PID(0x04, "Carga do Motor Calculada",     1, "%",    lambda d: d[0] * 100 / 255, 0, 100),
    0x05: PID(0x05, "Temp. Líquido Arrefecimento",  1, "°C",   lambda d: d[0] - 40, -40, 215),
    0x06: PID(0x06, "Ajuste Comb. Curto Prazo B1",  1, "%",    lambda d: d[0] * 100 / 128 - 100, -100, 99),
    0x07: PID(0x07, "Ajuste Comb. Longo Prazo B1",  1, "%",    lambda d: d[0] * 100 / 128 - 100, -100, 99),
    0x0A: PID(0x0A, "Pressão de Combustível",       1, "kPa",  lambda d: d[0] * 3, 0, 765),
    0x0B: PID(0x0B, "Pressão Coletor Admissão",     1, "kPa",  lambda d: d[0], 0, 255),
    0x0C: PID(0x0C, "Rotação do Motor (RPM)",       2, "rpm",  lambda d: (d[0] * 256 + d[1]) / 4, 0, 16383),
    0x0D: PID(0x0D, "Velocidade do Veículo",        1, "km/h", lambda d: d[0], 0, 255),
    0x0E: PID(0x0E, "Avanço de Ignição",            1, "°",    lambda d: d[0] / 2 - 64, -64, 63),
    0x0F: PID(0x0F, "Temp. Ar de Admissão",         1, "°C",   lambda d: d[0] - 40, -40, 215),
    0x10: PID(0x10, "Fluxo de Ar (MAF)",            2, "g/s",  lambda d: (d[0] * 256 + d[1]) / 100, 0, 655),
    0x11: PID(0x11, "Posição da Borboleta",         1, "%",    lambda d: d[0] * 100 / 255, 0, 100),
    0x1F: PID(0x1F, "Tempo desde a Partida",        2, "s",    lambda d: d[0] * 256 + d[1], 0, 65535),
    0x21: PID(0x21, "Distância com MIL Ligada",     2, "km",   lambda d: d[0] * 256 + d[1], 0, 65535),
    0x2F: PID(0x2F, "Nível de Combustível",         1, "%",    lambda d: d[0] * 100 / 255, 0, 100),
    0x31: PID(0x31, "Distância desde Códigos Limpos", 2, "km", lambda d: d[0] * 256 + d[1], 0, 65535),
    0x33: PID(0x33, "Pressão Barométrica",          1, "kPa",  lambda d: d[0], 0, 255),
    0x42: PID(0x42, "Tensão do Módulo de Controle", 2, "V",    lambda d: (d[0] * 256 + d[1]) / 1000, 0, 65),
    0x45: PID(0x45, "Posição Relativa da Borboleta", 1, "%",   lambda d: d[0] * 100 / 255, 0, 100),
    0x46: PID(0x46, "Temp. Ambiente",               1, "°C",   lambda d: d[0] - 40, -40, 215),
    0x49: PID(0x49, "Pedal do Acelerador (pos. D)", 1, "%",    lambda d: d[0] * 100 / 255, 0, 100),
    0x5C: PID(0x5C, "Temp. Óleo do Motor",          1, "°C",   lambda d: d[0] - 40, -40, 215),
    0x5E: PID(0x5E, "Taxa de Consumo de Combustível", 2, "L/h", lambda d: (d[0] * 256 + d[1]) / 20, 0, 3277),
    # Odômetro (J1979-DA): 4 bytes, resolução 0,1 km. É o PID usado pela
    # biblioteca de referência do VIRLOC para hodômetro (PID 166 decimal).
    0xA6: PID(0xA6, "Odômetro",                     4, "km",
              lambda d: (d[0] * 16777216 + d[1] * 65536 + d[2] * 256 + d[3]) / 10,
              0, 429496729),
}


# ── Fórmulas em TEXTO (para documentação/exportação) ────────────────────────
# As lambdas do PID_DATABASE não podem ser lidas como texto, então mantemos
# aqui a mesma conta em notação da norma (A = 1º byte de dados, B = 2º).
# Se uma fórmula do banco mudar, atualize também a descrição correspondente.
FORMULA_TXT: dict[int, str] = {
    0x04: "A × 100 / 255",
    0x05: "A − 40",
    0x06: "A × 100 / 128 − 100",
    0x07: "A × 100 / 128 − 100",
    0x0A: "A × 3",
    0x0B: "A",
    0x0C: "(A × 256 + B) / 4",
    0x0D: "A",
    0x0E: "A / 2 − 64",
    0x0F: "A − 40",
    0x10: "(A × 256 + B) / 100",
    0x11: "A × 100 / 255",
    0x1F: "A × 256 + B",
    0x21: "A × 256 + B",
    0x2F: "A × 100 / 255",
    0x31: "A × 256 + B",
    0x33: "A",
    0x42: "(A × 256 + B) / 1000",
    0x45: "A × 100 / 255",
    0x46: "A − 40",
    0x49: "A × 100 / 255",
    0x5C: "A − 40",
    0x5E: "(A × 256 + B) / 20",
    0xA6: "(A × 2²⁴ + B × 2¹⁶ + C × 256 + D) / 10",
}


def formula_text(pid: int) -> str:
    """Fórmula de conversão do PID em texto legível (ou '—' se desconhecida)."""
    return FORMULA_TXT.get(pid, "—")


# ── Notas de referência do protocolo (usadas na documentação exportada) ─────
# Pares (tópico, explicação) que descrevem como o diálogo OBD-II acontece no
# barramento. Ficam aqui, junto do codec, para que a exportação da GUI seja
# apenas formatação — sem regra de protocolo duplicada na interface.
PROTOCOL_NOTES: list[tuple[str, str]] = [
    ("Norma",
     "SAE J1979 (PIDs de diagnóstico) transportado por ISO 15765-2/4 "
     "(ISO-TP) sobre CAN. Neste programa tratamos o MODO 01 — leitura de "
     "dados atuais."),
    ("Camada física",
     "CAN 11 bits (identificador padrão). Na maioria dos veículos leves a "
     "taxa é 500 kbps; alguns usam 250 kbps."),
    ("Request funcional",
     f"ID 0x{OBD_REQUEST_FUNCTIONAL:03X} — a pergunta chega a TODAS as ECUs "
     "compatíveis. Cada módulo que conhece o PID responde na sua própria ID "
     "física, então o mesmo PID pode voltar com valores diferentes."),
    ("Request físico",
     f"ID 0x{OBD_REQUEST_PHYSICAL_BASE:03X}+n (n = 0..7) — endereça um único "
     "módulo, eliminando a ambiguidade de várias respostas."),
    ("Respostas",
     f"IDs 0x{OBD_RESP_MIN:03X}..0x{OBD_RESP_MAX:03X}. A ECU endereçada por "
     f"0x{OBD_REQUEST_PHYSICAL_BASE:03X}+n responde em "
     f"0x{OBD_RESP_MIN:03X}+n."),
    ("Quadro de request",
     "[0x02, 0x01, PID, padding...] — 0x02 é o comprimento útil em bytes "
     "(modo + PID), 0x01 é o modo (dados atuais) e o resto do quadro é "
     "preenchimento (0x55)."),
    ("Quadro de resposta",
     "[len, 0x41, PID, A, B, C, D] — 0x41 = 0x01 (modo) + 0x40 (bit de "
     "resposta positiva); A, B, C, D são os bytes de dados usados pela "
     "fórmula do PID."),
    ("Single Frame (SF)",
     "O nibble alto do 1º byte identifica o tipo de quadro ISO-TP; 0 = Single "
     "Frame. Este programa só decodifica Single Frame — respostas multi-frame "
     "(VIN, lista de DTCs) exigiriam Flow Control e são ignoradas."),
    ("Resposta negativa",
     "Quando a ECU não suporta o PID ela responde 0x7F (serviço não "
     "suportado) ou simplesmente não responde. PIDs sem resposta aparecem na "
     "seção 'PIDs sem resposta' desta documentação."),
    ("Modo 09 — informações do veículo",
     "Serviço de leitura separado do modo 01, onde fica o CHASSI (VIN) no "
     "PID 0x02. Request 02 09 02; resposta 49 02 01 seguida de 17 bytes ASCII. "
     "Por passar de 7 bytes, a resposta vem multi-frame e exige Flow Control. "
     "O chassi também pode ser lido por UDS no DID 0xF190 — consultar os dois "
     "e comparar é a forma segura, porque nem todo veículo atende ambos."),
    ("Impacto no barramento",
     "Esta é a ÚNICA função do programa que transmite no barramento: um "
     "quadro de request por PID consultado. Nenhum dado é escrito nas ECUs "
     "(modo 01 é somente leitura) e nenhuma rotina de atuação/teste é usada."),
]


def build_request(pid: int, target_ecu: Optional[int] = None) -> tuple[int, bytes]:
    """
    Monta o quadro de REQUEST OBD-II modo 01 para um PID.

    Formato ISO-TP Single Frame: [0x02, 0x01, PID, padding...]. O 0x02 é o
    comprimento útil (2 bytes: modo + PID); o resto é preenchido com 0x55.

    Parâmetro target_ecu — ESCOLHA DO ENDEREÇAMENTO (importante!):
      None  → ID FUNCIONAL 0x7DF (broadcast). TODAS as ECUs compatíveis
              respondem, cada uma na sua ID física. Em veículos com vários
              módulos (caminhões), o MESMO PID pode voltar com valores
              diferentes de ECUs diferentes — por isso a resposta sempre
              carrega o campo 'src' (ver parse_response).
      0..7  → ID FÍSICA 0x7E0+n. Só a ECU n responde, eliminando a
              ambiguidade de múltiplas fontes.

    Retorna (can_id, data) prontos para CANBus.send().
    """
    if target_ecu is None:
        can_id = OBD_REQUEST_FUNCTIONAL
    else:
        can_id = OBD_REQUEST_PHYSICAL_BASE + (int(target_ecu) & 0x07)
    data = bytes([0x02, 0x01, pid & 0xFF, 0x55, 0x55, 0x55, 0x55, 0x55])
    return can_id, data


# ════════════════════════════════════════════════════════════════════════════
#  PIDs DE SUPORTE — o veículo declara o que implementa
# ════════════════════════════════════════════════════════════════════════════
#
# Em vez de perguntar PID por PID e esperar o timeout de cada um que não
# existe, a norma prevê que a ECU DECLARE quais PIDs implementa. Cada PID de
# suporte devolve 4 bytes = 32 bits, um por PID da faixa seguinte:
#
#   PID 0x00 -> bitmap dos PIDs 0x01..0x20
#   PID 0x20 -> bitmap dos PIDs 0x21..0x40
#   PID 0x40 -> bitmap dos PIDs 0x41..0x60   ... e assim por diante
#
# O bit mais significativo do primeiro byte corresponde ao PRIMEIRO PID da
# faixa. O último bit de cada bitmap indica se o próximo bloco existe — por
# isso a varredura pode parar assim que um bloco não for suportado.
#
# Ganho prático: 7 consultas respondem "o que este veículo entrega", contra
# uma varredura de dezenas de PIDs em que a maioria só produz timeout. E o
# resultado é DECLARADO pela ECU, não inferido de silêncio.

SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)


def decode_supported_pids(base_pid: int, data: bytes) -> set:
    """
    Decodifica o bitmap de PIDs suportados devolvido por um PID de suporte.

    `base_pid` é o PID consultado (0x00, 0x20, …) e `data` são os 4 bytes de
    dados da resposta. Devolve o conjunto de PIDs declarados como suportados
    na faixa base_pid+1 .. base_pid+32.
    """
    if len(data) < 4:
        return set()
    valor = int.from_bytes(bytes(data[:4]), "big")
    # Bit 31 (o mais significativo) = primeiro PID da faixa.
    return {base_pid + 1 + i for i in range(32) if valor & (1 << (31 - i))}


def encode_supported_pids(base_pid: int, suportados: set) -> list:
    """
    Monta o bitmap de 4 bytes a partir de um conjunto de PIDs.

    Inverso de decode_supported_pids(); usado pela simulação para responder de
    forma coerente com os PIDs que ela de fato implementa.
    """
    valor = 0
    for i in range(32):
        if (base_pid + 1 + i) in suportados:
            valor |= 1 << (31 - i)
    return [(valor >> 24) & 0xFF, (valor >> 16) & 0xFF,
            (valor >> 8) & 0xFF, valor & 0xFF]


def parse_supported_response(payload: bytes):
    """
    Interpreta um payload de resposta do modo 01 que seja bitmap de suporte.

    Devolve (base_pid, conjunto_de_pids) quando o payload é `41 <PID de
    suporte> <4 bytes>`, ou None quando não é esse caso. Existe separado de
    decode_mode01_payload() porque os PIDs de suporte não têm fórmula nem
    unidade — não são grandezas, são metadados.
    """
    if len(payload) < 6:
        return None
    if payload[0] != 0x41 or payload[1] not in SUPPORT_PIDS:
        return None
    return payload[1], decode_supported_pids(payload[1], payload[2:6])


# ════════════════════════════════════════════════════════════════════════════
#  MODO 09 — INFORMAÇÕES DO VEÍCULO (chassi, Cal ID, nome da ECU)
# ════════════════════════════════════════════════════════════════════════════
#
# O modo 09 é o outro serviço de LEITURA do OBD-II legislado. É onde vive o
# CHASSI (VIN), que o modo 01 não cobre.
#
# Diferença prática em relação ao modo 01: a resposta é TEXTO e quase sempre
# MULTI-FRAME — o VIN tem 17 caracteres e o payload total fica em 20 bytes,
# bem acima dos 7 que cabem num quadro. Sem remontagem ISO-TP (IsoTpReader em
# core/uds.py) o chassi nunca aparece.
#
#   Request:   02 09 02 55 55 55 55 55
#   Resposta:  49 02 01 <17 bytes ASCII>
#              │  │  └── NODI: quantidade de itens de dado (1)
#              │  └── PID do modo 09
#              └── 0x49 = 0x09 + 0x40
#
# Por que ler o VIN dos dois jeitos (modo 09 e DID 0xF190 do UDS): nem todo
# veículo responde os dois. Caminhão costuma atender o UDS; veículo leve
# legislado atende o modo 09. Consultar os dois e comparar é o caminho
# seguro — inclusive porque, quando ambos respondem, o valor tem que bater.

MODE_09 = 0x09
RESP_MODE_09 = 0x49          # 0x09 + 0x40

MODE9_PIDS: dict[int, tuple[str, str]] = {
    # PID: (nome, tipo)
    0x02: ("Chassi (VIN)", "ascii"),
}


def build_mode09_request(pid: int,
                         target_ecu: Optional[int] = None) -> tuple[int, bytes]:
    """
    Monta o request do MODO 09 (informações do veículo).

    Mesma forma do modo 01, mudando só o byte de serviço:
    [0x02, 0x09, PID, preenchimento].
    """
    if target_ecu is None:
        can_id = OBD_REQUEST_FUNCTIONAL
    else:
        can_id = OBD_REQUEST_PHYSICAL_BASE + (int(target_ecu) & 0x07)
    data = bytes([0x02, MODE_09, pid & 0xFF, 0x55, 0x55, 0x55, 0x55, 0x55])
    return can_id, data


def decode_mode09_payload(payload: bytes) -> Optional[dict]:
    """
    Decodifica o payload ISO-TP já remontado de uma resposta do modo 09:
    [0x49, PID, NODI, dados...].

    Retorna {pid, value, name, data} — com `value` em TEXTO — ou None.

    O byte NODI (number of data items) vem logo após o PID e vale 1 para o
    VIN. Ele é descartado: o que interessa são os bytes ASCII seguintes.
    """
    if len(payload) < 3:
        return None
    if payload[0] != RESP_MODE_09:
        return None
    pid = payload[1]
    info = MODE9_PIDS.get(pid)
    if info is None:
        return None
    nome, tipo = info
    dados = bytes(payload[3:])        # pula 0x49, PID e NODI
    if not dados:
        return None
    if tipo == "ascii":
        from core.uds import decode_ascii
        valor = decode_ascii(dados)
    else:
        valor = dados.hex().upper()
    return {"pid": pid, "value": valor, "name": nome, "data": dados}


def decode_mode01_payload(payload: bytes) -> Optional[dict]:
    """
    Decodifica o PAYLOAD (já sem o byte de PCI do ISO-TP) de uma resposta
    do modo 01: [0x41, PID, A, B, ...].

    Separado de parse_response() porque a resposta pode chegar de dois jeitos:
    num único quadro CAN (o caso normal, tratado por parse_response) ou
    remontada pelo ISO-TP a partir de vários quadros — e as duas rotas
    precisam decodificar exatamente igual.

    Retorna {pid, value, unit, name, data} ou None.
    """
    if len(payload) < 2:
        return None
    if payload[0] != 0x41:        # 0x41 = 0x01 (modo) + 0x40 (flag de resposta)
        return None
    pid = payload[1]
    info = PID_DATABASE.get(pid)
    if info is None:
        return None
    dados = list(payload[2:2 + info.n_bytes])
    if len(dados) < info.n_bytes:
        return None
    try:
        value = info.formula(dados)
    except Exception:
        return None
    return {"pid": pid, "value": value, "unit": info.unit,
            "name": info.name, "data": bytes(dados)}


def parse_response(can_id: int, data: bytes) -> Optional[dict]:
    """
    Interpreta um quadro de RESPOSTA OBD-II modo 01.

    Retorna um dicionário {src, pid, value, unit, name, data} quando o quadro é
    uma resposta válida de um PID conhecido, ou None caso contrário.

    O campo 'src' é a ID da ECU que respondeu (0x7E8..0x7EF) e é ESSENCIAL:
    com request funcional (0x7DF) várias ECUs respondem ao mesmo PID, cada uma
    com o SEU valor. Sem saber a origem, respostas de módulos diferentes se
    confundiriam como se fossem a mesma leitura.

    Só trata Single Frame (SF): o nibble alto do byte 0 deve ser 0. Respostas
    multi-frame são remontadas pelo IsoTpReader (core/uds.py) e decodificadas
    por decode_mode01_payload().
    """
    # A resposta tem que vir na faixa física 0x7E8..0x7EF.
    if not (OBD_RESP_MIN <= can_id <= OBD_RESP_MAX):
        return None
    if len(data) < 3:
        return None

    # ISO-TP: nibble alto do 1º byte identifica o tipo de frame (0 = Single).
    if (data[0] >> 4) != 0:
        return None

    # Tudo após o PCI é o payload. Não usamos o comprimento declarado aqui:
    # algumas ECUs preenchem esse nibble de forma descuidada, e ser tolerante
    # custa nada (a fórmula do PID já sabe quantos bytes consumir).
    res = decode_mode01_payload(bytes(data[1:]))
    if res is None:
        return None
    # 'src' preserva QUAL ECU respondeu — sem isso, respostas de módulos
    # diferentes para o mesmo PID seriam indistinguíveis.
    res["src"] = can_id
    return res


# ════════════════════════════════════════════════════════════════════════════
#  BIBLIOTECA CAN (VIRLOC) — geração das linhas de configuração do equipamento
# ════════════════════════════════════════════════════════════════════════════
#
# O equipamento de telemetria (VIRLOC) é configurado por linhas de texto no
# formato >COMANDO,parâmetros<. Para OBD-II a biblioteca tem quatro blocos:
#
#   1) >VS19_ENAbbbb,1<   habilita a CAN na taxa bbbb (em kbps) em modo normal
#                         (modo normal é obrigatório: o OBD-II precisa
#                         transmitir os requests).
#   2) >VOBD_ENA1,nnn<    habilita a consulta automática de PIDs.
#      >VOBDppp,2<        um por PID consultado (ppp = PID em DECIMAL).
#   3) >VSRMxx,...< / >VSRTnn,MODELO.0<   registros de identificação.
#   4) >VS19ff,...<       um filtro por sinal, dizendo de onde extrair o valor
#      >VS19ff_MAT,ops<   as operações inteiras que convertem o valor bruto.
#
# O layout do filtro VS na variante OBD-II é
#     >VS19ff, iiiii, ppp, 11, cc, 4, n, mmmmmmmm, 0, 3<
# e cada campo está descrito em VS_FIELD_DOC (logo abaixo). Essa descrição é
# DADO, e não comentário, porque também é impressa na documentação exportada.

VS_FIELD_DOC: list[tuple[str, str]] = [
    ("VS19ff",   "número do filtro: 00..24 → VS1900..VS1924"),
    ("iiiii",    "ID CAN da RESPOSTA, em decimal com 5 dígitos "
                 "(0x7E8 = 02024)"),
    ("ppp",      "PID consultado, em DECIMAL (0x0C = 12)"),
    ("11",       "tamanho do identificador: o OBD-II usa ID padrão de 11 bits"),
    ("cc",       "CT — canal de destino do valor no equipamento (01..96)"),
    ("4",        "byte inicial, 1-indexado: na resposta "
                 "[len, 0x41, PID, A, B, C, D] o byte A é sempre o 4º"),
    ("n",        "quantos bytes ler (tamanho do dado do PID)"),
    ("mmmmmmmm", "máscara de bits: n bytes de FF "
                 "(1 byte = 000000FF, 2 bytes = 0000FFFF)"),
    ("0",        "modo: 0 = copia o valor (1 acumularia a cada leitura)"),
    ("3",        "ordem de bytes do OBD-II — dado big-endian, byte A primeiro"),
]

# Canal (CT) de destino usual de cada PID na biblioteca de referência. Só os
# quatro abaixo são conhecidos; qualquer outro PID recebe um CT sequencial a
# partir de CT_FALLBACK_BASE, que deve ser confirmado no equipamento.
VIRLOC_CT: dict[int, int] = {
    0x0C: 11,   # rotação do motor (RPM)
    0x0D: 10,   # velocidade do veículo
    0xA6: 13,   # hodômetro
    0x45: 4,    # % de pedal do acelerador
}
CT_FALLBACK_BASE = 20      # 1º CT sugerido para PIDs sem canal documentado
CT_MAX = 96                # maior CT aceito pelo equipamento
LIB_MAX_FILTERS = 25       # VS1900..VS1924 — limite de filtros do equipamento

# Operações MAT (matemática inteira do equipamento) equivalentes à fórmula de
# cada PID. O equipamento não tem ponto flutuante: só sabe multiplicar, dividir
# e somar inteiros, aplicados da ESQUERDA para a DIREITA, sem precedência.
# Lista vazia = o valor bruto já é o valor final (nenhuma linha MAT é gerada).
MAT_OPS: dict[int, list[str]] = {
    0x04: ["x100/255"],
    0x05: ["-40"],
    0x06: ["x100/128", "-100"],
    0x07: ["x100/128", "-100"],
    0x0A: ["x3"],
    0x0B: [],
    0x0C: ["/4"],
    0x0D: [],
    0x0E: ["/2", "-64"],
    0x0F: ["-40"],
    0x10: ["/100"],
    0x11: ["x100/255"],
    0x1F: [],
    0x21: [],
    0x2F: ["x100/255"],
    0x31: [],
    0x33: [],
    0x42: ["/1000"],
    0x45: ["x100/255"],
    0x46: ["-40"],
    0x49: ["x100/255"],
    0x5C: ["-40"],
    0x5E: ["/20"],
    0xA6: ["/10"],
}


def _assign_cts(pids: list[int]) -> dict[int, tuple[int, bool]]:
    """
    Escolhe o CT (canal de destino) de cada PID da biblioteca.

    Primeiro reserva os canais documentados em VIRLOC_CT; depois distribui
    canais livres a partir de CT_FALLBACK_BASE para os PIDs restantes. O
    segundo item da tupla diz se o canal é o documentado (True) ou apenas uma
    sugestão a conferir (False).
    """
    resultado: dict[int, tuple[int, bool]] = {}
    usados: set[int] = set()

    # 1ª passada: canais oficiais, na ordem dos PIDs.
    for pid in pids:
        ct = VIRLOC_CT.get(pid)
        if ct is not None and ct not in usados:
            resultado[pid] = (ct, True)
            usados.add(ct)

    # 2ª passada: os demais recebem o primeiro canal livre acima da base.
    proximo = CT_FALLBACK_BASE
    for pid in pids:
        if pid in resultado:
            continue
        while proximo in usados and proximo < CT_MAX:
            proximo += 1
        resultado[pid] = (proximo, False)
        usados.add(proximo)
        proximo += 1
    return resultado


def build_can_library(entries: list[tuple[int, int, bool]],
                      baudrate: int = 500000,
                      model: str = "VEICULO") -> list[tuple[str, str]]:
    """
    Monta as linhas da biblioteca CAN (VIRLOC) para leitura de PIDs OBD-II.

    Parâmetros:
      entries  — lista de (pid, id_de_resposta, confirmado). 'confirmado' é
                 True quando o PID realmente respondeu no veículo; False
                 quando é só uma intenção de leitura (PID marcado na tabela).
      baudrate — taxa do barramento em bits/s (vira kbps na linha VS19_ENA).
      model    — modelo do veículo gravado no registro de texto (VSRT).

    Retorna uma lista de pares (linha, comentário) na ordem em que devem ser
    enviadas ao equipamento. Entradas além de LIB_MAX_FILTERS são descartadas,
    pois o equipamento só tem 25 filtros (VS1900..VS1924).
    """
    # Remove repetições preservando a ordem e respeita o limite de filtros.
    vistos: set[tuple[int, int]] = set()
    itens: list[tuple[int, int, bool]] = []
    for pid, resp_id, ok in entries:
        chave = (pid, resp_id)
        if chave in vistos:
            continue
        vistos.add(chave)
        itens.append((pid, resp_id, ok))
    itens = itens[:LIB_MAX_FILTERS]

    def nome_pid(pid: int) -> str:
        info = PID_DATABASE.get(pid)
        return info.name if info else f"PID 0x{pid:02X} (não catalogado)"

    linhas: list[tuple[str, str]] = []

    # ── Bloco 1: habilitação da CAN ──────────────────────────────────────────
    kbps = int(round((baudrate or 500000) / 1000))
    linhas.append((
        f">VS19_ENA{kbps:04d},1<",
        f"baudrate {kbps} kbps — ATIVA A CAN EM MODO NORMAL (necessário para "
        "transmitir os requests OBD-II)"))

    # ── Bloco 2: consulta de PIDs ────────────────────────────────────────────
    pids = sorted({p for p, _, _ in itens})
    if pids:
        linhas.append((f">VOBD_ENA1,{max(pids)}<",
                       "CONSULTA DE PIDS (habilita a varredura automática)"))
        for pid in pids:
            linhas.append((f">VOBD{pid},2<",
                           f"consulta o PID {pid} (0x{pid:02X}) — {nome_pid(pid)}"))

    # ── Bloco 3: registros de identificação ──────────────────────────────────
    linhas.append((">VSRM11,2,98,98<",
                   "registro conforme a biblioteca de referência — confirme "
                   "no manual do equipamento"))
    modelo = (model or "VEICULO").strip().upper().replace(" ", "_") or "VEICULO"
    linhas.append((f">VSRT98,{modelo}.0<", "modelo do veículo"))

    # ── Bloco 4a: filtros VS ─────────────────────────────────────────────────
    cts = _assign_cts([p for p, _, _ in itens])
    for n, (pid, resp_id, confirmado) in enumerate(itens):
        info = PID_DATABASE.get(pid)
        length = max(1, min(info.n_bytes if info else 1, 4))
        mask = f"{(1 << (length * 8)) - 1:08X}"
        ct, ct_oficial = cts[pid]
        obs = []
        if not ct_oficial:
            obs.append("CT sugerido — confirme na biblioteca")
        if not confirmado:
            obs.append("PID ainda não confirmado no veículo")
        sufixo = f"  [{'; '.join(obs)}]" if obs else ""
        linhas.append((
            f">VS19{n:02d},{resp_id:05d},{pid},11,{ct:02d},4,{length},"
            f"{mask},0,3<",
            f"{nome_pid(pid)} — ECU 0x{resp_id:03X}, PID {pid} "
            f"(0x{pid:02X}), {length} byte(s), CT{ct:02d}{sufixo}"))

    # ── Bloco 4b: linhas MAT (conversão para valor de engenharia) ────────────
    for n, (pid, _, _) in enumerate(itens):
        ops = MAT_OPS.get(pid, [])
        if not ops:
            continue    # valor bruto já é o final → não precisa de MAT
        info = PID_DATABASE.get(pid)
        unidade = f" [{info.unit}]" if info and info.unit else ""
        linhas.append((
            f">VS19{n:02d}_MAT,{','.join(ops)}<",
            f"MAT {nome_pid(pid)}: {formula_text(pid)}{unidade}"))

    return linhas
