"""
UDS (ISO 14229) — serviço $22 "Read Data By Identifier" sobre ISO-TP/CAN.

POR QUE ESTE MÓDULO EXISTE, SE JÁ TEMOS O core/obd2.py?
    O OBD-II legislado (SAE J1979) usa o serviço 0x01 com PIDs de 1 byte e
    responde 0x41. Os sinais proprietários das montadoras vivem em outro
    serviço: UDS 0x22, com DIDs (Data Identifier) de 2 bytes e resposta 0x62.
    Mesmo barramento, mesmas IDs de diagnóstico, mesmo transporte ISO-TP —
    serviço diferente. Um leitor de modo 01 simplesmente descarta uma
    resposta 0x62, e é por isso que precisamos deste módulo.

        OBD-II modo 01:   02 01 0D ...        →  03 41 0D <A>
        UDS $22:          03 22 01 01 ...     →  05 62 01 01 <A> <B>

SEGURANÇA — ESTE MÓDULO SÓ LÊ:
    O único serviço implementado é 0x22 (Read Data By Identifier), que por
    definição da norma não altera estado nenhum da ECU. Os vizinhos perigosos
    NÃO são implementados e são recusados pela lista branca de transmissão do
    CANBus (ver TX_ALLOWED_SERVICES em core/can_bus.py):

        0x2E Write Data By Identifier ...... ESCREVE no DID
        0x2F Input Output Control .......... aciona atuador
        0x31 Routine Control ............... dispara rotina na ECU
        0x11 ECU Reset / 0x14 Clear DTC .... reinicia / apaga falhas
        0x10 Diagnostic Session Control .... muda sessão (pode degradar o
             veículo: comunicação normal suspensa, DTC gravado, limp mode)

    Se um DID responder que exige sessão estendida, o programa REPORTA o
    código negativo e para — não escala a sessão.

MULTI-FRAME (ISO 15765-2):
    Uma resposta maior que 7 bytes não cabe num quadro CAN. A ECU manda um
    First Frame, espera o nosso Flow Control e então envia os Consecutive
    Frames. Quem remonta isso é a classe IsoTpReader, com três proteções:
      - buffer limitado (MAX_PAYLOAD) — nada de crescer sem controle;
      - timeout por transferência — transferência travada é descartada;
      - checagem do número de sequência — sequência fora de ordem aborta em
        vez de acumular dado corrompido.
    O Flow Control só é enviado quando HÁ um request nosso pendente (ver a
    aba OBD-II): assim não atropelamos a transferência de outro equipamento
    de diagnóstico que esteja conectado no mesmo barramento.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


# ── Serviço e respostas ─────────────────────────────────────────────────────
SID_READ_DATA_BY_ID = 0x22     # request:  22 <DID alto> <DID baixo>
RESP_READ_DATA_BY_ID = 0x62    # resposta positiva: 0x22 + 0x40
NEGATIVE_RESPONSE = 0x7F       # resposta negativa: 7F <serviço> <NRC>

# ── IDs de diagnóstico em CAN 11 bits (ISO 15765-4) ─────────────────────────
# São as MESMAS do OBD-II: o gateway atende os dois serviços no mesmo par.
REQUEST_FUNCTIONAL = 0x7DF          # request funcional (todas as ECUs)
REQUEST_PHYSICAL_BASE = 0x7E0       # request físico: 0x7E0+n
RESP_MIN, RESP_MAX = 0x7E8, 0x7EF   # faixa das respostas

# Quadro de Flow Control "clear to send": libera a ECU a enviar os
# Consecutive Frames sem limite de bloco e sem tempo mínimo entre quadros.
FLOW_CONTROL_CTS = bytes([0x30, 0x00, 0x00, 0x55, 0x55, 0x55, 0x55, 0x55])

# Teto de bytes remontados. As leituras de DID aqui têm poucos bytes; este
# limite existe para que um First Frame malformado (ou malicioso) anunciando
# 4095 bytes não faça o programa alocar memória à toa.
MAX_PAYLOAD = 256

# Tempo máximo que uma transferência multi-frame pode ficar incompleta.
TRANSFER_TIMEOUT = 1.0   # segundos


# ── Códigos de resposta negativa (NRC) ──────────────────────────────────────
# Chegam como 7F 22 <NRC>. Traduzir é essencial: o NRC diz se o DID não
# existe, se falta sessão/segurança ou se o momento está errado.
NRC_NAMES: dict[int, str] = {
    0x10: "rejeição geral",
    0x11: "serviço não suportado",
    0x12: "sub-função não suportada",
    0x13: "tamanho da mensagem incorreto",
    0x14: "resposta muito longa",
    0x21: "ECU ocupada — repita a requisição",
    0x22: "condições incorretas (ex.: motor precisa estar ligado)",
    0x24: "erro de sequência de requisição",
    0x31: "DID fora de faixa (não existe nesta ECU)",
    0x33: "acesso de segurança negado (exige 0x27 seed-key)",
    0x35: "chave inválida",
    0x36: "número de tentativas excedido",
    0x37: "tempo de espera obrigatório não decorrido",
    0x78: "resposta pendente (ECU processando)",
    0x7E: "sub-função não suportada na sessão ativa",
    0x7F: "serviço não suportado na sessão ativa (exigiria sessão estendida)",
}


def nrc_name(nrc: int) -> str:
    """Descrição legível de um código de resposta negativa."""
    return NRC_NAMES.get(nrc, f"NRC 0x{nrc:02X} (não catalogado)")


@dataclass
class DID:
    """
    Descreve um Data Identifier do serviço $22.

    Campos:
      did .......... identificador de 2 bytes (ex.: 0x0101).
      name ......... nome do sinal.
      unit ......... unidade de engenharia informada pela montadora.
      n_bytes ...... tamanho esperado do dado, quando conhecido (None =
                     descobrir na prática).
      scale/offset . conversão HIPOTÉTICA (valor = bruto × scale + offset).
                     A montadora informou a unidade mas NÃO a escala, então
                     tudo aqui é hipótese a confirmar; o programa sempre
                     mostra o valor BRUTO junto.
      hypothesis ... rótulo legível da hipótese (aparece na interface).
      source ....... de onde veio a informação do DID.
      kind ......... natureza do dado:
                       "num"   — número (o caso comum: rotação, temperatura…)
                       "ascii" — TEXTO em ASCII, como o chassi (VIN) e os
                                 números de série/peça. Texto não tem escala
                                 nem faixa: converter para inteiro seria
                                 absurdo, então a interface e a documentação
                                 exibem a string decodificada.
    """
    did: int
    name: str
    unit: str = ""
    n_bytes: Optional[int] = None
    scale: Optional[float] = None
    offset: float = 0.0
    hypothesis: str = ""
    source: str = ""
    kind: str = "num"


# ── Banco de DIDs ───────────────────────────────────────────────────────────
# Lista informada pela VW Caminhões e Ônibus (VWCO) para leitura via UDS $22.
# ATENÇÃO: as escalas abaixo são HIPÓTESES baseadas nas convenções usuais
# (RPM ÷4, odômetro ÷10, percentuais ×100/255, temperaturas −40). A montadora
# informou apenas a unidade. Confirme com leitura real antes de usar o valor
# convertido — o valor bruto é sempre exibido e exportado.
DID_DATABASE: dict[int, DID] = {
    0x0101: DID(0x0101, "Rotação do Motor", "rpm", None, 0.25, 0.0,
                "÷ 4 (hipótese)", "VWCO"),
    0x0116: DID(0x0116, "Pedal do Acelerador", "%", None, 100 / 255, 0.0,
                "× 100/255 (hipótese)", "VWCO"),
    0x018B: DID(0x018B, "Pedal de Embreagem", "%", None, 100 / 255, 0.0,
                "× 100/255 (hipótese)", "VWCO"),
    0x1009: DID(0x1009, "Temperatura do Motor", "°C", None, 1.0, -40.0,
                "− 40 (hipótese)", "VWCO"),
    0x1615: DID(0x1615, "Nível de AdBlue", "%", None, 100 / 255, 0.0,
                "× 100/255 (hipótese)", "VWCO"),
    0xB003: DID(0xB003, "Temperatura do Ar", "°C", None, 1.0, -40.0,
                "− 40 (hipótese)", "VWCO"),
    # A VWCO listou o MESMO DID (B005) para aceleração longitudinal e lateral:
    # provavelmente os dois eixos vêm no mesmo DID, em bytes diferentes. Sem a
    # escala não dá para separar — exibimos o bruto completo.
    0xB005: DID(0xB005, "Acelerações Longitudinal + Lateral", "m/s²", None,
                None, 0.0, "dois eixos no mesmo DID — bruto", "VWCO"),
    0xD001: DID(0xD001, "Terminal 15 (KL15 / ignição)", "", 1, 1.0, 0.0,
                "status (0/1)", "VWCO"),
    0xE101: DID(0xE101, "Odômetro", "km", None, 0.1, 0.0,
                "÷ 10 (hipótese)", "VWCO"),

    # ── DIDs de identificação padronizados (ISO 14229-1, Anexo C) ───────────
    # Estes NÃO são proprietários: a norma fixa o identificador e o formato,
    # então valem em qualquer ECU que implemente UDS — diferente dos DIDs
    # acima, que só existem porque a montadora informou.
    0xF190: DID(0xF190, "Chassi (VIN)", "", 17, None, 0.0,
                "texto ASCII de 17 caracteres", "ISO 14229-1", kind="ascii"),
}

# DIDs cujo valor é texto, não número (atalho para a interface).
TEXT_DIDS = {d for d, i in DID_DATABASE.items() if i.kind == "ascii"}

# Sinais que a VWCO marcou como prioritários no pedido de validação.
PRIORITY_DIDS = (0x0101, 0xD001, 0xE101)


# ── Notas de referência do protocolo (usadas na documentação exportada) ─────
UDS_PROTOCOL_NOTES: list[tuple[str, str]] = [
    ("Norma",
     "ISO 14229 (UDS) sobre transporte ISO 15765-2 (ISO-TP) em CAN. O "
     "programa implementa APENAS o serviço 0x22 — Read Data By Identifier."),
    ("Request",
     "22 <DID alto> <DID baixo> — o DID tem 2 bytes. No quadro CAN sai como "
     "[0x03, 0x22, alto, baixo, preenchimento]: 0x03 é o comprimento útil."),
    ("Resposta positiva",
     "62 <DID alto> <DID baixo> <dados...> — 0x62 = 0x22 + 0x40. Os bytes de "
     "dados vêm em big-endian (byte mais significativo primeiro)."),
    ("Resposta negativa",
     "7F 22 <NRC>. O NRC diz o motivo: 0x31 = DID não existe nesta ECU, "
     "0x33 = exige acesso de segurança, 0x7F = exigiria sessão estendida. "
     "A resposta negativa NÃO repete o DID — por isso o programa mantém "
     "apenas um request pendente por vez, para saber a quem ela pertence."),
    ("Multi-frame",
     "Resposta acima de 7 bytes vem em First Frame + Consecutive Frames. O "
     "programa responde o Flow Control (30 00 00) endereçado à ECU que "
     "respondeu e remonta o payload."),
    ("Escalas",
     "O serviço 0x22 devolve o valor BRUTO: a norma não define fator/offset "
     "para DIDs proprietários. As conversões desta documentação são HIPÓTESES "
     "a confirmar com a montadora; o valor bruto é sempre registrado."),
    ("Serviços NÃO implementados",
     "0x2E (escreve DID), 0x2F (aciona atuador), 0x31 (rotina), 0x11 (reset), "
     "0x14 (apaga falhas), 0x10/0x3E (sessão) e 0x27 (seed-key). A lista "
     "branca de transmissão do programa recusa todos eles no barramento."),
]


def flow_control_id_for(resp_id: int) -> int:
    """
    ID de request para onde mandar o Flow Control de uma resposta multi-frame.

    O Flow Control tem que ser endereçado À ECU que está transmitindo — não
    pode ir na ID funcional 0x7DF. A ECU que responde em 0x7E8+n escuta em
    0x7E0+n, então o FC vai para lá, mesmo que o request original tenha saído
    em broadcast.
    """
    return REQUEST_PHYSICAL_BASE + ((resp_id - RESP_MIN) & 0x07)


def build_read_did_request(did: int,
                           target_ecu: Optional[int] = None) -> tuple[int, bytes]:
    """
    Monta o quadro de request do serviço $22 para um DID.

    Formato ISO-TP Single Frame: [0x03, 0x22, DID_alto, DID_baixo, padding].
    O 0x03 é o comprimento útil (serviço + 2 bytes de DID).

    target_ecu: None = ID funcional 0x7DF (todas as ECUs);
                0..7 = ID física 0x7E0+n (só aquele módulo).
    """
    if target_ecu is None:
        can_id = REQUEST_FUNCTIONAL
    else:
        can_id = REQUEST_PHYSICAL_BASE + (int(target_ecu) & 0x07)
    data = bytes([0x03, SID_READ_DATA_BY_ID,
                  (did >> 8) & 0xFF, did & 0xFF,
                  0x55, 0x55, 0x55, 0x55])
    return can_id, data


# ── Remontagem ISO-TP ───────────────────────────────────────────────────────

@dataclass
class _Transfer:
    """Uma transferência multi-frame em andamento, por ECU de origem."""
    total: int                 # bytes anunciados no First Frame
    data: bytearray            # o que já foi remontado
    next_seq: int              # próximo número de sequência esperado (1..15→0)
    started: float             # instante do First Frame (para o timeout)
    frames: int = 1            # quantos quadros CAN já compuseram a resposta
    first_raw: bytes = b""     # o First Frame cru (vai para a documentação)


@dataclass
class IsoTpEvent:
    """
    Resultado de alimentar um quadro no IsoTpReader.

    kind:
      "complete" — payload completo em .payload (Single Frame ou último CF)
      "need_fc"  — First Frame recebido: o chamador DEVE enviar Flow Control
      "progress" — Consecutive Frame aceito, ainda falta dado
      "ignored"  — quadro que não nos interessa
      "error"    — transferência abortada (motivo em .detail)
    """
    kind: str
    src: int = 0
    payload: bytes = b""
    detail: str = ""
    frames: int = 1
    # Primeiro quadro CAN cru da resposta (o Single Frame, ou o First Frame de
    # uma transferência multi-frame). A documentação exportada registra este
    # quadro como "payload RX" — é o que a montadora pede para validação.
    first_frame: bytes = b""


class IsoTpReader:
    """
    Remonta respostas ISO-TP vindas das ECUs (lado RECEPTOR apenas).

    Mantém uma transferência por ID de origem, porque em request funcional
    várias ECUs podem responder ao mesmo tempo. Não implementa o lado
    transmissor multi-frame: nossos requests sempre cabem num Single Frame.
    """

    def __init__(self, timeout: float = TRANSFER_TIMEOUT,
                 max_payload: int = MAX_PAYLOAD):
        self._transfers: dict[int, _Transfer] = {}
        self._timeout = timeout
        self._max_payload = max_payload

    def reset(self):
        """Descarta todas as transferências em andamento."""
        self._transfers.clear()

    def purge(self, now: Optional[float] = None) -> list[int]:
        """
        Remove transferências paradas há mais que o timeout.

        Devolve as IDs descartadas, para que o chamador possa avisar o
        operador de que a resposta veio incompleta.
        """
        now = time.time() if now is None else now
        vencidas = [src for src, t in self._transfers.items()
                    if now - t.started > self._timeout]
        for src in vencidas:
            del self._transfers[src]
        return vencidas

    def feed(self, can_id: int, data: bytes,
             now: Optional[float] = None) -> IsoTpEvent:
        """
        Processa um quadro CAN recebido e devolve o que fazer com ele.

        Só considera quadros na faixa de resposta de diagnóstico
        (0x7E8..0x7EF); qualquer outro é ignorado.
        """
        now = time.time() if now is None else now
        if not (RESP_MIN <= can_id <= RESP_MAX):
            return IsoTpEvent("ignored", can_id)
        if not data:
            return IsoTpEvent("ignored", can_id)

        pci = data[0] >> 4

        # ── Single Frame: a resposta inteira num quadro ──────────────────────
        if pci == 0x0:
            length = data[0] & 0x0F
            if length < 1 or length > 7 or len(data) < 1 + length:
                return IsoTpEvent("ignored", can_id)
            # Um SF novo invalida qualquer transferência pendente dessa ECU.
            self._transfers.pop(can_id, None)
            return IsoTpEvent("complete", can_id, bytes(data[1:1 + length]),
                              frames=1, first_frame=bytes(data))

        # ── First Frame: começa uma transferência e pede Flow Control ───────
        if pci == 0x1:
            if len(data) < 8:
                return IsoTpEvent("ignored", can_id)
            total = ((data[0] & 0x0F) << 8) | data[1]
            if total <= 7:
                # Tamanho inválido para multi-frame — quadro suspeito.
                return IsoTpEvent("ignored", can_id)
            if total > self._max_payload:
                return IsoTpEvent(
                    "error", can_id,
                    detail=f"resposta anunciada com {total} bytes — acima do "
                           f"limite de {self._max_payload}; descartada")
            self._transfers[can_id] = _Transfer(
                total=total, data=bytearray(data[2:8]), next_seq=1,
                started=now, frames=1, first_raw=bytes(data))
            return IsoTpEvent("need_fc", can_id, first_frame=bytes(data))

        # ── Consecutive Frame: continua a transferência ──────────────────────
        if pci == 0x2:
            t = self._transfers.get(can_id)
            if t is None:
                # CF sem First Frame nosso → é transferência de outro
                # equipamento (ou chegou fora de ordem). Ignorar.
                return IsoTpEvent("ignored", can_id)
            seq = data[0] & 0x0F
            if seq != t.next_seq:
                del self._transfers[can_id]
                return IsoTpEvent(
                    "error", can_id,
                    detail=f"sequência fora de ordem (esperado {t.next_seq}, "
                           f"recebido {seq}) — transferência abortada")
            falta = t.total - len(t.data)
            t.data.extend(data[1:1 + min(7, falta)])
            t.next_seq = (t.next_seq + 1) & 0x0F   # 1..15 e volta a 0
            t.frames += 1
            t.started = now                        # renova o timeout
            if len(t.data) >= t.total:
                del self._transfers[can_id]
                return IsoTpEvent("complete", can_id, bytes(t.data[:t.total]),
                                  frames=t.frames, first_frame=t.first_raw)
            return IsoTpEvent("progress", can_id, frames=t.frames)

        # ── Flow Control: é quadro de TESTADOR, não nos interessa receber ────
        return IsoTpEvent("ignored", can_id)


# ── Interpretação da resposta ───────────────────────────────────────────────

def parse_read_did_response(payload: bytes) -> Optional[dict]:
    """
    Interpreta um payload ISO-TP já remontado como resposta do serviço $22.

    Devolve:
      resposta positiva → {"kind": "positive", "did": int, "data": bytes}
      resposta negativa → {"kind": "negative", "sid": int, "nrc": int,
                           "nrc_name": str}
      None → não é resposta do serviço $22

    O payload NÃO inclui o byte de PCI do ISO-TP (o IsoTpReader já o removeu).
    """
    if len(payload) < 3:
        return None

    # 7F <serviço> <NRC> — resposta negativa.
    if payload[0] == NEGATIVE_RESPONSE:
        if payload[1] != SID_READ_DATA_BY_ID:
            return None       # negativa de outro serviço (não é nossa)
        return {"kind": "negative", "sid": payload[1], "nrc": payload[2],
                "nrc_name": nrc_name(payload[2])}

    # 62 <DID alto> <DID baixo> <dados...> — resposta positiva.
    if payload[0] != RESP_READ_DATA_BY_ID:
        return None
    did = (payload[1] << 8) | payload[2]
    return {"kind": "positive", "did": did, "data": bytes(payload[3:])}


def raw_to_int(data: bytes) -> int:
    """
    Converte os bytes de dado num inteiro sem sinal, big-endian.

    Big-endian é a ordem usada pelo diagnóstico automotivo (byte mais
    significativo primeiro), tanto no OBD-II quanto nos DIDs UDS.
    """
    valor = 0
    for b in data:
        valor = (valor << 8) | b
    return valor


def decode_ascii(data: bytes) -> str:
    """
    Decodifica bytes ASCII de identificação (chassi, número de série, peça).

    Tolerante de propósito: caracteres fora da faixa imprimível viram '.' em
    vez de estourar exceção. Padding com 0x00 e 0xFF é comum no fim do campo
    e é removido, assim como espaços nas pontas.
    """
    if not data:
        return ""
    texto = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return texto.strip(". \x00").strip()


def interpret(did: int, data: bytes):
    """
    Converte os bytes lidos de um DID no valor a exibir.

    Devolve (valor, rótulo). O valor pode ser:
      • float — quando há hipótese de escala (ver a advertência abaixo);
      • str   — quando o DID é de texto (kind="ascii"), como o chassi;
      • None  — quando não há como converter e só o bruto faz sentido.

    Para DIDs proprietários a escala é HIPÓTESE, não norma: o rótulo devolvido
    diz isso e a interface mostra o valor bruto ao lado.
    """
    info = DID_DATABASE.get(did)
    if info is None or not data:
        return None, (info.hypothesis if info else "")
    if info.kind == "ascii":
        # Texto não tem escala: devolve a string decodificada.
        return decode_ascii(data), info.hypothesis
    if info.scale is None:
        return None, info.hypothesis
    try:
        valor = raw_to_int(data) * info.scale + info.offset
    except Exception:
        return None, info.hypothesis
    return valor, info.hypothesis


def format_hex(data: bytes) -> str:
    """Bytes em hexadecimal separados por espaço (ex.: '62 01 01 0C A9')."""
    return " ".join(f"{b:02X}" for b in data) if data else "—"
