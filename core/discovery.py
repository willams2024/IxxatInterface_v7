"""Signal discovery engine — detects which CAN bytes carry a given signal.

============================================================================
MOTOR DE DESCOBERTA DE SINAIS CAN
============================================================================
Este módulo é o "cérebro" que descobre, de forma automática, em quais bytes
de quais mensagens CAN está codificado um determinado sinal do veículo
(RPM, velocidade, temperatura, estado de portas, etc).

VISÃO GERAL DO ALGORITMO (máquina de estados em 4 fases):

  1) BASELINE  — Com o veículo PARADO/em repouso, gravamos por alguns
                 segundos os valores de cada byte de cada mensagem. Isso
                 estabelece o "ruído de fundo": como cada byte se comporta
                 quando o sinal de interesse NÃO está sendo estimulado.

  2) TESTING   — O operador executa a AÇÃO pedida (ex: acelerar de 800 a
                 3500 RPM). Gravamos novamente todos os bytes durante a ação.

  3) ANALYZING — Comparamos teste × baseline. Bytes que passaram a variar
                 MUITO mais durante a ação são candidatos. Em seguida,
                 cada candidato é correlacionado (Pearson) com o padrão
                 esperado (`reference_values`) para ranquear quem mais se
                 parece com o sinal procurado. Detectamos sinais de 1 byte
                 e de 2 bytes (little/big endian), estimamos fator/offset
                 da fórmula linear e calculamos uma confiança 0..1.

  4) DONE      — Resultados prontos: lista de candidatos ordenada por
                 confiança (top 8 exibido na tela; lista completa no log).

MATEMÁTICA EMPREGADA (resumo, detalhada nas funções):
  • Correlação de Pearson — mede o quão linearmente uma série de byte
    acompanha o padrão esperado (-1 a +1).
  • Reamostragem — alinha o número de amostras capturadas ao número de
    pontos do padrão de referência, para que a correlação seja calculável.
  • Detecção 1/2 bytes (LE/BE) — testa 3 interpretações por byte candidato.
  • Estimativa de fator/offset — ajuste linear que mapeia o valor cru
    (raw) para a unidade de engenharia (rpm, km/h, °C...).
  • Monotonicidade — verifica se uma série "só cresce", útil para
    contadores acumulativos (hodômetro, horímetro, combustível total).

LOG — cada teste é anexado a um arquivo por sessão/conexão, permitindo
      análise cronológica posterior (ver start_new_session / _write_debug_log).
============================================================================
"""

import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional

# Utilitários J1939: decodifica IDs estendidos de 29 bits, base de dados de
# PGNs conhecidos e função que testa se um ID é J1939 (extended).
from core.j1939 import decode_29bit_id, PGN_DATABASE, is_j1939


# ── Local do log de descoberta ──────────────────────────────────────────────
# Pasta onde os logs de descoberta são gravados:
#   C:\Users\<usuário>\Documents\IxxatInterface\  (em Windows)
# os.path.expanduser("~") expande para a home do usuário logado.
DEBUG_LOG_DIR = os.path.join(os.path.expanduser("~"), "Documents", "IxxatInterface")

# Arquivo da sessão atual. UM arquivo por conexão. Todos os testes daquela
# conexão são anexados ao mesmo arquivo, facilitando análise cronológica.
# Variável de módulo (global): guarda o caminho do .txt da sessão em curso.
# Vale None enquanto nenhuma sessão tiver sido iniciada.
_current_session_file: Optional[str] = None


def start_new_session() -> str:
    """
    Cria um arquivo NOVO para a sessão atual (chamado ao conectar no IXXAT).
    Todos os testes seguintes serão anexados neste arquivo até nova chamada.
    Retorna o caminho completo do arquivo criado.
    """
    global _current_session_file
    # Garante que a pasta de logs exista (não falha se já existir).
    os.makedirs(DEBUG_LOG_DIR, exist_ok=True)
    # ts_iso: timestamp "seguro" para nome de arquivo (sem barras/pontos).
    ts_iso = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ts_disp: timestamp legível para humanos, escrito dentro do arquivo.
    ts_disp = datetime.now().strftime("%d/%m/%Y às %H:%M:%S")
    # Define o caminho do novo arquivo da sessão e o registra na global.
    _current_session_file = os.path.join(DEBUG_LOG_DIR, f"sessao_{ts_iso}.txt")
    try:
        # Modo 'w': cria/zera o arquivo e escreve o cabeçalho da sessão.
        with open(_current_session_file, 'w', encoding='utf-8') as f:
            f.write(f"{'='*100}\n")
            f.write(f"SESSÃO DE MAPEAMENTO IXXAT — INTERFACE v7.0\n")
            f.write(f"Iniciada em: {ts_disp}\n")
            f.write(f"{'='*100}\n\n")
            f.write("Cada bloco abaixo representa UM teste de descoberta executado.\n")
            f.write("Os candidatos marcados com '✓ TOP' apareceram na tela do usuário;\n")
            f.write("os marcados com '✗ descartado' foram analisados mas não exibidos.\n\n")
    except Exception:
        # Falha ao criar o arquivo (ex: permissão) não pode derrubar o app:
        # o log é apenas auxiliar; a descoberta continua funcionando sem ele.
        pass
    return _current_session_file


def get_session_file() -> Optional[str]:
    """Retorna o caminho do arquivo da sessão atual (ou None se nenhuma iniciada)."""
    return _current_session_file


# ── Funções auxiliares para correlação com padrão esperado ──────────────────

def _pearson_correlation(x: list, y: list) -> float:
    """Calcula coeficiente de Pearson entre duas séries (-1..1).

    O coeficiente de Pearson mede o quão LINEARMENTE duas séries se movem
    juntas:
        +1  → sobem/descem perfeitamente juntas (relação linear positiva)
         0  → não há relação linear
        -1  → quando uma sobe a outra desce (relação linear negativa)

    Fórmula:  r = cov(x, y) / (desvio_x * desvio_y)
    onde cov é a soma dos produtos dos desvios em relação à média de cada
    série, e desvio_x / desvio_y são as raízes das variâncias (não
    normalizadas — o N se cancela na divisão).

    Aqui usamos para comparar a série de um byte do CAN com o padrão
    esperado (`reference_values`): quanto mais alto |r|, mais aquele byte
    "se parece" com o sinal procurado.
    """
    # Séries de tamanhos diferentes ou curtas demais não têm correlação útil.
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    n = len(x)
    mean_x = sum(x) / n          # média da série x
    mean_y = sum(y) / n          # média da série y
    # Covariância (sem dividir por n — fator cancela no resultado final).
    cov   = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)   # variância de x (×n)
    var_y = sum((yi - mean_y) ** 2 for yi in y)   # variância de y (×n)
    # Se alguma série é constante (variância 0), a correlação é indefinida → 0.
    if var_x <= 0 or var_y <= 0:
        return 0.0
    # r = covariância / (sqrt(var_x) * sqrt(var_y)).
    return cov / (math.sqrt(var_x) * math.sqrt(var_y))


def _resample(values: list, n: int) -> list:
    """Reduz uma série temporal a N pontos via média de blocos consecutivos.

    A correlação de Pearson exige que as duas séries tenham o MESMO número
    de pontos. Durante um teste capturamos centenas de amostras de cada
    byte, mas o padrão de referência (`reference_values`) tem apenas alguns
    pontos (ex: [800, 1500, 2500, 3500, 800]). Esta função reamostra a série
    longa para que ela fique com exatamente `n` pontos, alinhada ao padrão.

    Três casos:
      • Tamanho já igual a n  → devolve cópia inalterada.
      • Série mais CURTA que n → "estica" repetindo o último valor.
      • Série mais LONGA que n → "encurta" tirando a média de blocos
        (chunks) consecutivos — preserva a forma geral da curva.
    """
    # Dados insuficientes para reamostrar de forma significativa.
    if len(values) < 2 or n < 2:
        return []
    # Já tem o tamanho desejado: devolve uma cópia.
    if len(values) == n:
        return list(values)
    if len(values) < n:
        # Estica: repete últimos valores até atingir n pontos.
        result = list(values)
        while len(result) < n:
            result.append(values[-1])
        return result
    # Encurta: média de chunks.
    # chunk = quantas amostras originais cabem, em média, em cada ponto de saída.
    chunk = len(values) / n
    result = []
    for i in range(n):
        # Fatia [start:end) das amostras originais que formam o ponto i.
        start = int(i * chunk)
        end   = int((i + 1) * chunk)
        # Garante pelo menos uma amostra por bloco (evita divisão por zero).
        if end <= start:
            end = start + 1
        # O ponto reamostrado é a média do bloco.
        result.append(sum(values[start:end]) / (end - start))
    return result


