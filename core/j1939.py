"""
J1939 protocol decoder and PGN/SPN database.

Database completa conforme FMS-Standard description Version 05 (11.11.2025)
da Task Force HDEI/BCEI (Daimler, Volvo, MAN, Scania, Renault, IVECO, DAF,
VDL, Ford Otosan).

Application Layer: SAE J1939/71 (J1939DA_202007)
Data Link Layer:   SAE J1939/21
Physical Layer:    ISO 11898 (250 kb/s)

Total: 45 PGNs com ~220 SPNs documentados.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SPN:
    """
    SPN (Suspect Parameter Number) — representa um parâmetro individual
    dentro de uma mensagem J1939, conforme definido pela norma SAE J1939/71.

    Cada SPN descreve EXATAMENTE onde, dentro dos 8 bytes de dados de um quadro
    CAN, um valor está localizado e como convertê-lo do valor bruto (raw) lido
    do barramento para a grandeza física real (engenharia).

    ATENÇÃO: os números deste banco (start_byte, start_bit, length_bits, factor,
    offset, min/max) vêm DIRETAMENTE da norma SAE J1939 / FMS-Standard. São
    valores críticos e não devem ser alterados.

    Campos:
      number       — número oficial do SPN na norma SAE J1939 (identificador único).
      name         — descrição legível do parâmetro (em português neste projeto).
      start_byte   — byte inicial onde o dado começa, indexado a partir de 0
                     (0 = primeiro byte do payload de 8 bytes).
      start_bit    — bit inicial DENTRO do start_byte, indexado a partir de 0,
                     onde 0 = bit menos significativo (LSB). Usado em campos
                     menores que 1 byte (bitfields), p. ex. flags de 2 ou 3 bits.
      length_bits  — tamanho do campo em bits. Valores típicos: 1..8 (cabe em
                     um byte / bitfield), 16 (dois bytes) ou 32 (quatro bytes).
      factor       — fator de escala (resolução). O valor físico é obtido
                     multiplicando o valor bruto por este fator. Ex.: 0.125 rpm/bit.
      offset       — deslocamento somado APÓS aplicar o fator. Permite representar
                     grandezas com valores negativos (ex.: temperatura com offset -40).
      unit         — unidade de medida da grandeza física (ex.: "°C", "km/h", "%").
      min_val      — valor mínimo válido segundo a norma (apenas informativo).
      max_val      — valor máximo válido segundo a norma (apenas informativo).
      is_bitfield  — True quando o campo é um conjunto de bits / enumeração
                     (status, flags) e não uma grandeza analógica contínua.
                     Quando True, o decode trata o campo como leitura de bits
                     dentro de um único byte.

    Fórmula geral de conversão:  valor_fisico = raw * factor + offset
    """
    number: int
    name: str
    start_byte: int   # 0-indexed
    start_bit: int    # 0-indexed within byte (0=LSB)
    length_bits: int
    factor: float
    offset: float
    unit: str
    min_val: float = 0.0
    max_val: float = 0.0
    is_bitfield: bool = False

    def decode(self, data: bytes) -> Optional[float]:
        """
        Extrai e converte este SPN a partir do payload (bytes) de uma mensagem.

        Retorna o valor físico já convertido (raw * factor + offset), ou None
        quando o dado não está disponível / não cabe no payload recebido.

        Passo a passo:
        """
        # Proteção de limites: se o payload recebido nem chega ao byte inicial
        # deste SPN, não há o que decodificar. Evita IndexError.
        if len(data) < self.start_byte + 1:
            return None

        # --- Caso 1: bitfield OU campo que cabe em até 1 byte (<= 8 bits) ---
        # Lê apenas o byte start_byte, desloca para a direita até o start_bit
        # (alinhando o campo ao LSB) e aplica uma máscara para isolar somente
        # os length_bits desejados. Ex.: campo de 2 bits começando no bit 4.
        if self.is_bitfield or self.length_bits <= 8:
            byte_val = data[self.start_byte]
            mask = (1 << self.length_bits) - 1   # gera 'length_bits' bits em 1 (ex.: 2 bits -> 0b11)
            raw = (byte_val >> self.start_bit) & mask

        # --- Caso 2: campo de 16 bits (2 bytes) ---
        # J1939 usa ordem LITTLE-ENDIAN: o primeiro byte é o menos significativo.
        # Por isso o segundo byte é deslocado 8 bits para a esquerda e combinado.
        elif self.length_bits == 16:
            if len(data) < self.start_byte + 2:   # verifica se há 2 bytes disponíveis
                return None
            raw = data[self.start_byte] | (data[self.start_byte + 1] << 8)

        # --- Caso 3: campo de 32 bits (4 bytes) ---
        # Também little-endian; usa int.from_bytes com ordem 'little' para montar
        # o valor de 4 bytes (LSB primeiro).
        elif self.length_bits == 32:
            if len(data) < self.start_byte + 4:   # verifica se há 4 bytes disponíveis
                return None
            raw = int.from_bytes(data[self.start_byte:self.start_byte + 4], 'little')

        # Tamanho de campo não suportado por este decodificador.
        else:
            return None

        # --- Tratamento de "valor não disponível" / "erro" (J1939) ---
        # Por convenção da norma, um campo todo preenchido com 1s (todos os bits
        # = 0xFF...) significa "not available" e o penúltimo valor (all_ff - 1)
        # significa "error indicator". Em ambos os casos não há leitura válida.
        all_ff = (1 << self.length_bits) - 1     # maior valor possível p/ length_bits (todos os bits em 1)
        if raw == all_ff or raw == all_ff - 1:
            return None

        # Conversão final do valor bruto para grandeza física (engenharia).
        return raw * self.factor + self.offset


@dataclass
class PGN:
    """
    PGN (Parameter Group Number) — representa um grupo de parâmetros J1939,
    isto é, o "tipo de mensagem" que trafega no barramento CAN. Cada PGN
    agrupa um ou mais SPNs que compartilham o mesmo quadro de 8 bytes e a
    mesma taxa de transmissão.

    Campos:
      number               — número oficial do PGN na norma SAE J1939
                             (ex.: 61444). É a chave usada no PGN_DATABASE e o
                             valor extraído do ID CAN de 29 bits em decode_29bit_id().
      name                 — nome descritivo completo da mensagem (ex.:
                             "Electronic Engine Controller 1").
      acronym              — sigla curta da mensagem conforme FMS/J1939
                             (ex.: "EEC1"), usada na exibição compacta.
      spns                 — lista de objetos SPN contidos nesta mensagem. Cada
                             SPN sabe se localizar e se decodificar dentro do payload.
      transmission_rate_ms — período nominal de transmissão em milissegundos
                             segundo a norma (ex.: 20 ms, 100 ms, 1000 ms).
                             0 = enviado sob demanda / via TP-BAM, não periódico.
    """
    number: int
    name: str
    acronym: str
    spns: list = field(default_factory=list)
    transmission_rate_ms: int = 100


# ───────────────────────────────────────────────────────────────────────────────
# TELL TALE TABLE (FMS1 — PGN 64893)
# Mapeia (Block ID, Status Index) → (ISO No., Nome)
# 5 blocks × 15 statuses = 75 indicadores possíveis
#
# O que é um "Tell Tale": é uma luz/indicador do painel do veículo (ex.: farol
# alto, freio de estacionamento, falha do motor/MIL, nível de combustível, etc.).
# A mensagem FMS1 (PGN 64893) transmite o estado desses indicadores de forma
# compacta. Por não caberem todos num único quadro de 8 bytes, os 75 indicadores
# são organizados em 5 BLOCOS (Block ID = 0..4); cada bloco carrega 15 status
# (Status 1..15), totalizando 5 × 15 = 75 indicadores possíveis.
#
# Estrutura da tabela (dicionário Python):
#   CHAVE  = tupla (block_id, status_idx)  -> identifica qual indicador.
#   VALOR  = tupla (numero_ISO, nome)      -> "numero_ISO" é a referência do
#            símbolo na norma ISO 2575 (ou "—" quando não há número padronizado),
#            e "nome" é a descrição em português exibida ao usuário.
#
# A função decode lê o Block ID (nibble do byte 0) e depois os 15 campos de
# status; combinando (block_id, status_idx) consulta-se esta tabela para obter
# o nome do indicador correspondente.
# ───────────────────────────────────────────────────────────────────────────────

TELL_TALE_TABLE: dict[tuple[int, int], tuple[str, str]] = {
    # Block 0
    (0,  1): ("27",   "Ar condicionado (Cooling A/C)"),
    (0,  2): ("82",   "Farol alto"),
    (0,  3): ("83",   "Farol baixo"),
    (0,  4): ("84",   "Setas (Turn signals)"),
    (0,  5): ("85",   "Pisca-alerta"),
    (0,  6): ("100",  "Acessibilidade (PNE)"),
    (0,  7): ("238",  "Freio de Estacionamento"),
    (0,  8): ("239",  "Falha no sistema de freios"),
    (0,  9): ("242",  "Tampa aberta"),
    (0, 10): ("245",  "Nível de combustível"),
    (0, 11): ("246",  "Temperatura do líquido de arrefecimento"),
    (0, 12): ("247",  "Condição de carga da bateria"),
    (0, 13): ("248",  "Óleo do motor"),
    (0, 14): ("456",  "Luzes de posição / lanterna"),
    (0, 15): ("633",  "Luz de neblina dianteira"),
    # Block 1
    (1,  1): ("634",  "Luz de neblina traseira"),
    (1,  2): ("637",  "Aquecedor de Estacionamento"),
    (1,  3): ("640",  "MIL / Falha do motor"),
    (1,  4): ("717",  "Manutenção / Service"),
    (1,  5): ("1168", "Temperatura óleo transmissão"),
    (1,  6): ("1396", "Falha da transmissão"),
    (1,  7): ("1407", "Falha ABS"),
    (1,  8): ("1408", "Pastilhas de freio gastas"),
    (1,  9): ("1422", "Fluido limpador para-brisa"),
    (1, 10): ("1434", "Falha do pneu"),
    (1, 11): ("1603", "Falha geral / Mau funcionamento"),
    (1, 12): ("2426", "Temperatura óleo do motor"),
    (1, 13): ("2427", "Nível de óleo do motor"),
    (1, 14): ("2429", "Nível líquido arrefecimento"),
    (1, 15): ("2440", "Nível fluido da direção"),
    # Block 2
    (2,  1): ("2441", "Falha direção"),
    (2,  2): ("2461", "Controle de altura (Nivelamento)"),
    (2,  3): ("2574", "Retarder"),
    (2,  4): ("2596", "Falha emissão motor (MIL)"),
    (2,  5): ("2630", "ESC (Controle de estabilidade)"),
    (2,  6): ("—",    "Luzes de freio"),
    (2,  7): ("—",    "Articulação"),
    (2,  8): ("—",    "Pedido de parada (Ônibus)"),
    (2,  9): ("—",    "Pedido carrinho de bebê"),
    (2, 10): ("—",    "Freio de ponto de ônibus"),
    (2, 11): ("2946", "Nível ARLA/AdBlue"),
    (2, 12): ("—",    "Elevação (Ônibus)"),
    (2, 13): ("—",    "Abaixamento (Ônibus)"),
    (2, 14): ("—",    "Ajoelhamento (Kneeling)"),
    (2, 15): ("—",    "Temperatura compartimento motor"),
    # Block 3
    (3,  1): ("—",    "Pressão de ar auxiliar"),
    (3,  2): ("2432", "Filtro de ar obstruído"),
    (3,  3): ("2452", "Pressão diferencial filtro combustível"),
    (3,  4): ("249",  "Cinto de segurança"),
    (3,  5): ("—",    "EBS"),
    (3,  6): ("2682", "Saída de faixa (LDW)"),
    (3,  7): ("—",    "Frenagem de emergência (AEB)"),
    (3,  8): ("2581", "ACC (Cruzeiro adaptativo)"),
    (3,  9): ("—",    "Reboque conectado"),
    (3, 10): ("2444", "ABS Reboque 1/2"),
    (3, 11): ("2108", "Airbag"),
    (3, 12): ("—",    "EBS Reboque 1/2"),
    (3, 13): ("—",    "Indicação Tacógrafo"),
    (3, 14): ("2649", "ESC desativado"),
    (3, 15): ("—",    "LDW desativado"),
    # Block 4 (elétricos/híbridos)
    (4,  1): ("2433", "Filtro de fuligem (Soot Filter)"),
    (4,  2): ("2633", "Falha motor elétrico"),
    (4,  3): ("—",    "Adulteração ARLA"),
    (4,  4): ("—",    "Sistema multiplex"),
    (4,  5): ("2632", "Pacote de baterias"),
    (4,  6): ("6042", "Sistema de alta tensão"),
    (4,  7): ("3129", "Temperatura pacote baterias"),
    (4,  8): ("2639", "Performance limitada motor elétrico"),
    (4,  9): ("2455", "Resfriamento pacote baterias"),
    (4, 10): ("—",    "Status de carga"),
    (4, 11): ("—",    "Balanceamento bateria tração"),
    (4, 12): ("—",    "Status pantógrafo"),
    (4, 13): ("—",    "Detecção de incêndio / alarme"),
    (4, 14): ("—",    "Regeneração perturbada"),
    (4, 15): ("—",    "Evento térmico bateria HV"),
}


def tell_tale_name(block_id: int, status_idx: int) -> str:
    """
    Retorna o nome do tell-tale (indicador de painel) conforme FMS1 (PGN 64893).

    Parâmetros:
      block_id   — identificador do bloco (0..4), lido do nibble do byte 0 da
                   mensagem FMS1.
      status_idx — índice do status dentro do bloco (1..15).

    Funcionamento:
      Consulta a TELL_TALE_TABLE pela chave (block_id, status_idx). Cada entrada
      é a tupla (numero_ISO, nome); retornamos o segundo elemento (info[1]), que
      é o nome legível. Caso a combinação não exista na tabela (posição reservada
      ou não atribuída pela norma), devolve um rótulo genérico indicando que
      aquele bloco/status é reservado.
    """
    # .get retorna None se a chave (block_id, status_idx) não estiver na tabela.
    info = TELL_TALE_TABLE.get((block_id, status_idx))
    # info[1] = nome do indicador; fallback para texto "(reservado)" quando ausente.
    return info[1] if info else f"Tell Tale B{block_id}/S{status_idx} (reservado)"


# ─── J1939 PGN DATABASE — FMS Standard v05 (Nov/2025) ────────────────────────
#
# Banco de dados central do decodificador. É um dicionário onde:
#   CHAVE  = número do PGN (int) — o mesmo valor extraído do ID CAN de 29 bits.
#   VALOR  = objeto PGN totalmente preenchido (nome, sigla, taxa e lista de SPNs).
#
# Ao receber uma mensagem CAN, decode_message() extrai o PGN do ID e faz a busca
# direta aqui (PGN_DATABASE.get(pgn)); se encontrar, percorre os SPNs do PGN e
# decodifica cada parâmetro chamando SPN.decode() sobre o payload de 8 bytes.
#
# A ordem/organização abaixo segue as seções do FMS-Standard v05:
#   1.1  Parâmetros comuns a BUS + TRUCK
#   1.2  Parâmetros apenas de TRUCK
#   1.3  Parâmetros apenas de BUS
#   +    PGNs adicionais comuns em J1939 mas fora do escopo do FMS
# Cada entrada já traz, no comentário de cabeçalho, o número decimal do PGN,
# seu valor hexadecimal, a sigla e o nome conforme a norma.
#
# IMPORTANTE: todos os parâmetros numéricos dos SPNs (start_byte, start_bit,
# length_bits, factor, offset, min, max) provêm da norma — NÃO devem ser alterados.

PGN_DATABASE: dict[int, PGN] = {

    # ════════════════════════════════════════════════════════════════════════
    # 1.1  PARÂMETROS BUS + TRUCK
    # ════════════════════════════════════════════════════════════════════════

    # ── 1.1.1   65257 (0xFEE9) LFC — Fuel Consumption (Liquid) ────────────────
    65257: PGN(65257, "Fuel Consumption (Liquid)", "LFC", transmission_rate_ms=1000, spns=[
        SPN(182, "Combustível Viagem (L)",             0, 0, 32, 0.5,      0.0,   "L",    0, 2105540607.5),
        SPN(250, "Combustível Total (L)",              4, 0, 32, 0.5,      0.0,   "L",    0, 2105540607.5),
    ]),

    # ── 1.1.2   65276 (0xFEFC) DD1 — Dash Display 1 ───────────────────────────
    65276: PGN(65276, "Dash Display 1", "DD1", transmission_rate_ms=1000, spns=[
        SPN(96,  "Nível Combustível 1 (%)",            1, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(38,  "Nível Combustível 2 (%)",            6, 0,  8, 0.4,      0.0,   "%",    0, 100),
    ]),

    # ── 1.1.3   61444 (0xF004) EEC1 — Engine Controller 1 ─────────────────────
    # Exemplo de leitura dos campos de cada SPN (assinatura):
    #   SPN(number, name, start_byte, start_bit, length_bits, factor, offset,
    #       unit, min_val, max_val, is_bitfield)
    # Ex.: "Velocidade do Motor" -> byte 3, 16 bits, factor 0.125 rpm/bit, offset 0.
    61444: PGN(61444, "Electronic Engine Controller 1", "EEC1", transmission_rate_ms=20, spns=[
        SPN(512, "Chave de Embreagem",                 0, 3,  2, 1.0,      0.0,   "",     0,    3, True),
        SPN(513, "Torque Real do Motor (%)",           2, 0,  8, 1.0,   -125.0,   "%",  -125, 125),
        SPN(190, "Velocidade do Motor (RPM)",          3, 0, 16, 0.125,    0.0,   "rpm",  0, 8031.875),
        SPN(91,  "Posição Pedal Acelerador",           1, 0,  8, 0.4,      0.0,   "%",    0, 100),
    ]),

    # ── 1.1.4   65253 (0xFEE5) HOURS — Engine Hours/Revolutions ───────────────
    65253: PGN(65253, "Engine Hours/Revolutions", "HOURS", transmission_rate_ms=1000, spns=[
        SPN(247, "Horas do Motor",                     0, 0, 32, 0.05,     0.0,   "h",    0, 210554060.75),
        SPN(249, "Revoluções Totais do Motor",         4, 0, 32, 1000.0,   0.0, "rev",    0, 4.21e12),
    ]),

    # ── 1.1.5   65260 (0xFEEC) VI — Vehicle Identification ────────────────────
    65260: PGN(65260, "Vehicle Identification", "VI", transmission_rate_ms=0, spns=[
        SPN(237, "VIN (Número do Chassi)",             0, 0,  8, 1.0,      0.0,   "",     0, 255),
    ]),

    # ── 1.1.6   64977 (0xFDD1) FMS — FMS Interface Identity ───────────────────
    64977: PGN(64977, "FMS Interface Identity / Capabilities", "FMS", transmission_rate_ms=10000, spns=[
        SPN(2805, "FMS Versão Diagnóstico Suportada",  0, 0,  4, 1.0,      0.0,   "",     0,   15, True),
        SPN(2806, "FMS Habilitação Diagnóstico",       0, 4,  4, 1.0,      0.0,   "",     0,   15, True),
    ]),

    # ── 1.1.7   65217 (0xFEC1) VDHR — High Res Vehicle Distance ───────────────
    65217: PGN(65217, "High Resolution Vehicle Distance", "VDHR", transmission_rate_ms=1000, spns=[
        SPN(917, "Hodômetro Total (Alta Res.)",        0, 0, 32, 0.005,    0.0,   "km",   0, 21055406.075),
        SPN(918, "Distância Viagem (Alta Res.)",       4, 0, 32, 0.005,    0.0,   "km",   0, 21055406.075),
    ]),

    # ── 1.1.8   65132 (0xFE6C) TCO1 — Tachograph ──────────────────────────────
    65132: PGN(65132, "Tachograph", "TCO1", transmission_rate_ms=50, spns=[
        SPN(1624, "Modo Operação Tacógrafo",           0, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(1622, "Estado Trabalho Motorista 1",       1, 4,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(1623, "Estado Trabalho Motorista 2",       1, 1,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(1611, "Cartão Motorista 1",                2, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1612, "Cartão Motorista 2",                2, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1613, "Sobrecarga Motorista",              3, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(1614, "Handling do Veículo",               3, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1615, "Evento do Sistema",                 3, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1616, "Performance Tacógrafo",             4, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(1617, "Movimento do Veículo",              4, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1618, "Indicador de Direção (Dir./Esq.)",  4, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1620, "Velocidade Tacógrafo",              6, 0, 16, 1.0/256,  0.0,   "km/h", 0, 250.996),
    ]),

    # ── 1.1.9   65262 (0xFEEE) ET1 — Engine Temperature 1 ─────────────────────
    65262: PGN(65262, "Engine Temperature 1", "ET1", transmission_rate_ms=1000, spns=[
        SPN(110,  "Temp. Líquido de Arrefecimento",    0, 0,  8, 1.0,    -40.0,   "°C", -40,  210),
        SPN(174,  "Temp. Combustível",                 1, 0,  8, 1.0,    -40.0,   "°C", -40,  210),
        SPN(175,  "Temp. Óleo do Motor",               2, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(176,  "Temp. Óleo Turbo",                  4, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(52,   "Temp. Intercooler",                 6, 0,  8, 1.0,    -40.0,   "°C", -40,  210),
        SPN(1134, "Termostato Intercooler (%)",        7, 0,  8, 0.4,      0.0,   "%",    0,  100),
    ]),

    # ── 1.1.10  65269 (0xFEF5) AMB — Ambient Conditions ───────────────────────
    65269: PGN(65269, "Ambient Conditions", "AMB", transmission_rate_ms=1000, spns=[
        SPN(108, "Pressão Barométrica",                0, 0,  8, 0.5,      0.0,   "kPa",  0,  125),
        SPN(170, "Temp. Interior da Cabine",           1, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(171, "Temp. Ar Ambiente",                  3, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(172, "Temp. Ar Admissão",                  5, 0,  8, 1.0,    -40.0,   "°C", -40,  210),
        SPN(79,  "Temp. Superfície da Pista",          6, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
    ]),

    # ── 1.1.11  65131 (0xFE6B) DI — Driver's Identification ───────────────────
    65131: PGN(65131, "Driver's Identification", "DI", transmission_rate_ms=10000, spns=[
        SPN(1625, "ID do Motorista (texto via TP/BAM)", 0, 0,  8, 1.0,    0.0,   "",     0, 255),
    ]),

    # ── 1.1.12  65266 (0xFEF2) LFE — Fuel Economy (Liquid) ────────────────────
    65266: PGN(65266, "Fuel Economy (Liquid)", "LFE", transmission_rate_ms=100, spns=[
        SPN(183, "Taxa Consumo Combustível",           0, 0, 16, 0.05,     0.0,   "L/h",  0, 3212.75),
        SPN(184, "Economia Instantânea",               2, 0, 16, 1.0/512,  0.0,   "km/L", 0, 125.5),
        SPN(185, "Economia Média",                     4, 0, 16, 1.0/512,  0.0,   "km/L", 0, 125.5),
        SPN(51,  "Posição Throttle (Borboleta)",       6, 0,  8, 0.4,      0.0,   "%",    0, 100),
    ]),

    # ── 1.1.13  65198 (0xFEAE) AIR1 — Air Supply Pressure ─────────────────────
    65198: PGN(65198, "Air Supply Pressure", "AIR1", transmission_rate_ms=1000, spns=[
        SPN(1086, "Pressão Pneumática (Alimentação)",  0, 0,  8, 4.0,      0.0,   "kPa",  0, 1000),
        SPN(1087, "Pressão Freio Circuito 1",          1, 0,  8, 8.0,      0.0,   "kPa",  0, 2000),
        SPN(1088, "Pressão Freio Circuito 2",          2, 0,  8, 8.0,      0.0,   "kPa",  0, 2000),
        SPN(1089, "Pressão Freio Estac./Reboque",      3, 0,  8, 8.0,      0.0,   "kPa",  0, 2000),
        SPN(1090, "Pressão Equipamento Auxiliar",      4, 0,  8, 8.0,      0.0,   "kPa",  0, 2000),
        SPN(1091, "Pressão Suspensão a Ar",            5, 0,  8, 8.0,      0.0,   "kPa",  0, 2000),
    ]),

    # ── 1.1.14  64777 (0xFD09) HRLFC — High Res Fuel Consumption Liquid ───────
    64777: PGN(64777, "High Resolution Fuel Consumption (Liquid)", "HRLFC", transmission_rate_ms=1000, spns=[
        SPN(5054, "Combustível Total (Alta Res.)",     0, 0, 32, 0.001,    0.0,   "L",    0, 4211081.215),
        SPN(5055, "Combustível Viagem (Alta Res.)",    4, 0, 32, 0.001,    0.0,   "L",    0, 4211081.215),
    ]),

    # ── 1.1.15  65110 (0xFE56) AT1T1I — DEF Tank 1 Information ────────────────
    65110: PGN(65110, "Aftertreatment 1 DEF Tank Info", "AT1T1I", transmission_rate_ms=1000, spns=[
        SPN(1761, "Volume Tanque ARLA/DEF (%)",        0, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(3514, "Indicador Nível Baixo ARLA",        1, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(3515, "Estado Sistema ARLA",               1, 4,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(3031, "Temp. Tanque ARLA",                 2, 0,  8, 1.0,    -40.0,   "°C", -40, 210),
    ]),

    # ── 1.1.16  64893 (0xFD7D) FMS1 — FMS Tell Tale Status ────────────────────
    # Estrutura: byte 0 nibble alto = Block ID, restante = 15 status (3 bits cada)
    # Block 0..4 contém 75 indicadores diferentes — ver TELL_TALE_TABLE acima
    64893: PGN(64893, "FMS Tell Tale Status", "FMS1", transmission_rate_ms=1000, spns=[
        SPN(13000, "Tell Tale Block ID",               0, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(13001, "Tell Tale Status 1",               0, 4,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13002, "Tell Tale Status 2",               1, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13003, "Tell Tale Status 3",               1, 3,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13004, "Tell Tale Status 4",               2, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13005, "Tell Tale Status 5",               2, 3,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13006, "Tell Tale Status 6",               3, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13007, "Tell Tale Status 7",               3, 3,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13008, "Tell Tale Status 8",               4, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13009, "Tell Tale Status 9",               4, 3,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13010, "Tell Tale Status 10",              5, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13011, "Tell Tale Status 11",              5, 3,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13012, "Tell Tale Status 12",              6, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13013, "Tell Tale Status 13",              6, 3,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13014, "Tell Tale Status 14",              7, 0,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(13015, "Tell Tale Status 15",              7, 3,  3, 1.0,      0.0,   "",     0,   7, True),
    ]),

    # ── 1.1.17  61441 (0xF001) EBC1 — Electronic Brake Controller 1 ───────────
    61441: PGN(61441, "Electronic Brake Controller 1", "EBC1", transmission_rate_ms=100, spns=[
        SPN(561, "ABS Ativo",                          0, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(563, "Sinal ABS Off-Road",                 1, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3033, "Demanda Frenagem do Motorista",     2, 0,  8, 1.0,   -125.0,   "%",  -125, 125),
        SPN(1121, "Pressão Ar Circuito 1",             3, 0, 16, 8.0/1000, 0.0,   "kPa",  0, 1000),
        SPN(1122, "Pressão Ar Circuito 2",             5, 0, 16, 8.0/1000, 0.0,   "kPa",  0, 1000),
    ]),

    # ── 1.1.18  64962 (0xFDC2) EEC14 — Engine Controller 14 ───────────────────
    64962: PGN(64962, "Electronic Engine Controller 14", "EEC14", transmission_rate_ms=10000, spns=[
        SPN(5837, "Tipo Combustível Motor",            0, 0,  8, 1.0,      0.0,   "",     0, 255),
    ]),

    # ── 1.1.19  65199 (0xFEAF) GFC — Fuel Consumption (Gaseous) ───────────────
    65199: PGN(65199, "Fuel Consumption (Gaseous)", "GFC", transmission_rate_ms=1000, spns=[
        SPN(1039, "Combustível Viagem (Gasoso)",       0, 0, 32, 0.5,      0.0,   "kg",   0, 2105540607.5),
        SPN(1040, "Combustível Total (Gasoso)",        4, 0, 32, 0.5,      0.0,   "kg",   0, 2105540607.5),
    ]),

    # ── 1.1.20  61440 (0xF000) ERC1 — Electronic Retarder Controller 1 ────────
    61440: PGN(61440, "Electronic Retarder Controller 1", "ERC1", transmission_rate_ms=100, spns=[
        SPN(1085, "Modo Retarder",                     0, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(571,  "Estado Retarder Ativo",             0, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(572,  "Indicador Habilitação Retarder",    0, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(521,  "Demanda Retarder (Motorista)",      1, 0,  8, 1.0,   -125.0,   "%",  -125, 125),
        SPN(520,  "Retarder Atual (%)",                2, 0,  8, 1.0,   -125.0,   "%",  -125, 125),
        SPN(1716, "Fonte Seleção Retarder",            3, 0,  8, 1.0,      0.0,   "",     0, 255),
        SPN(900,  "Posição Seleção Retarder",          5, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(556,  "Demanda Externa Retarder",          6, 6,  2, 1.0,      0.0,   "",     0,   3, True),
    ]),

    # ── 1.1.21  65258 (0xFEEA) VW — Vehicle Weight ────────────────────────────
    65258: PGN(65258, "Vehicle Weight", "VW", transmission_rate_ms=1000, spns=[
        SPN(927, "Localização do Eixo (peso)",         0, 0,  8, 1.0,      0.0,   "",     0, 255),
        SPN(928, "Peso do Eixo (kg)",                  1, 0, 16, 0.5,      0.0,   "kg",   0, 32127.5),
        SPN(581, "Peso Total Veículo (Pneus)",         3, 0, 16, 2.0,      0.0,   "kg",   0, 128510),
    ]),

    # ── 1.1.22  64184 (0xFAB8) EVSE1DCS1 — EV Charging DC Status 1 ────────────
    64184: PGN(64184, "EVSE 1 DC Status 1", "EVSE1DCS1", transmission_rate_ms=1000, spns=[
        SPN(13171, "Tensão DC EVSE",                   0, 0, 16, 0.05,     0.0,   "V",    0, 3212.75),
        SPN(13172, "Corrente DC EVSE",                 2, 0, 16, 0.05,  -1600.0,  "A", -1600, 1612.75),
        SPN(13173, "Potência DC EVSE",                 4, 0, 16, 0.05,     0.0,   "kW",   0, 3212.75),
    ]),

    # ── 1.1.23  64617 (0xFC69) EVOI1 — EV Operator Indicators 1 ───────────────
    64617: PGN(64617, "EV Operator Indicators 1", "EVOI1", transmission_rate_ms=1000, spns=[
        SPN(15268, "Indicador Status de Carga",        0, 0,  4, 1.0,      0.0,   "",     0,  15, True),
    ]),

    # ── 1.1.24  65268 (0xFEF4) TIRE1 — Tire Condition Message 1 ───────────────
    65268: PGN(65268, "Tire Condition Message 1", "TIRE1", transmission_rate_ms=10000, spns=[
        SPN(929,  "Estado Pneu",                       2, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(587,  "Pressão do Pneu",                   3, 0,  8, 4.0,      0.0,   "kPa",  0, 1000),
        SPN(1696, "Temperatura do Pneu",               4, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(964,  "Indicador Detecção Pressão",        6, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(965,  "Limite Extremo Pressão Pneu",       6, 2,  2, 1.0,      0.0,   "",     0,   3, True),
    ]),

    # ── 1.1.25  65279 (0xFEFF) OI — Operator Indicators ───────────────────────
    65279: PGN(65279, "Operator Indicators", "OI", transmission_rate_ms=1000, spns=[
        SPN(97, "Indicador Água no Combustível",       0, 0,  2, 1.0,      0.0,   "",     0,   3, True),
    ]),

    # ════════════════════════════════════════════════════════════════════════
    # 1.2  PARÂMETROS APENAS TRUCK
    # ════════════════════════════════════════════════════════════════════════

    # ── 1.2.1   65265 (0xFEF1) CCVS1 — Cruise Control/Vehicle Speed ───────────
    65265: PGN(65265, "Cruise Control/Vehicle Speed 1", "CCVS1", transmission_rate_ms=100, spns=[
        SPN(69,   "Chave Eixo Duas Velocidades",       0, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(70,   "Freio de Estacionamento",           0, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1633, "Velocidade Cruzeiro Pausada",       0, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(84,   "Velocidade do Veículo",             1, 0, 16, 1.0/256,  0.0,   "km/h", 0, 250.996),
        SPN(595,  "Cruzeiro Ativo",                    3, 7,  1, 1.0,      0.0,   "",     0,   1, True),
        SPN(596,  "Cruzeiro Habilitado",               3, 6,  1, 1.0,      0.0,   "",     0,   1, True),
        SPN(597,  "Pedal Freio (Chave)",               3, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(598,  "Embreagem (Chave)",                 3, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(599,  "Cruzeiro: SET (Chave)",             3, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(600,  "Cruzeiro: COAST (Chave)",           4, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(601,  "Cruzeiro: RESUME (Chave)",          4, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(602,  "Cruzeiro: ACCELERATE (Chave)",      4, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(86,   "Velocidade Cruzeiro Ajustada",      5, 0,  8, 1.0,      0.0,   "km/h", 0, 250),
        SPN(976,  "Estado Cruzeiro (Bitmap)",          6, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(1237, "Posição Marcha Lenta",              6, 5,  3, 1.0,      0.0,   "",     0,   7, True),
        SPN(1480, "Fonte de Velocidade",               7, 0,  8, 1.0,      0.0,   "",     0, 255),
    ]),

    # ── 1.2.2   61443 (0xF003) EEC2 — Electronic Engine Controller 2 ──────────
    61443: PGN(61443, "Electronic Engine Controller 2", "EEC2", transmission_rate_ms=50, spns=[
        SPN(974, "Chave Pedal Acel. 1 (Baixo)",        0, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(558, "Chave Pedal Acel. 1 (Idle)",         0, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(559, "Chave Pedal Acel. 1 (Kickdown)",     0, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(91,  "Posição Pedal Acelerador 1",         1, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(92,  "Carga do Motor (% Real)",            2, 0,  8, 1.0,      0.0,   "%",    0, 250),
        SPN(29,  "Posição Pedal Acelerador 2",         3, 0,  8, 0.4,      0.0,   "%",    0, 100),
    ]),

    # ── 1.2.3   65216 (0xFEC0) SERV — Service Information ─────────────────────
    65216: PGN(65216, "Service Information", "SERV", transmission_rate_ms=1000, spns=[
        SPN(914, "Componente Próxima Revisão",         0, 0,  8, 1.0,      0.0,   "",     0, 255),
        SPN(911, "Distância p/ Revisão",               1, 0, 16, 5.0, -160635.0,  "km", -160635, 161725),
        SPN(912, "Atraso Revisão (Tempo)",             3, 0,  8, 1.0,   -125.0,   "h",  -125, 125),
    ]),

    # ── 1.2.4   64932 (0xFDA4) PTODE — PTO Drive Engagement ───────────────────
    64932: PGN(64932, "PTO Drive Engagement", "PTODE", transmission_rate_ms=100, spns=[
        SPN(3948, "Estado PTO Drive 1",                0, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3949, "Estado PTO Drive 2",                1, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3950, "Estado PTO Drive 3",                2, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3951, "Estado PTO Drive 4",                3, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3952, "Estado PTO Drive 5",                4, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3953, "Estado PTO Drive 6",                5, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3954, "Estado PTO Drive 7",                6, 0,  5, 1.0,      0.0,   "",     0,  31, True),
        SPN(3955, "Estado PTO Drive 8",                7, 0,  5, 1.0,      0.0,   "",     0,  31, True),
    ]),

    # ── 1.2.5   65136 (0xFE70) CVW — Combination Vehicle Weight ───────────────
    65136: PGN(65136, "Combination Vehicle Weight", "CVW", transmission_rate_ms=0, spns=[
        SPN(1760, "Peso Combinado do Veículo",         0, 0, 16, 10.0,     0.0,   "kg",   0, 655350),
    ]),

    # ════════════════════════════════════════════════════════════════════════
    # 1.3  PARÂMETROS APENAS BUS  (PGNs adicionais não cobertos acima)
    # ════════════════════════════════════════════════════════════════════════

    # ── 1.3.3   65102 (0xFE4E) DC1 — Door Control 1 ───────────────────────────
    65102: PGN(65102, "Door Control 1", "DC1", transmission_rate_ms=100, spns=[
        SPN(3411, "Status 2 Portas (Habilit. Global)", 0, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1820, "Rampa / Elevador Cadeirante",       0, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(1821, "Posição das Portas",                0, 4,  4, 1.0,      0.0,   "",     0,  15, True),
    ]),

    # ── 1.3.4   64933 (0xFDA5) DC2 — Door Control 2 ───────────────────────────
    # 10 portas × 3 status (Open/Enable/Lock) — todos 2 bits
    64933: PGN(64933, "Door Control 2", "DC2", transmission_rate_ms=100, spns=[
        SPN(3413, "Porta 1 — Aberta",                  0, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3414, "Porta 1 — Habilitada",              0, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3415, "Porta 2 — Travada",                 0, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3417, "Porta 2 — Habilitada",              1, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3418, "Porta 3 — Travada",                 1, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3419, "Porta 3 — Aberta",                  1, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3421, "Porta 4 — Travada",                 2, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3422, "Porta 4 — Aberta",                  2, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3423, "Porta 4 — Habilitada",              2, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3425, "Porta 5 — Aberta",                  3, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3426, "Porta 5 — Habilitada",              3, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3427, "Porta 6 — Travada",                 3, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3429, "Porta 6 — Habilitada",              4, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3430, "Porta 7 — Travada",                 4, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3431, "Porta 7 — Aberta",                  4, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3434, "Porta 8 — Aberta",                  5, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3435, "Porta 8 — Habilitada",              5, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3438, "Porta 9 — Habilitada",              5, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3439, "Porta 10 — Travada",                6, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3440, "Porta 10 — Aberta",                 6, 2,  2, 1.0,      0.0,   "",     0,   3, True),
    ]),

    # ── 1.3.5   65254 (0xFEE6) TD — Time/Date ─────────────────────────────────
    65254: PGN(65254, "Time/Date", "TD", transmission_rate_ms=1000, spns=[
        SPN(959, "Segundos",                           0, 0,  8, 0.25,     0.0,   "s",    0, 62.5),
        SPN(960, "Minutos",                            1, 0,  8, 1.0,      0.0,   "min",  0, 250),
        SPN(961, "Horas",                              2, 0,  8, 1.0,      0.0,   "h",    0, 250),
        SPN(963, "Mês",                                3, 0,  8, 1.0,      0.0,   "",     1,  13),
        SPN(962, "Dia",                                4, 0,  8, 0.25,     0.0,   "",     0, 62.75),
        SPN(964, "Ano",                                5, 0,  8, 1.0,   1985.0,   "",  1985, 2235),
    ]),

    # ── 1.3.6   65237 (0xFED5) AS — Alternator Speed/Status ───────────────────
    65237: PGN(65237, "Alternator Speed/Status", "AS", transmission_rate_ms=1000, spns=[
        SPN(3353, "Alternador 1 — Status",             0, 0,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3354, "Alternador 2 — Status",             0, 2,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3355, "Alternador 3 — Status",             0, 4,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(3356, "Alternador 4 — Status",             0, 6,  2, 1.0,      0.0,   "",     0,   3, True),
        SPN(167,  "Rotação Alternador 1",              1, 0, 16, 0.5,      0.0,   "rpm",  0, 32127.5),
        SPN(2434, "Rotação Alternador 2",              3, 0, 16, 0.5,      0.0,   "rpm",  0, 32127.5),
        SPN(2435, "Rotação Alternador 3",              5, 0, 16, 0.5,      0.0,   "rpm",  0, 32127.5),
    ]),

    # ── 1.3.7   61445 (0xF005) ETC2 — Electronic Transmission Controller 2 ────
    61445: PGN(61445, "Electronic Transmission Controller 2", "ETC2", transmission_rate_ms=100, spns=[
        SPN(524, "Marcha Selecionada",                 0, 0,  8, 1.0,   -125.0,   "",   -125, 125),
        SPN(525, "Marcha Atual (Componente)",          1, 0, 16, 0.001,    0.0,   "",     0, 64.255),
        SPN(523, "Marcha Atual",                       3, 0,  8, 1.0,   -125.0,   "",   -125, 125),
        SPN(162, "Indicador Eixo Transmissão",         6, 0,  8, 1.0,      0.0,   "",     0, 255),
    ]),

    # ── 1.3.8   65112 (0xFE58) ASC4 — Air Suspension Control 4 ────────────────
    65112: PGN(65112, "Air Suspension Control 4", "ASC4", transmission_rate_ms=1000, spns=[
        SPN(1817, "Altura Suspensão (Frente)",         0, 0, 16, 0.1,      0.0,   "mm",   0, 6425.5),
        SPN(1816, "Altura Suspensão (Trás)",           2, 0, 16, 0.1,      0.0,   "mm",   0, 6425.5),
        SPN(1815, "Modo Ajoelhamento (Kneeling)",      5, 0,  2, 1.0,      0.0,   "",     0,   3, True),
    ]),

    # ── 1.3.9   64695 (0xFCB7) VEP4 — Vehicle Electrical Power 4 ──────────────
    64695: PGN(64695, "Vehicle Electrical Power 4", "VEP4", transmission_rate_ms=10000, spns=[
        SPN(5464, "Carga Restante Pacote Híbrido",     0, 0, 16, 0.0025,   0.0,   "%",    0, 160.6375),
    ]),

    # ── 1.3.10  61449 (0xF009) VDC2 — Vehicle Dynamic Stability Control 2 ─────
    61449: PGN(61449, "Vehicle Dynamic Stability Control 2", "VDC2", transmission_rate_ms=100, spns=[
        SPN(1807, "Ângulo do Volante",                 0, 0, 16, 0.0009765625,  -31.374, "rad",   -31.374, 31.374),
        SPN(1808, "Taxa Ângulo do Volante",            2, 0, 16, 0.0009765625,  -31.374, "rad/s", -31.374, 31.374),
        SPN(1818, "Taxa de Guinada (Yaw Rate)",        4, 0, 16, 0.0001220703125, -3.92, "rad/s",   -3.92,  3.92),
        SPN(1819, "Aceleração Lateral",                6, 0, 16, 0.00048828125, -15.687, "m/s²",  -15.687, 15.687),
    ]),

    # ── 1.3.13  61705 (0xF109) HVESSTS1 — HV Energy Storage Thermal Status ────
    61705: PGN(61705, "HV Energy Storage Thermal Status 1", "HVESSTS1", transmission_rate_ms=100, spns=[
        SPN(8086, "Temp. Bateria HV (Mín)",            0, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(8085, "Temp. Bateria HV (Máx)",            2, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
        SPN(8088, "Temp. Bateria HV (Média)",          4, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
    ]),

    # ── 1.3.14  64606 (0xFC5E) HVESSHIST — HV Energy Storage History ──────────
    64606: PGN(64606, "HV Energy Storage History", "HVESSHIST", transmission_rate_ms=10000, spns=[
        SPN(13473, "Energia Total Descarregada (kWh)", 0, 0, 32, 0.05,     0.0,   "kWh",  0, 210554060),
        SPN(13474, "Energia Total Carregada (kWh)",    4, 0, 32, 0.05,     0.0,   "kWh",  0, 210554060),
    ]),

    # ── 1.3.15  64706 (0xFCC2) HSS1 — Hybrid System Status 1 ──────────────────
    64706: PGN(64706, "Hybrid System Status 1", "HSS1", transmission_rate_ms=100, spns=[
        SPN(7896, "Modo Sistema Híbrido",              0, 0,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(8473, "Estado da Bateria HV",              0, 4,  4, 1.0,      0.0,   "",     0,  15, True),
        SPN(8472, "Nível Carga (SOC)",                 1, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(7895, "Potência Sistema Híbrido",          2, 0, 16, 1.0,  -32000.0,  "kW", -32000, 32635),
    ]),

    # ── 1.3.16  65263 (0xFEEF) EFL — Engine Fluid Level/Pressure 1 ────────────
    65263: PGN(65263, "Engine Fluid Level/Pressure 1", "EFL_P1", transmission_rate_ms=500, spns=[
        SPN(94, "Pressão Filtro Combustível",          0, 0,  8, 4.0,      0.0,   "kPa",  0, 1000),
        SPN(22, "Pressão Óleo do Motor (Std)",         3, 0,  8, 4.0,      0.0,   "kPa",  0, 1000),
        SPN(98, "Nível Óleo do Motor",                 5, 0,  8, 0.4,      0.0,   "%",    0, 100),
    ]),

    # ── 1.3.17  65272 (0xFEF8) TRF1 — Transmission Fluids 1 ───────────────────
    65272: PGN(65272, "Transmission Fluids 1", "TRF1", transmission_rate_ms=1000, spns=[
        SPN(123, "Nível Filtro Transmissão",           0, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(124, "Nível Óleo Transmissão",             1, 0,  8, 0.4,      0.0,   "%",    0, 100),
        SPN(127, "Pressão Óleo Transmissão",           2, 0,  8, 16.0,     0.0,   "kPa",  0, 4000),
        SPN(177, "Temp. Óleo Transmissão",             3, 0, 16, 0.03125, -273.0, "°C", -273, 1734),
    ]),

    # ════════════════════════════════════════════════════════════════════════
    # PGNs adicionais (não no FMS, mas comuns em J1939)
    # ════════════════════════════════════════════════════════════════════════

    # 65226 (0xFECA) DM1 — Active DTCs
    65226: PGN(65226, "DM1 - Falhas Ativas", "DM1", transmission_rate_ms=1000, spns=[]),

    # 65248 (0xFEE0) VD — Vehicle Distance (resolução padrão)
    65248: PGN(65248, "Vehicle Distance", "VD", transmission_rate_ms=100, spns=[
        SPN(244, "Hodômetro Total",                    0, 0, 32, 0.125,    0.0,   "km",   0, 526385151.875),
        SPN(245, "Distância da Viagem",                4, 0, 32, 0.125,    0.0,   "km",   0, 526385151.875),
    ]),
}


# ───────────────────────────────────────────────────────────────────────────────

def decode_29bit_id(can_id: int) -> tuple[int, int, int]:
    """
    Decodifica um identificador CAN estendido (29 bits) no formato J1939,
    extraindo a prioridade, o PGN e o endereço de origem (Source Address).

    Retorna a tupla (priority, pgn, source_address).

    Layout do ID de 29 bits conforme SAE J1939/21 (do bit mais alto p/ o mais baixo):

        bits 28..26 (3 bits) -> Priority    : prioridade de arbitragem (0 = maior)
        bit  25     (1 bit)  -> Reserved (R) : bit reservado / Extended Data Page
        bit  24     (1 bit)  -> Data Page (DP)
        bits 23..16 (8 bits) -> PF (PDU Format)
        bits 15..8  (8 bits) -> PS (PDU Specific)
        bits  7..0  (8 bits) -> SA (Source Address) — quem enviou a mensagem

    Cada extração abaixo desloca (>>) o ID até alinhar o campo no bit 0 e aplica
    uma máscara (& 0x..) para isolar apenas a largura daquele campo.
    """
    priority = (can_id >> 26) & 0x07   # 3 bits de prioridade
    reserved = (can_id >> 25) & 0x01   # bit reservado (R / EDP)
    dp       = (can_id >> 24) & 0x01   # Data Page
    pf       = (can_id >> 16) & 0xFF   # PDU Format (8 bits)
    ps       = (can_id >> 8)  & 0xFF   # PDU Specific (8 bits)
    sa       = can_id & 0xFF           # Source Address (8 bits)

    # Montagem do PGN depende do valor de PF (regra do J1939):
    #   - PF >= 240 (0xF0): mensagem PDU2 (broadcast). O campo PS faz parte do
    #     próprio PGN (é uma "Group Extension"), então PS entra no PGN.
    #   - PF <  240        : mensagem PDU1 (destino específico). Nesse caso o PS
    #     representa o endereço de destino e NÃO faz parte do PGN; por isso o PGN
    #     é montado sem o PS (os 8 bits baixos do PGN ficam em zero).
    if pf >= 240:
        pgn = (reserved << 17) | (dp << 16) | (pf << 8) | ps
    else:
        pgn = (reserved << 17) | (dp << 16) | (pf << 8)

    return priority, pgn, sa


def is_j1939(can_id: int, is_extended: bool) -> bool:
    """
    Heurística para decidir se um quadro CAN deve ser tratado como J1939.

    J1939 só usa identificadores ESTENDIDOS (29 bits); logo, exige is_extended.
    A máscara 0x18000000 testa os bits 27 e 28 (faixa de prioridade/data page):
    se algum deles estiver setado, considera-se um ID compatível com J1939.
    """
    return is_extended and (can_id & 0x18000000) != 0


def source_address_name(sa: int) -> str:
    """
    Traduz um Source Address (SA) numérico para o nome legível do dispositivo
    de origem, conforme a tabela de endereços padronizada pela SAE J1939-71.

    O SA (8 bits) identifica QUAL unidade eletrônica (ECU) enviou a mensagem
    no barramento (ex.: motor, transmissão, freios). Endereços não mapeados
    aqui retornam apenas seu valor em hexadecimal (ex.: "0x4C").
    """
    # Dicionário SA -> nome do dispositivo (subconjunto relevante da norma).
    names = {
        0x00: "Engine #1",
        0x01: "Engine #2",
        0x03: "Transmission #1",
        0x05: "Shift Console - Primary",
        0x0B: "Brakes - System Controller",
        0x0F: "Retarder - Engine",
        0x10: "Retarder - Driveline",
        0x11: "Cruise Control",
        0x17: "Instrument Cluster #1",
        0x21: "Body Controller",
        0x27: "Cab Display #1",
        0x29: "Retarder - Exhaust",
        0x2A: "Headway Controller",
        0x2B: "On-Board Diagnostic Unit",
        0x33: "Tachograph",
        0x37: "Off Vehicle Gateway",
        0x39: "Fuel System",
        0x3D: "Body-to-Vehicle Interface",
        0x42: "Aftertreatment 1 System Gas",
        0x49: "Hybrid System Controller",
        0xF9: "Off Board Diagnostic-Service Tool #2",
        0xFB: "Off Board Diagnostic-Service Tool #1",
        0xFE: "Null",
        0xFF: "Broadcast",
    }
    # Busca o nome pelo SA; se não houver entrada, devolve o SA em hexadecimal.
    return names.get(sa, f"0x{sa:02X}")


def decode_message(can_id: int, data: bytes, is_extended: bool) -> Optional[dict]:
    """
    Decodifica uma mensagem CAN completa caso ela corresponda a um PGN J1939
    conhecido (presente no PGN_DATABASE).

    Parâmetros:
      can_id      — identificador do quadro CAN.
      data        — payload de bytes recebido (até 8 bytes).
      is_extended — True se o quadro usa ID estendido de 29 bits.

    Retorna um dicionário com o resultado da decodificação, ou None se a
    mensagem não for J1939 ou se o PGN não estiver cadastrado no banco.

    Formato do dicionário retornado:
      {
        "pgn":      <número do PGN>,
        "pgn_name": <sigla do PGN, ex. "EEC1">,
        "sa":       <source address numérico>,
        "spns":     { <nome_spn>: {"value":..., "unit":..., "spn":<número>}, ... }
      }
    """
    # 1) Descarta quadros que não são J1939 (ID padrão de 11 bits, etc.).
    if not is_j1939(can_id, is_extended):
        return None
    # 2) Extrai PGN e Source Address do ID de 29 bits (priority é descartado).
    _, pgn, sa = decode_29bit_id(can_id)
    # 3) Procura o PGN no banco de dados; se desconhecido, não há como decodificar.
    pgn_info = PGN_DATABASE.get(pgn)
    if pgn_info is None:
        return None
    # 4) Monta a estrutura base do resultado.
    decoded = {"pgn": pgn, "pgn_name": pgn_info.acronym, "sa": sa, "spns": {}}
    # 5) Decodifica cada SPN do PGN. Valores None (não disponíveis) são ignorados,
    #    entrando no resultado apenas os parâmetros efetivamente válidos.
    for spn in pgn_info.spns:
        val = spn.decode(data)
        if val is not None:
            decoded["spns"][spn.name] = {"value": val, "unit": spn.unit, "spn": spn.number}
    return decoded
