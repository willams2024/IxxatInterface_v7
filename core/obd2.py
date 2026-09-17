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
    0x46: PID(0x46, "Temp. Ambiente",               1, "°C",   lambda d: d[0] - 40, -40, 215),
    0x5C: PID(0x5C, "Temp. Óleo do Motor",          1, "°C",   lambda d: d[0] - 40, -40, 215),
    0x5E: PID(0x5E, "Taxa de Consumo de Combustível", 2, "L/h", lambda d: (d[0] * 256 + d[1]) / 20, 0, 3277),
}


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


def parse_response(can_id: int, data: bytes) -> Optional[dict]:
    """
    Interpreta um quadro de RESPOSTA OBD-II modo 01.

    Retorna um dicionário {src, pid, value, unit, name} quando o quadro é uma
    resposta válida de um PID conhecido, ou None caso contrário.

    O campo 'src' é a ID da ECU que respondeu (0x7E8..0x7EF) e é ESSENCIAL:
    com request funcional (0x7DF) várias ECUs respondem ao mesmo PID, cada uma
    com o SEU valor. Sem saber a origem, respostas de módulos diferentes se
    confundiriam como se fossem a mesma leitura.

    Só trata Single Frame (SF): o nibble alto do byte 0 deve ser 0 e o byte 1
    deve ser 0x41 (resposta ao modo 01). Multi-frame é ignorado.
    """
    # A resposta tem que vir na faixa física 0x7E8..0x7EF.
    if not (OBD_RESP_MIN <= can_id <= OBD_RESP_MAX):
        return None
    if len(data) < 3:
        return None

    # ISO-TP: nibble alto do 1º byte identifica o tipo de frame (0 = Single).
    if (data[0] >> 4) != 0:
        return None
    if data[1] != 0x41:           # 0x41 = 0x01 (modo) + 0x40 (flag de resposta)
        return None

    pid = data[2]
    info = PID_DATABASE.get(pid)
    if info is None:
        return None

    payload = list(data[3:3 + info.n_bytes])
    if len(payload) < info.n_bytes:
        return None
    try:
        value = info.formula(payload)
    except Exception:
        return None
    # 'src' preserva QUAL ECU respondeu — sem isso, respostas de módulos
    # diferentes para o mesmo PID seriam indistinguíveis.
    return {"src": can_id, "pid": pid, "value": value,
            "unit": info.unit, "name": info.name}