def _is_monotonic_increasing(values: list, tolerance: int = 1) -> bool:
    """True se a série só cresce (com pequena tolerância para ruído).

    Útil para detectar CONTADORES ACUMULATIVOS (hodômetro, horímetro,
    combustível total): esses valores nunca diminuem ao longo do tempo.

    Estratégia:
      • Calcula as diferenças consecutivas (diffs) da série.
      • `decreasing` = quantas vezes a série caiu além da tolerância
        (ruído de leitura). Para ser monotônica crescente, deve ser 0.
      • `increasing` = quantas vezes a série subiu além da tolerância.
        Exigimos pelo menos 3 subidas para evitar falsos positivos em
        séries quase constantes.
    """
    # Poucas amostras não permitem afirmar tendência com segurança.
    if len(values) < 5:
        return False
    # Diferenças entre amostras consecutivas (derivada discreta).
    diffs = [values[i+1] - values[i] for i in range(len(values)-1)]
    # Quedas reais (abaixo de -tolerância) e subidas reais (acima de +tolerância).
    decreasing = sum(1 for d in diffs if d < -tolerance)
    increasing = sum(1 for d in diffs if d >  tolerance)
    # Monotônica crescente: nenhuma queda real e ao menos 3 subidas reais.
    return increasing >= 3 and decreasing == 0


class Phase(Enum):
    """Fases da máquina de estados da descoberta de um único sinal.

    O fluxo normal é sequencial:
        IDLE → BASELINE → TESTING → ANALYZING → DONE
    `auto()` apenas atribui valores inteiros distintos automaticamente; o
    valor numérico em si não importa, só a identidade de cada fase.
    """
    IDLE       = auto()   # Ocioso: nenhum teste em andamento.
    BASELINE   = auto()   # Gravando o "repouso" (veículo parado).
    TESTING    = auto()   # Gravando durante a ação do operador.
    ANALYZING  = auto()   # Processando os dados (comparação + correlação).
    DONE       = auto()   # Análise concluída; resultados disponíveis.


@dataclass
class ByteStats:
    """Estatísticas em janela deslizante para UM par (can_id, byte_index).

    Para cada combinação de "mensagem CAN + posição de byte" mantemos o
    histórico recente dos valores (0..255) observados nesse byte. A partir
    desse histórico calculamos média, variância, desvio, mínimo, máximo e
    amplitude (range) — métricas que alimentam o algoritmo de descoberta.
    """
    # Histórico dos valores do byte. deque(maxlen=500) descarta
    # automaticamente o valor mais antigo quando passa de 500 amostras
    # (janela deslizante), limitando o uso de memória.
    values: deque = field(default_factory=lambda: deque(maxlen=500))

    def push(self, v: int):
        """Adiciona uma nova leitura do byte ao histórico."""
        self.values.append(v)

    @property
    def mean(self) -> float:
        """Média aritmética das leituras (0.0 se ainda não há dados)."""
        return sum(self.values) / len(self.values) if self.values else 0.0

    @property
    def variance(self) -> float:
        """Variância populacional: média dos quadrados dos desvios da média."""
        if len(self.values) < 2:
            return 0.0
        m = self.mean
        return sum((x - m) ** 2 for x in self.values) / len(self.values)

    @property
    def std(self) -> float:
        """Desvio padrão = raiz quadrada da variância (dispersão típica)."""
        return math.sqrt(self.variance)

    @property
    def min_val(self) -> float:
        """Menor valor já observado neste byte."""
        return min(self.values) if self.values else 0.0

    @property
    def max_val(self) -> float:
        """Maior valor já observado neste byte."""
        return max(self.values) if self.values else 0.0

    @property
    def range(self) -> float:
        """Amplitude = máximo - mínimo. Quanto o byte "se mexeu" no total."""
        return self.max_val - self.min_val


@dataclass
class CandidateSignal:
    """Um BYTE (ou par de bytes) candidato a conter o sinal procurado.

    Cada candidato representa a hipótese "o sinal X está neste byte desta
    mensagem CAN, com esta fórmula de conversão e esta confiança". A análise
    gera vários candidatos e os ranqueia por `confidence`.
    """
    can_id: int            # Identificador da mensagem CAN onde está o byte.
    is_extended: bool      # True = ID estendido de 29 bits (J1939); False = 11 bits.
    byte_index: int        # Posição (0..7) do byte dentro do payload da mensagem.
    length_bytes: int      # Tamanho do sinal em bytes: 1 ou 2 (16 bits).
    byte_order: str        # Ordem dos bytes em sinais de 2 bytes: 'little' ou 'big'.
    baseline_mean: float   # Média do byte durante o BASELINE (repouso).
    baseline_std: float    # Desvio padrão do byte durante o BASELINE.
    test_range: float      # Amplitude (máx-mín) observada durante o TESTE.
    correlation: float     # Correlação com o padrão esperado (0..1, maior = melhor).
    formula_factor: float  # Fator multiplicativo da fórmula linear (raw → unidade).
    formula_offset: float  # Offset (deslocamento) da fórmula linear.
    formula_str: str       # Fórmula legível, ex: "valor = raw × 0.125  [rpm]".
    pgn: Optional[int] = None        # PGN J1939 da mensagem (se for J1939).
    pgn_name: Optional[str] = None   # Acrônimo/nome do PGN (se conhecido).
    spn_name: Optional[str] = None   # Nome do SPN (parâmetro) correspondente.
    confidence: float = 0.0          # Confiança final 0..1 (usada para ranquear).

    def describe(self) -> str:
        """Descrição curta e legível do candidato (para UI/depuração).

        Prefere mostrar o PGN/nome quando a mensagem é J1939 conhecida;
        caso contrário, mostra o CAN ID em hexadecimal.
        """
        if self.pgn:
            return f"PGN {self.pgn} ({self.pgn_name}) — Byte {self.byte_index}"
        return f"ID 0x{self.can_id:08X} — Byte {self.byte_index}"


@dataclass
class TestDefinition:
    """Definição (receita) de UM teste de descoberta.

    Descreve tudo que o motor precisa para conduzir e analisar a descoberta
    de um sinal específico: instruções para o operador, durações das fases,
    faixa esperada do valor em engenharia, o padrão temporal esperado para
    correlação e dicas de onde procurar (PGN/SPN J1939).
    """
    key: str                # Chave única do teste (ex: "rpm", "speed").
    name: str               # Nome exibido na interface (ex: "Rotação do Motor").
    unit: str               # Unidade de engenharia do sinal (ex: "rpm", "km/h").
    instruction: str        # Texto guiando o operador sobre a ação a executar.
    baseline_sec: float     # Duração da fase BASELINE em segundos (repouso).
    test_sec: float         # Duração da fase TESTING em segundos (ação).
    expected_min: float     # Valor mínimo esperado do sinal (em engenharia).
    expected_max: float     # Valor máximo esperado do sinal (em engenharia).
    signal_type: str        # 'analog' (contínuo) ou 'binary' (liga/desliga).
    reference_values: list  # Padrão temporal esperado dos valores de entrada;
                            # usado na correlação de Pearson para ranquear bytes.
    pgn_hints: list         # PGNs J1939 conhecidos a verificar PRIMEIRO (atalho).
    spn_hints: list         # Nomes de SPN conhecidos para casar com o PGN.
    sim_inject_attr: str    # Atributo do simulador (SimState) a acionar no modo
                            # de simulação; "" quando não há injeção simulada.


# ════════════════════════════════════════════════════════════════════════════
# CATÁLOGO DE TESTES DISPONÍVEIS
# ════════════════════════════════════════════════════════════════════════════
# Dicionário {chave: TestDefinition} com todos os sinais que o sistema sabe
# descobrir. Cada entrada define a "receita" daquele teste.
#
# Sobre os campos mais importantes de cada teste:
#   • reference_values — é o coração da correlação. Representa, em ordem
#     cronológica, como o valor REAL do sinal deve evoluir quando o operador
#     segue a instrução. O motor reamostra cada byte capturado para o mesmo
#     número de pontos e mede a correlação de Pearson com esta lista. Por isso
#     os valores foram escolhidos para espelhar exatamente a instrução
#     (ex: "acelerar para 1500, 2500, 3500 RPM" → [800,1500,2500,3500,800]).
#       - Padrões CRESCENTES (contadores) exigem correlação positiva.
#       - Padrões OSCILANTES (sobe e desce) aceitam correlação +/- (o byte
#         pode estar invertido em relação ao sinal real).
#       - Padrões de TOGGLE [0,1,0,1,...] modelam acionamentos liga/desliga.
#   • expected_min/max — definem a faixa de engenharia usada para estimar
#     fator/offset e para checar a "coerência" do range observado.
#   • pgn_hints/spn_hints — atalho: se a mensagem for J1939 e bater com um
#     PGN/SPN conhecido, o candidato recebe confiança altíssima direto.
# ════════════════════════════════════════════════════════════════════════════
TESTS: dict[str, TestDefinition] = {

    # ═══════════════════════════════════════════════════════════════════════
    # MOTOR / FUNCIONAMENTO
    # ═══════════════════════════════════════════════════════════════════════

    "rpm": TestDefinition(
        key="rpm", name="Rotação do Motor (RPM)", unit="rpm",
        instruction=(
            "Motor em marcha lenta.\n"
            "Acelerar progressivamente para:\n"
            "  • 1500 RPM\n"
            "  • 2500 RPM\n"
            "  • 3500 RPM\n\n"
            "Objetivo: encontrar frame com variação rápida proporcional."
        ),
        baseline_sec=4.0, test_sec=18.0,
        expected_min=0, expected_max=4000,
        signal_type="analog",
        # Padrão: marcha lenta (~800) → sobe em degraus → volta à lenta.
        # Espelha a instrução "acelerar para 1500, 2500, 3500 RPM" e o retorno
        # ao final. É oscilante, então aceita correlação positiva ou negativa.
        reference_values=[800, 1500, 2500, 3500, 800],
        # PGN 61444 (EEC1) carrega a velocidade do motor em J1939 — checado antes.
        pgn_hints=[61444], spn_hints=["Velocidade do Motor (RPM)"],
        sim_inject_attr="inject_rpm_peak",
    ),

    "speed": TestDefinition(
        key="speed", name="Velocidade do Veículo", unit="km/h",
        instruction=(
            "Rodar o veículo em velocidades diferentes:\n"
            "  • 10 km/h\n"
            "  • 20 km/h\n"
            "  • 40 km/h\n"
            "  • 60 km/h\n\n"
            "Objetivo: localizar variação dos bytes de forma crescente "
            "de acordo com as velocidades simuladas (10, 20, 40 e 60 km/h)."
        ),
        baseline_sec=4.0, test_sec=25.0,
        expected_min=0, expected_max=120,
        signal_type="analog",
        # Estritamente crescente (0→10→20→40→60), igual às velocidades pedidas.
        # Por ser monotônico, a correlação só aceita bytes que SOBEM junto.
        reference_values=[0, 10, 20, 40, 60],   # padrão estritamente crescente
        # PGN 65265 (CCVS) traz a velocidade do veículo em J1939.
        pgn_hints=[65265], spn_hints=["Velocidade do Veículo"],
        sim_inject_attr="inject_speed_ramp",
    ),

    "throttle": TestDefinition(
        key="throttle", name="Pedal do Acelerador", unit="%",
        instruction=(
            "Com o veículo parado:\n"
            "1. Pressionar o acelerador lentamente de 0% até 100%\n"
            "2. Repetir 3 vezes\n\n"
            "Objetivo: identificar variação linear proporcional."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=100,
        signal_type="analog",
        # 3 ciclos de 0→100→0 como descrito na instrução. Cada subida passa
        # por 50 (ponto intermediário) para dar forma de rampa à correlação.
        reference_values=[0, 50, 100, 50, 0, 50, 100, 50, 0, 50, 100, 50, 0],
        # PGNs 61443 (EEC2) / 61444 (EEC1) costumam conter a posição do pedal.
        pgn_hints=[61443, 61444], spn_hints=["Posição Pedal Acelerador"],
        sim_inject_attr="inject_throttle_ramp",
    ),

    "clutch": TestDefinition(
        key="clutch", name="Pedal da Embreagem", unit="",
        instruction=(
            "Pressionar e soltar o pedal da embreagem lentamente\n"
            "5 vezes.\n\n"
            "Objetivo: identificar sinal digital ou analógico do pedal."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=1,
        signal_type="binary",
        # 5 toggles: pressiona → solta × 5. Padrão liga/desliga repetido que o
        # scanner binário usa para casar bytes que alternam entre dois estados.
        reference_values=[0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
        pgn_hints=[61444, 61443], spn_hints=["Chave de Embreagem", "Embreagem"],
        sim_inject_attr="inject_clutch_toggle",
    ),

    "brake_pedal": TestDefinition(
        key="brake_pedal", name="Pedal do Freio", unit="%",
        instruction=(
            "Pressionar e soltar o pedal do freio 5 vezes.\n\n"
            "Objetivo: buscar mudança binária ou percentual."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=100,
        signal_type="binary",
        # 5 toggles pressiona/solta — mesmo padrão dos demais pedais binários.
        reference_values=[0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],   # 5 toggles
        pgn_hints=[61441, 65265], spn_hints=["Pedal Freio", "Posição Pedal Freio"],
        sim_inject_attr="",
    ),

    "parking_brake": TestDefinition(
        key="parking_brake", name="Freio de Estacionamento", unit="",
        instruction=(
            "Acionar e soltar o freio de estacionamento 3 vezes."
        ),
        baseline_sec=3.0, test_sec=12.0,
        expected_min=0, expected_max=1,
        signal_type="binary",
        # 3 toggles aciona/solta — a instrução pede acionar 3 vezes.
        reference_values=[0, 1, 0, 1, 0, 1, 0],   # 3 toggles
        pgn_hints=[65265], spn_hints=["Freio de Estacionamento"],
        sim_inject_attr="inject_brake_toggle",
    ),

    "gear": TestDefinition(
        key="gear", name="Câmbio (Marcha Atual)", unit="",
        instruction=(
            "Alternar todas as posições do câmbio:\n"
            "  • Automático: P → R → N → D\n"
            "  • Manual: todas as marchas\n\n"
            "Permanecer ~3 segundos em cada posição.\n\n"
            "Objetivo: mapear estados discretos do câmbio."
        ),
        baseline_sec=3.0, test_sec=20.0,
        expected_min=-1, expected_max=10,
        signal_type="analog",
        # Estados discretos crescentes do câmbio (P/R/N/D ou marchas 1..5) e
        # retorno ao início. Tratado como analógico por terem ordem numérica.
        reference_values=[0, 1, 2, 3, 4, 5, 0],
        # PGN 61445 (ETC2) carrega marcha atual/selecionada em J1939.
        pgn_hints=[61445], spn_hints=["Marcha Atual", "Marcha Selecionada"],
        sim_inject_attr="inject_gear_change",
    ),

    "coolant_temp": TestDefinition(
        key="coolant_temp", name="Temperatura do Arrefecimento", unit="°C",
        instruction=(
            "1. Motor frio inicialmente\n"
            "2. Acompanhar aquecimento até temperatura\n"
            "   operacional (~85°C)\n\n"
            "Objetivo: identificar valor crescente gradual."
        ),
        baseline_sec=3.0, test_sec=120.0,
        expected_min=-20, expected_max=120,
        signal_type="analog",
        # Aquecimento gradual do motor frio até a temperatura operacional (~85°C).
        # Monotônico crescente: a temperatura não cai durante o aquecimento.
        reference_values=[20, 30, 40, 50, 60, 70, 80, 85],   # subida gradual
        # PGN 65262 (ET1) traz a temperatura do líquido de arrefecimento.
        pgn_hints=[65262], spn_hints=["Temp. Líquido de Arrefecimento"],
        sim_inject_attr="",
    ),

    "oil_temp": TestDefinition(
        key="oil_temp", name="Temperatura do Óleo", unit="°C",
        instruction=(
            "1. Ligar o motor frio\n"
            "2. Rodar até aquecimento completo\n\n"
            "Objetivo: encontrar sensor térmico com subida progressiva."
        ),
        baseline_sec=3.0, test_sec=120.0,
        expected_min=-20, expected_max=150,
        signal_type="analog",
        # Subida progressiva da temperatura do óleo até aquecimento completo.
        # Monotônico crescente, como o arrefecimento, mas vai mais alto (~100°C).
        reference_values=[20, 30, 40, 50, 60, 70, 80, 90, 100],
        # PGN 65262 (ET1) também contém a temperatura do óleo do motor.
        pgn_hints=[65262], spn_hints=["Temp. Óleo do Motor"],
        sim_inject_attr="",
    ),

    "oil_pressure": TestDefinition(
        key="oil_pressure", name="Pressão do Óleo", unit="kPa",
        instruction=(
            "1. Motor desligado\n"
            "2. Ligar motor\n"
            "3. Acompanhar mudança da pressão após partida\n\n"
            "Objetivo: identificar transição do estado de pressão."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=800,
        signal_type="analog",
        # Pressão zero com motor desligado, salta ao dar partida e estabiliza.
        # O patamar final (400,400,400) modela a pressão constante de operação.
        reference_values=[0, 0, 100, 200, 300, 400, 400, 400],  # sobe ao ligar
        # PGN 65263 (EFL/P1) carrega a pressão do óleo do motor.
        pgn_hints=[65263], spn_hints=["Pressão Óleo do Motor"],
        sim_inject_attr="",
    ),

    "horimeter": TestDefinition(
        key="horimeter", name="Horímetro", unit="h",
        instruction=(
            "1. Ligar a ignição\n"
            "2. Dar partida no motor\n"
            "3. Manter o motor funcionando ~7 minutos\n"
            "4. Desligar e ligar novamente\n\n"
            "Objetivo: identificar contador incremental de tempo do motor "
            "a cada 6 minutos."
        ),
        baseline_sec=3.0, test_sec=420.0,   # 7 minutos de janela de teste
        expected_min=0, expected_max=1000000,
        signal_type="analog",
        # Contador de horas do motor: só cresce com o tempo (monotônico).
        # Os valores 0..7 representam a tendência crescente, não horas reais.
        reference_values=[0, 1, 2, 3, 4, 5, 6, 7],  # crescimento monotônico
        # PGN 65253 (HOURS) carrega horas totais de funcionamento do motor.
        pgn_hints=[65253], spn_hints=["Horas do Motor"],
        sim_inject_attr="",
    ),

    "fuel_total": TestDefinition(
        key="fuel_total", name="Consumo Combustível Acumulado", unit="L",
        instruction=(
            "1. Ligar o veículo\n"
            "2. Acelerar em rota curta ou manter RPM variando\n"
            "   entre lenta e 2500 RPM\n"
            "3. Manter o motor ligado por alguns minutos\n\n"
            "Objetivo: buscar parâmetro acumulativo crescente."
        ),
        baseline_sec=3.0, test_sec=180.0,
        expected_min=0, expected_max=100000,
        signal_type="analog",
        # Combustível acumulado: contador que só cresce (monotônico).
        reference_values=[0, 1, 2, 3, 4, 5, 6, 7, 8],   # acumulador crescente
        # PRIORIDADE: 64777 (HRLFC — Alta Resolução) > 65257 (LFC — resolução padrão)
        # A ordem importa: o PGN de alta resolução é verificado primeiro.
        pgn_hints=[64777, 65257],
        spn_hints=["Combustível Total (Alta Res.)", "Combustível Total"],
        sim_inject_attr="",
    ),

    "trip_distance": TestDefinition(
        key="trip_distance", name="Filtro de Hodômetro (Viagem)", unit="km",
        instruction=(
            "1. Rodar o veículo por aproximadamente 1 km\n"
            "2. Comparar mensagens antes e depois do deslocamento\n\n"
            "Objetivo: encontrar frames relacionados à distância percorrida."
        ),
        baseline_sec=3.0, test_sec=120.0,
        expected_min=0, expected_max=100000,
        signal_type="analog",
        # Distância de viagem: acumulador crescente (monotônico).
        reference_values=[0, 1, 2, 3, 4, 5, 6, 7, 8],   # acumulador
        # PGNs 65248 (VD) / 65217 (HRVD — alta resolução) trazem distâncias.
        pgn_hints=[65248, 65217], spn_hints=["Distância da Viagem"],
        sim_inject_attr="",
    ),

    "odometer": TestDefinition(
        key="odometer", name="Hodômetro Total", unit="km",
        instruction=(
            "1. Registrar valor inicial\n"
            "2. Rodar o veículo por alguns quilômetros\n"
            "3. Comparar incremento\n\n"
            "Objetivo: encontrar contador acumulativo de distância."
        ),
        baseline_sec=2.0, test_sec=15.0,
        expected_min=0, expected_max=9999999,
        signal_type="analog",
        # Hodômetro total: contador acumulativo de distância, sempre crescente.
        reference_values=[0, 1, 2, 3, 4, 5, 6, 7, 8],   # contador monotônico
        # Mesmos PGNs de distância (65248 VD / 65217 HRVD).
        pgn_hints=[65248, 65217], spn_hints=["Hodômetro Total"],
        sim_inject_attr="",
    ),

    # ═══════════════════════════════════════════════════════════════════════
    # INFORMAÇÕES GERAIS
    # ═══════════════════════════════════════════════════════════════════════

    "ignition": TestDefinition(
        key="ignition", name="Ignição", unit="",
        instruction=(
            "Alternar chave entre:\n"
            "  • OFF\n"
            "  • ACC\n"
            "  • ON\n"
            "  • START\n\n"
            "Objetivo: mapear estados da ignição."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=3,
        signal_type="analog",
        # Estados crescentes da chave: OFF(0)→ACC(1)→ON(2)→START(3) e retorno.
        # Sem pgn_hints/spn_hints: ignição raramente é padronizada em J1939.
        reference_values=[0, 1, 2, 3, 2, 0],
        pgn_hints=[], spn_hints=[],
        sim_inject_attr="",
    ),

    "air_conditioner": TestDefinition(
        key="air_conditioner", name="Ar Condicionado", unit="",
        instruction=(
            "1. Ligar e desligar o ar-condicionado 5 vezes\n"
            "2. Alterar velocidade da ventilação\n\n"
            "Objetivo: identificar comandos HVAC."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=1,
        signal_type="binary",
        # 5 acionamentos liga/desliga do ar-condicionado.
        reference_values=[0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],   # 5 toggles
        pgn_hints=[], spn_hints=["Ar condicionado"],
        sim_inject_attr="",
    ),

    "battery_voltage": TestDefinition(
        key="battery_voltage", name="Voltagem da Bateria", unit="V",
        instruction=(
            "1. Medir com motor desligado (~12 V)\n"
            "2. Ligar motor (~14 V)\n"
            "3. Acionar consumidores elétricos:\n"
            "   • Farol\n"
            "   • Ar-condicionado\n"
            "   • Desembaçador\n\n"
            "Objetivo: localizar leitura de tensão."
        ),
        baseline_sec=4.0, test_sec=20.0,
        expected_min=10, expected_max=16,
        signal_type="analog",
        # ~12V parado → ~14V com motor ligado (alternador carregando) → leve
        # queda ao acionar consumidores (13.5) → recuperação. Padrão oscilante.
        reference_values=[12, 14, 14, 13.5, 14],
        # PGN 64695 (DC/DC ou tensão) — checado como atalho.
        pgn_hints=[64695], spn_hints=["Tensão Bateria"],
        sim_inject_attr="",
    ),

    "fuel_level": TestDefinition(
        key="fuel_level", name="Nível do Tanque", unit="%",
        instruction=(
            "1. Comparar leitura do painel com CAN\n"
            "2. Se possível, movimentar a bóia manualmente\n\n"
            "Objetivo: encontrar valor estável proporcional."
        ),
        baseline_sec=4.0, test_sec=15.0,
        expected_min=0, expected_max=100,
        signal_type="analog",
        # Sem reference_values: o nível do tanque é quase estável durante o
        # teste, não há padrão temporal claro. A análise cai no critério de
        # variação pura (base_score) em vez de correlação.
        reference_values=[],
        # PGN 65276 (DD — Dash Display) carrega o nível de combustível.
        pgn_hints=[65276], spn_hints=["Nível Combustível"],
        sim_inject_attr="",
    ),

    "seatbelt": TestDefinition(
        key="seatbelt", name="Cinto de Segurança", unit="",
        instruction=(
            "Engatar e desengatar o cinto 5 vezes.\n\n"
            "Objetivo: mapear estado binário."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=1,
        signal_type="binary",
        # 5 engates/desengates do cinto — estado binário afivelado/solto.
        reference_values=[0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],   # 5 toggles
        pgn_hints=[], spn_hints=["Cinto"],
        sim_inject_attr="",
    ),

    "wiper": TestDefinition(
        key="wiper", name="Limpador de Para-brisa", unit="",
        instruction=(
            "Acionar em sequência:\n"
            "  • Intermitente\n"
            "  • Baixa velocidade\n"
            "  • Alta velocidade\n"
            "  • Esguicho\n\n"
            "Objetivo: encontrar diferentes estados do limpador."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=4,
        signal_type="analog",
        # Estados crescentes do limpador: desligado→intermitente→baixa→alta→
        # esguicho, e retorno. Discreto, mas com ordem numérica (analógico).
        reference_values=[0, 1, 2, 3, 4, 0],
        pgn_hints=[], spn_hints=[],
        sim_inject_attr="",
    ),

    "driver_door": TestDefinition(
        key="driver_door", name="Porta do Motorista", unit="",
        instruction=(
            "Abrir e fechar a porta 5 vezes.\n\n"
            "Objetivo: identificar sinal de status da porta."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=1,
        signal_type="binary",
        # 5 aberturas/fechamentos da porta do motorista (estado binário).
        reference_values=[0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],   # 5 toggles
        # PGNs 65102 / 64933 reportam status de portas; "Porta 1" = motorista.
        pgn_hints=[65102, 64933], spn_hints=["Porta 1"],
        sim_inject_attr="",
    ),

    "passenger_door": TestDefinition(
        key="passenger_door", name="Porta do Passageiro", unit="",
        instruction=(
            "Abrir e fechar a porta 5 vezes.\n\n"
            "Objetivo: mapear estado individual da porta."
        ),
        baseline_sec=3.0, test_sec=15.0,
        expected_min=0, expected_max=1,
        signal_type="binary",
        # 5 aberturas/fechamentos da porta do passageiro (estado binário).
        reference_values=[0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],   # 5 toggles
        # Mesmos PGNs de porta; "Porta 2" = passageiro.
        pgn_hints=[65102, 64933], spn_hints=["Porta 2"],
        sim_inject_attr="",
    ),

    "air_pressure": TestDefinition(
        key="air_pressure", name="Pressão do Ar (Pneumático)", unit="kPa",
        instruction=(
            "(Veículos pesados / sistemas pneumáticos)\n\n"
            "1. Ligar o veículo\n"
            "2. Acompanhar enchimento do sistema pneumático\n"
            "3. Acionar freios algumas vezes\n\n"
            "Objetivo: identificar pressão variável do sistema."
        ),
        baseline_sec=4.0, test_sec=60.0,
        expected_min=0, expected_max=1200,
        signal_type="analog",
        # Enchimento do reservatório pneumático: pressão sobe de 0 até o
        # patamar de operação (~1000 kPa). Monotônico crescente.
        reference_values=[0, 100, 300, 500, 700, 900, 1000],   # enchimento
        # PGN 65198 (AIR1) carrega pressões do sistema pneumático de freio.
        pgn_hints=[65198], spn_hints=["Pressão Freio"],
        sim_inject_attr="",
    ),
}


class DiscoveryEngine:
    """Orquestra o ciclo: captura do baseline → gravação do teste → análise.

    É o objeto principal. A interface alimenta as mensagens CAN via feed()
    enquanto o teste está em BASELINE ou TESTING; ao fim do tempo de teste,
    o motor analisa automaticamente e expõe os resultados em self.results.
    """

    def __init__(self):
        """Inicializa o motor no estado ocioso (IDLE), sem teste carregado."""
        self.phase = Phase.IDLE
        # Estatísticas por (can_id, byte_index) coletadas durante o BASELINE.
        # defaultdict cria um ByteStats vazio automaticamente em chaves novas.
        self._baseline_stats: dict[tuple, ByteStats] = defaultdict(ByteStats)
        # Estatísticas por (can_id, byte_index) coletadas durante o TESTING.
        self._test_stats: dict[tuple, ByteStats] = defaultdict(ByteStats)
        self._current_test: Optional[TestDefinition] = None  # teste em curso.
        self._phase_start = 0.0           # instante (time.time) de início da fase.
        self.results: list[CandidateSignal] = []   # candidatos da última análise.
        self.progress = 0.0  # 0-1        # progresso global do teste (0 a 1).
        # Última flag is_extended vista em feed(); usada por tick() para concluir
        # o teste mesmo quando o fluxo de mensagens para (ex.: replay CSV acabou).
        self._last_is_extended = True

    def start_test(self, test: TestDefinition):
        """Inicia um novo teste: zera os buffers e entra na fase BASELINE.

        A partir daqui, todas as mensagens passadas a feed() serão gravadas
        como "repouso" até esgotar baseline_sec, quando passa a TESTING.
        """
        self._current_test = test
        self._baseline_stats.clear()   # descarta dados de testes anteriores.
        self._test_stats.clear()
        self.results = []
        self.progress = 0.0
        self.phase = Phase.BASELINE
        self._phase_start = time.time()  # marca o início do baseline.

    def feed(self, can_id: int, data: bytes, is_extended: bool):
        """Alimenta o motor com uma mensagem CAN recebida.

        Chamado para cada frame que chega da interface IXXAT. Direciona os
        bytes para o buffer da fase atual (BASELINE ou TESTING), atualiza o
        progresso e faz as transições de fase quando o tempo se esgota.
        Mensagens recebidas fora de BASELINE/TESTING são ignoradas.

        Parâmetros:
            can_id      — identificador da mensagem CAN.
            data        — payload (bytes) da mensagem.
            is_extended — True se ID estendido de 29 bits (J1939).
        """
        # Só processamos durante a captura de baseline ou teste.
        if self.phase not in (Phase.BASELINE, Phase.TESTING):
            return

        # Guarda a última flag de ID estendido — tick() precisa dela para
        # rodar a análise caso o fluxo de mensagens pare antes do tempo acabar.
        self._last_is_extended = is_extended

        # feed() apenas GRAVA os dados. As transições de fase e a análise são
        # feitas por tick() (chamado pelo timer da GUI), garantindo que o teste
        # SEMPRE conclua por tempo de parede, mesmo se as mensagens pararem
        # (ex.: o replay do CSV terminou no meio do teste).
        if self.phase == Phase.BASELINE:
            for i, b in enumerate(data):
                self._baseline_stats[(can_id, i)].push(b)
        elif self.phase == Phase.TESTING:
            for i, b in enumerate(data):
                self._test_stats[(can_id, i)].push(b)

    def tick(self):
        """
        Avança as fases do teste com base no TEMPO DE PAREDE (wall clock).

        Deve ser chamado periodicamente pela GUI (timer ~10 Hz). Diferente de
        feed(), NÃO depende da chegada de mensagens — por isso o teste conclui
        mesmo que o barramento fique em silêncio ou o replay CSV termine antes
        do tempo previsto. Isso evita o travamento em que a barra fica parada
        (ex.: 59%) esperando mensagens que nunca chegam.

        Como roda só na thread da GUI (uma única thread), não há corrida com
        feed() para a transição de fase/análise.
        """
        if self.phase not in (Phase.BASELINE, Phase.TESTING):
            return
        if self._current_test is None:
            return

        now = time.time()
        elapsed = now - self._phase_start
        t = self._current_test

        if self.phase == Phase.BASELINE:
            self.progress = min(0.5, elapsed / t.baseline_sec * 0.5)
            if elapsed >= t.baseline_sec:
                self.phase = Phase.TESTING
                self._phase_start = now

        elif self.phase == Phase.TESTING:
            self.progress = 0.5 + min(0.45, elapsed / t.test_sec * 0.45)
            if elapsed >= t.test_sec:
                self.phase = Phase.ANALYZING
                self.progress = 0.95
                try:
                    self.results = self._analyze(self._last_is_extended)
                except Exception:
                    # Nunca deixa uma falha de análise travar a UI
                    self.results = []
                self.phase = Phase.DONE
                self.progress = 1.0

    def _analyze(self, is_extended: bool) -> list[CandidateSignal]:
        """Núcleo da fase ANALYZING: gera, deduplica e ranqueia candidatos.

        Passos:
          1) Verifica PGNs conhecidos (atalho J1939) — candidatos quase certos.
          2) Faz a varredura estatística completa (analógica ou binária)
             conforme o tipo do sinal.
          3) Deduplica por (can_id, byte_index), mantendo a maior confiança.
          4) Ordena tudo por confiança decrescente.
          5) Grava TODOS os candidatos no log da sessão (top + descartados).
          6) Retorna apenas os 8 melhores para exibição.
        """
        t = self._current_test
        candidates = []

        # 1) Atalho: tenta casar diretamente com PGNs/SPNs J1939 conhecidos.
        known = self._check_known_pgns(t, is_extended)
        candidates.extend(known)

        # 2) Varredura estatística conforme o tipo de sinal.
        if t.signal_type == "analog":
            candidates.extend(self._scan_analog(t, is_extended))
        else:
            candidates.extend(self._scan_binary(t, is_extended))

        # 3) Deduplica por (can_id, byte_index) mantendo a maior confiança.
        #    Como percorremos em ordem decrescente de confiança, o primeiro a
        #    ocupar cada chave já é o melhor; os demais (piores) são ignorados.
        seen = {}
        for c in sorted(candidates, key=lambda x: -x.confidence):
            key = (c.can_id, c.byte_index)
            if key not in seen:
                seen[key] = c

        # 4) Lista completa ordenada por confiança (antes de cortar o top 8).
        all_sorted = sorted(seen.values(), key=lambda x: -x.confidence)

        # 5) Salva TODOS os candidatos (incluindo descartados) no log de debug.
        try:
            self._write_debug_log(t, all_sorted, top_n=8)
        except Exception:
            # Falha no log não pode quebrar a descoberta.
            pass

        # 6) Apenas os 8 melhores vão para a tela do usuário.
        return all_sorted[:8]

    @staticmethod
    def _write_debug_log(t: 'TestDefinition',
                         all_candidates: list,
                         top_n: int = 8):
        """
        Anexa os resultados do teste ao arquivo da SESSÃO atual.

            Documents/IxxatInterface/sessao_AAAAMMDD_HHMMSS.txt

        Um arquivo por conexão — todos os testes ficam no mesmo arquivo.
        """
        # Se ainda não foi iniciada uma sessão (ex: teste antes de conectar),
        # cria uma automaticamente
        global _current_session_file
        if _current_session_file is None:
            start_new_session()

        # Timestamp legível para o cabeçalho deste bloco de teste.
        ts_disp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        header = (
            f"\n{'='*100}\n"
            f"TESTE: {t.name}  ({t.key})\n"
            f"Data:  {ts_disp}\n"
            f"Total de candidatos: {len(all_candidates)}  |  "
            f"Mostrados: {min(top_n, len(all_candidates))}  |  "
            f"Descartados: {max(0, len(all_candidates) - top_n)}\n"
            f"{'-'*100}\n"
            f"Instrução: {t.instruction.replace(chr(10), ' | ')}\n"
            f"{'-'*100}\n"
            f"{'#':<4}{'Status':<14}{'Conf':<7}{'CAN ID':<14}{'Tipo':<13}"
            f"{'Byte(s)':<10}{'Order':<7}{'Factor':<14}{'Offset':<10}"
            f"{'PGN':<8}{'PGN Nome':<10}{'Fórmula':<40}\n"
            f"{'-'*100}\n"
        )
        lines = [header]
        # Uma linha por candidato (ordem já é por confiança decrescente).
        for i, c in enumerate(all_candidates, start=1):
            # Os top_n primeiros foram exibidos ao usuário; os demais, não.
            status = "✓ TOP" if i <= top_n else "✗ descartado"
            # ID em hex: 8 dígitos para estendido (29b), 4 para padrão (11b).
            id_str = (f"0x{c.can_id:08X}" if c.is_extended
                      else f"0x{c.can_id:04X}")
            tipo   = ("J1939 (29b)" if c.is_extended
                      else "Std (11b)")
            # Representa o(s) byte(s): "3" para 1 byte, "3-4" para 2 bytes.
            byte_s = (f"{c.byte_index}"
                      f"{'-'+str(c.byte_index + c.length_bytes - 1) if c.length_bytes > 1 else ''}")
            pgn_s  = str(c.pgn) if c.pgn else "—"   # PGN ou travessão se ausente.
            pgn_n  = c.pgn_name or "—"
            # Monta a linha tabulada com larguras fixas (alinhamento de colunas).
            line = (
                f"{i:<4}{status:<14}{c.confidence:<7.2f}{id_str:<14}{tipo:<13}"
                f"{byte_s:<10}{c.byte_order:<7}{c.formula_factor:<14.6f}"
                f"{c.formula_offset:<10.2f}{pgn_s:<8}{pgn_n:<10}"
                f"{c.formula_str[:38]:<40}\n"
            )
            lines.append(line)
        try:
            # Append ('a') no arquivo da sessão: preserva blocos de testes
            # anteriores e acrescenta este ao final (histórico cronológico).
            with open(_current_session_file, 'a', encoding='utf-8') as f:
                f.writelines(lines)
        except Exception:
            # Novamente: falha de escrita do log não pode interromper o fluxo.
            pass

    def _check_known_pgns(self, t: TestDefinition, is_extended: bool) -> list[CandidateSignal]:
        """Atalho J1939: casa o teste com PGNs/SPNs conhecidos na base.

        Se a mensagem capturada for J1939 e seu PGN estiver entre os
        pgn_hints do teste, E o byte bater com o start_byte do SPN cujo nome
        casa com spn_hints, geramos um candidato de confiança altíssima (0.98)
        usando o fator/offset oficiais da base de dados — sem precisar
        descobrir nada estatisticamente.
        """
        results = []
        # Para cada PGN sugerido pelo teste...
        for pgn_num in t.pgn_hints:
            pgn_info = PGN_DATABASE.get(pgn_num)
            if not pgn_info:
                continue   # PGN não está na base local; pula.
            # ...percorre seus SPNs (parâmetros) definidos.
            for spn in pgn_info.spns:
                # Só nos interessam SPNs cujo nome contenha alguma dica esperada.
                if not any(hint in spn.name for hint in t.spn_hints):
                    continue
                # Procura, nos dados de teste, mensagens que sejam este PGN.
                for (can_id, byte_idx), stats in self._test_stats.items():
                    if not is_j1939(can_id, is_extended):
                        continue   # ignora IDs não-J1939.
                    # Decodifica o ID de 29 bits para extrair o PGN real.
                    _, pgn_decoded, sa = decode_29bit_id(can_id)
                    if pgn_decoded != pgn_num:
                        continue   # PGN da mensagem não bate com o procurado.
                    if byte_idx != spn.start_byte:
                        continue   # byte não é o byte inicial do SPN.
                    baseline = self._baseline_stats.get((can_id, byte_idx))
                    test_range = stats.range
                    # Exige variação mínima para confirmar que o sinal mexeu.
                    if test_range < 2:
                        continue

                    # Fórmula oficial do SPN (fator/offset/unidade da base).
                    formula_str = self._format_formula(spn.factor, spn.offset, spn.unit)
                    results.append(CandidateSignal(
                        can_id=can_id, is_extended=is_extended,
                        # Comprimento em bytes derivado dos bits do SPN (mín. 1).
                        byte_index=byte_idx, length_bytes=max(1, spn.length_bits // 8),
                        byte_order='little',   # J1939 é little-endian (Intel).
                        baseline_mean=baseline.mean if baseline else 0,
                        baseline_std=baseline.std if baseline else 0,
                        test_range=test_range,
                        correlation=1.0,       # match exato por definição.
                        formula_factor=spn.factor, formula_offset=spn.offset,
                        formula_str=formula_str,
                        pgn=pgn_num, pgn_name=pgn_info.acronym, spn_name=spn.name,
                        confidence=0.98,       # confiança altíssima (PGN conhecido).
                    ))
        return results

    def _scan_analog(self, t: TestDefinition, is_extended: bool) -> list[CandidateSignal]:
        """
        Scan estatístico de sinais analógicos com:

        1) Detecção automática de sinais 1-byte OU 2-byte (LE e BE).
           Para cada byte candidato testa as 3 interpretações e escolhe a
           que MAIS se parece com o padrão `reference_values` da instrução.
           Funciona mesmo quando o byte único não satura (range < 100).

        2) Correlação contra `reference_values` da TestDefinition:
           - Padrão monotônico crescente → exige correlação positiva
             (rejeita sinais decrescentes, importante p/ hodômetro, horímetro,
             fuel_total, temperaturas, etc.)
           - Padrão oscilante (RPM/Velocidade subindo e descendo) → aceita
             correlação positiva ou negativa (sinal pode estar invertido).
        """
        results = []

        # Verifica se o padrão de referência é estritamente crescente.
        # (Crescente o suficiente: todos os passos não-decrescentes e o fim
        #  maior que o início.) Isso decide se aceitamos só correlação positiva.
        ref = list(t.reference_values) if t.reference_values else []
        is_monotonic = (
            len(ref) >= 3
            and all(ref[i + 1] >= ref[i] for i in range(len(ref) - 1))
            and ref[-1] > ref[0]
        )

        # Pré-seleciona bytes com variação significativa.
        # "change" = quanto a amplitude (range) cresceu do baseline p/ o teste.
        # Só bytes que se mexeram MAIS durante a ação interessam.
        candidates: dict[tuple, tuple] = {}
        for (can_id, byte_idx), test_stats in self._test_stats.items():
            baseline = self._baseline_stats.get((can_id, byte_idx))
            if baseline is None:
                continue   # byte não visto no baseline; sem referência de repouso.
            change = test_stats.range - baseline.range
            if change < 3:           # threshold mais baixo p/ pegar 2-byte com range menor
                continue
            candidates[(can_id, byte_idx)] = (change, test_stats, baseline)

        # "used" evita reusar o byte vizinho que já foi consumido por um
        # candidato de 2 bytes (LSB+MSB não devem virar dois candidatos).
        used: set[tuple] = set()
        for (can_id, byte_idx), (change, test_stats, baseline) in candidates.items():
            if (can_id, byte_idx) in used:
                continue

            # ── Avalia 1-byte, 2-byte LE e 2-byte BE ─────────────────────────
            # Para o mesmo byte candidato, testamos 3 interpretações possíveis
            # e escolhemos a que melhor casa com o padrão esperado. Assim
            # detectamos automaticamente sinais de 8 e 16 bits, LE e BE.
            options = []   # [(length_bytes, byte_order, series, range, neighbor)]
            # Opção 1: 1-byte (8 bits) — usa só este byte.
            series_1b = list(test_stats.values)
            r1, r2 = test_stats.min_val, test_stats.max_val
            options.append((1, 'little', series_1b, r2 - r1, None))

            # Opção 2: 2-byte little-endian — este byte é o LSB, o seguinte é MSB.
            # valor = byte_idx | (byte_idx+1 << 8)
            le_series = self._combine_bytes(can_id, byte_idx, byte_idx + 1)
            if le_series:
                options.append((2, 'little', le_series,
                                max(le_series) - min(le_series), byte_idx + 1))

            # Opção 3: 2-byte big-endian — este byte é o MSB, o seguinte é LSB.
            # (Note a inversão dos índices passados a _combine_bytes.)
            be_series = self._combine_bytes(can_id, byte_idx + 1, byte_idx)
            if be_series:
                options.append((2, 'big', be_series,
                                max(be_series) - min(be_series), byte_idx + 1))

            # Escolhe a melhor opção: maior correlação com reference_values
            # (Se não houver ref, prefere a interpretação com maior range coerente.)
            # expected_range = amplitude esperada do sinal em engenharia.
            expected_range = max(1.0, t.expected_max - t.expected_min)
            best = None
            best_score = -1.0
            for (lb, order, series, rng, neighbor) in options:
                if rng < 1:
                    continue   # interpretação sem variação não serve.
                # Rejeita 2-byte cujo range é absurdamente grande p/ a faixa esperada
                # (provavelmente é ruído de bytes não relacionados, não o sinal).
                if lb == 2 and rng > expected_range * 256:
                    continue
                # Calcula correlação com referência (reamostrando p/ casar tamanhos).
                corr = 0.0
                if len(ref) >= 3 and len(series) >= len(ref):
                    resamp = _resample(series, len(ref))
                    r = _pearson_correlation(resamp, ref)
                    # Monotônico → só vale correlação positiva (rejeita invertido).
                    # Oscilante  → vale |r| (o byte pode estar invertido).
                    corr = max(0.0, r) if is_monotonic else max(0.0, abs(r))
                # Score = correlação (peso 0.7) + bônus de coerência de range (0.3).
                # ratio compara a amplitude crua com a faixa esperada de engenharia.
                ratio = rng / expected_range if expected_range else 1.0
                # range fora de [exp/4, exp*4] é menos plausível → penaliza.
                coherence = 1.0 if 0.25 <= ratio <= 4.0 else 0.5
                score = corr * 0.7 + coherence * 0.3
                # Pequeno desempate: prefere 2-byte se score for empate
                # (sinais de 16 bits costumam ser mais informativos).
                if lb == 2:
                    score += 0.02
                if score > best_score:
                    best_score = score
                    best = (lb, order, series, rng, neighbor, corr)

            if best is None:
                continue   # nenhuma interpretação válida para este byte.
            # Desempacota a melhor interpretação encontrada.
            length_bytes, byte_order, series, raw_range, neighbor, pattern_score = best
            raw_min, raw_max = min(series), max(series)

            # Marca o vizinho como usado se foi 2-byte (não vira outro candidato).
            if length_bytes == 2 and neighbor is not None:
                used.add((can_id, neighbor))

            # ── Estima a fórmula linear: valor_eng = raw * factor + offset ───
            # Mapeia a faixa observada [raw_min..raw_max] para [exp_min..exp_max].
            eng_range = t.expected_max - t.expected_min
            factor    = eng_range / raw_range if raw_range else 1.0
            # offset posiciona o mínimo cru no mínimo esperado de engenharia.
            offset    = t.expected_min - raw_min * factor
            # Arredonda para evitar dízimas infinitas (ex: 0.3937007874015748)
            # mantendo 6 dígitos significativos.
            factor    = float(f"{factor:.6g}")
            offset    = float(f"{offset:.6g}")
            # base_score: quanto o byte variou, normalizado para 0..1 (255 = byte cheio).
            base_score = min(1.0, change / 255.0)   # 0..1

            # Combina score de variação + score de padrão.
            if len(ref) >= 3:
                # Com referência: o padrão (correlação) domina (0.7) sobre a variação (0.3).
                final_score = base_score * 0.3 + pattern_score * 0.7
            else:
                final_score = base_score   # sem referência, só variação

            # Contexto J1939 (se aplicável): resolve PGN e nome para exibição.
            pgn_num, pgn_name = None, None
            if is_j1939(can_id, is_extended):
                _, pgn_num, _ = decode_29bit_id(can_id)
                pgn_info = PGN_DATABASE.get(pgn_num)
                pgn_name = pgn_info.acronym if pgn_info else f"PGN {pgn_num}"

            formula_str = self._format_formula(factor, offset, t.unit)
            results.append(CandidateSignal(
                can_id=can_id, is_extended=is_extended,
                byte_index=byte_idx, length_bytes=length_bytes,
                byte_order=byte_order,
                baseline_mean=baseline.mean, baseline_std=baseline.std,
                test_range=raw_range, correlation=pattern_score,
                formula_factor=factor, formula_offset=offset,
                formula_str=formula_str,
                pgn=pgn_num, pgn_name=pgn_name,
                # Confiança final ponderada: 2-byte recebe teto maior (0.95)
                # que 1-byte (0.85), pois interpretações de 16 bits são mais
                # específicas e menos sujeitas a coincidência.
                confidence=final_score * (0.95 if length_bytes == 2 else 0.85),
            ))
            used.add((can_id, byte_idx))   # marca este byte como consumido.
        return results

    def _combine_bytes(self, can_id: int, lsb_idx: int, msb_idx: int) -> Optional[list]:
        """Combina histórico de dois bytes em valores 16-bit sincronizados.

        Junta as séries do byte menos significativo (LSB) e do mais
        significativo (MSB) amostra a amostra, formando valores de 16 bits:
            valor = LSB | (MSB << 8)
        Trocar a ordem dos índices (quem é LSB/MSB) é o que diferencia
        little-endian de big-endian na chamada feita por _scan_analog.
        Retorna None se algum byte não existe ou há amostras insuficientes.
        """
        lsb_stats = self._test_stats.get((can_id, lsb_idx))
        msb_stats = self._test_stats.get((can_id, msb_idx))
        if not lsb_stats or not msb_stats:
            return None   # um dos bytes nunca apareceu nesta mensagem.
        lsb_vals = list(lsb_stats.values)
        msb_vals = list(msb_stats.values)
        # Usa o menor comprimento comum para alinhar as amostras par a par.
        n = min(len(lsb_vals), len(msb_vals))
        if n < 3:
            return None   # poucas amostras para correlacionar.
        # Combina cada par sincronizado em um inteiro de 16 bits.
        return [lsb_vals[i] | (msb_vals[i] << 8) for i in range(n)]

    def _scan_binary(self, t: TestDefinition, is_extended: bool) -> list[CandidateSignal]:
        """
        Scan estatístico de sinais binários.

        Se reference_values for fornecido (ex: [0,1,0,1,0,1,...] = 5 toggles),
        usa correlação para priorizar bytes que seguem esse padrão temporal.
        Caso contrário, usa só o critério de "poucos valores únicos + variação".
        """
        results = []
        ref = list(t.reference_values) if t.reference_values else []
        # Estima quantos toggles (acionamentos) foram pedidos.
        # Conta as transições no padrão e divide por 2, pois cada acionamento
        # completo gera 2 transições (0→1 ao apertar e 1→0 ao soltar).
        expected_toggles = 0
        if ref:
            for i in range(1, len(ref)):
                if ref[i] != ref[i - 1]:
                    expected_toggles += 1
            expected_toggles //= 2   # cada acionamento conta como 2 transições

        for (can_id, byte_idx), test_stats in self._test_stats.items():
            baseline = self._baseline_stats.get((can_id, byte_idx))
            if baseline is None:
                continue   # sem baseline para comparar.
            if test_stats.range < 1:
                continue   # byte não variou; não pode ser liga/desliga.
            # Sinal binário deve ter POUCOS valores distintos (ex: 0 e 255).
            unique_vals = set(test_stats.values)
            if len(unique_vals) > 4:
                continue   # variação contínua demais para ser binário.
            change = test_stats.range - (baseline.range if baseline else 0)
            if change < 1:
                continue   # não variou mais que no repouso.

            # ── Contagem de transições (bordas) ─────────────────────────────
            # Cada mudança de valor entre amostras consecutivas é uma transição.
            vals = list(test_stats.values)
            transitions = 0
            for i in range(1, len(vals)):
                if vals[i] != vals[i - 1]:
                    transitions += 1
            actual_toggles = transitions // 2   # 2 transições = 1 acionamento.

            # ── Correlação com padrão esperado ─────────────────────────────
            corr = 0.0
            if ref and len(vals) >= len(ref):
                resamp = _resample(vals, len(ref))
                # Normaliza ambos para [0..1] para casar com referência binária
                # (o byte pode valer 0/255 enquanto a ref vale 0/1).
                vmin, vmax = min(resamp), max(resamp)
                if vmax > vmin:
                    norm = [(v - vmin) / (vmax - vmin) for v in resamp]
                else:
                    norm = [0.0] * len(resamp)
                # |r|: aceita o byte mesmo se a lógica estiver invertida.
                corr = max(0.0, abs(_pearson_correlation(norm, ref)))

            # ── Score combinado ──────────────────────────────────────────────
            # 1) Correlação com padrão (peso alto se ref existe)
            # 2) Proximidade do número de toggles ao esperado
            # 3) Variação base
            score = 0.0
            if ref:
                score += corr * 0.5      # casamento de forma temporal.
                if expected_toggles > 0:
                    # toggle_match = 1.0 quando o nº de toggles bate exatamente;
                    # cai linearmente conforme a diferença aumenta.
                    toggle_match = 1.0 - min(1.0, abs(actual_toggles - expected_toggles)
                                            / max(expected_toggles, 1))
                    score += toggle_match * 0.3
                score += 0.2     # baseline para qualquer candidato com transições
            else:
                score = 0.6      # sem referência, score fixo

            # Contexto J1939 (se aplicável): resolve PGN e nome para exibição.
            pgn_num, pgn_name = None, None
            if is_j1939(can_id, is_extended):
                _, pgn_num, _ = decode_29bit_id(can_id)
                pgn_info = PGN_DATABASE.get(pgn_num)
                pgn_name = pgn_info.acronym if pgn_info else f"PGN {pgn_num}"

            results.append(CandidateSignal(
                can_id=can_id, is_extended=is_extended,
                # Sinais binários são tratados sempre como 1 byte.
                byte_index=byte_idx, length_bytes=1, byte_order='little',
                baseline_mean=baseline.mean if baseline else 0,
                baseline_std=baseline.std if baseline else 0,
                test_range=test_stats.range, correlation=corr,
                # Binário não tem conversão linear: fator 1, offset 0.
                formula_factor=1.0, formula_offset=0.0,
                formula_str=(f"Bit(s) no byte {byte_idx}: 0=Solto / 1=Acionado "
                             f"({actual_toggles} toggles detectados)"),
                pgn=pgn_num, pgn_name=pgn_name,
                confidence=min(1.0, score),   # confiança limitada a 1.0.
            ))
        return results

    @staticmethod
    def _format_formula(factor: float, offset: float, unit: str) -> str:
        """Monta a fórmula linear legível "valor = raw × fator [± offset] [un]".

        Omite o offset quando ele é zero, e escolhe o sinal (+/-) conforme o
        offset for positivo ou negativo, sempre mostrando o módulo.
        """
        def _fmt(v: float) -> str:
            # Inteiro vira "5", decimal vira no máx. 6 dígitos significativos
            # (evita exibir dízimas longas como 0.39370078...).
            if v == int(v):
                return str(int(v))
            return f"{v:.6g}"
        if offset == 0:
            # Sem offset: forma simplificada.
            return f"valor = raw × {_fmt(factor)}  [{unit}]"
        # Com offset: escolhe o sinal e exibe o valor absoluto.
        sign = "+" if offset >= 0 else "-"
        return f"valor = raw × {_fmt(factor)} {sign} {_fmt(abs(offset))}  [{unit}]"

    def remaining_seconds(self) -> float:
        """Estima quantos segundos faltam para concluir o teste atual.

        Usado pela interface para exibir uma contagem regressiva:
          • Em BASELINE: o que resta do baseline MAIS todo o tempo de teste.
          • Em TESTING:  apenas o que resta do tempo de teste.
          • Fora dessas fases (IDLE/ANALYZING/DONE): 0.
        """
        if not self._current_test:
            return 0.0
        t = self._current_test
        elapsed = time.time() - self._phase_start
        if self.phase == Phase.BASELINE:
            return (t.baseline_sec - elapsed) + t.test_sec
        elif self.phase == Phase.TESTING:
            return t.test_sec - elapsed
        return 0.0
