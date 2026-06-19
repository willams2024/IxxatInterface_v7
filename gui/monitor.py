"""Live CAN traffic monitor tab with real-time byte graph."""
# ─────────────────────────────────────────────────────────────────────────────
# ABA "MONITOR CAN" (PyQt5)
#
# Este módulo implementa a aba que mostra o tráfego CAN ao vivo. Ele é dividido
# em duas partes principais empilhadas verticalmente:
#
#   1) Painel de gráficos (em cima): 8 mini-gráficos em tempo real, um por byte
#      da mensagem CAN selecionada, desenhados manualmente com QPainter.
#   2) Tabela de mensagens (embaixo): uma linha por CAN ID único, com
#      decodificação J1939 (PGN, endereço de origem, valores de SPN), filtros,
#      navegação por setas e ordenação alfabética.
#
# PONTO CRÍTICO — THREAD SAFETY:
#   As mensagens CAN chegam por uma thread separada (a thread do barramento).
#   Widgets Qt NÃO são thread-safe e só podem ser tocados pela thread da GUI.
#   Por isso o fluxo é desacoplado em dois passos:
#     • on_message()      → roda na thread do CAN; APENAS enfileira em um deque.
#     • _refresh_table()  → roda na thread Qt (via QTimer); consome o deque em
#                           lotes e mexe nos widgets com segurança.
#   O deque é usado porque append()/popleft() são operações atômicas em CPython
#   (protegidas pelo GIL), então não precisamos de locks explícitos.
# ─────────────────────────────────────────────────────────────────────────────

import time
from collections import deque  # fila dupla; usada como buffer thread-safe e como histórico com limite

from PyQt5.QtCore import Qt, QTimer, QRect, QRectF, pyqtSlot
from PyQt5.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QPainterPath, QLinearGradient,
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QPushButton,
    QLabel, QCheckBox, QLineEdit, QHeaderView, QFrame,
)

# Estrutura de uma mensagem CAN bruta recebida do barramento (can_id, data, dlc, is_extended...)
from core.can_bus import CANMessage
# Utilitários de decodificação do protocolo J1939 (camada de aplicação sobre CAN 29-bit):
#   decode_29bit_id      → quebra um ID estendido em (prioridade, PGN, endereço de origem)
#   decode_message       → converte os bytes de dados em valores físicos (SPNs) com unidade
#   is_j1939             → verifica se um ID é uma mensagem J1939 válida
#   source_address_name  → nome legível do endereço de origem (ex: "Engine #1")
#   PGN_DATABASE         → dicionário PGN → metadados (sigla/acrônimo, SPNs...)
from core.j1939 import decode_29bit_id, decode_message, is_j1939, source_address_name, PGN_DATABASE
from gui.styles import COLORS  # paleta de cores compartilhada da aplicação


# ── Byte colours (8 bytes, 8 distinct colours) ────────────────────────────────
# Uma cor fixa por índice de byte (B0..B7). Mantém a identidade visual de cada
# byte consistente entre a tabela, os gráficos e as legendas. São strings hex
# usadas diretamente por QColor — NÃO devem ser alteradas (fazem parte do visual).
BYTE_COLORS = [
    "#6c63ff",  # 0  purple
    "#4caf50",  # 1  green
    "#ff9800",  # 2  orange
    "#f44336",  # 3  red
    "#00bcd4",  # 4  cyan
    "#e91e63",  # 5  pink
    "#ffeb3b",  # 6  yellow
    "#ff7043",  # 7  deep-orange
]


# ─────────────────────────────────────────────────────────────────────────────
class CANGraphWidget(QWidget):
    """Real-time sparkline graph of CAN byte values for one tracked CAN ID.

    Gráfico (sparkline) único que desenha as curvas dos 8 bytes de UMA mensagem
    CAN (identificada por can_id) sobrepostas no mesmo plano, com legenda lateral.
    Mantém um histórico de até BUFFER amostras por byte em deques de tamanho fixo.

    Observação: na aba atual quem é exibido é o CANGraphPanel (grade 4x2 de
    mini-gráficos). Esta classe é uma implementação alternativa de gráfico único
    com a mesma API pública (track/feed/clear).
    """

    BUFFER = 200   # samples to keep  — nº máximo de amostras mantidas por byte (largura do histórico)

    def __init__(self, parent=None):
        """Inicializa o widget: define tamanho mínimo, os 8 buffers de histórico
        e um QTimer que dispara o repaint periodicamente (~12 Hz)."""
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setMinimumHeight(160)
        self._tracked_id: int | None = None   # CAN ID atualmente rastreado (None = nada selecionado)
        self._pgn_name   = ""                 # nome/sigla da PGN, exibido no título
        self._dlc        = 0                  # nº de bytes válidos (Data Length Code) da última msg
        # Um deque por byte (8 no total); maxlen garante que o histórico nunca cresce além de BUFFER
        self._buffers    = [deque(maxlen=self.BUFFER) for _ in range(8)]
        self._visible    = [True] * 8   # per-byte visibility toggle — liga/desliga cada curva (clique na legenda)
        # Timer de repaint: o desenho NÃO acontece a cada mensagem recebida (isso
        # travaria com tráfego alto); ele acontece em intervalo fixo, desacoplado.
        self._repaint_timer = QTimer(self)
        self._repaint_timer.timeout.connect(self.update)
        self._repaint_timer.start(80)   # ~12 Hz — repinta a cada 80 ms

    # ── Public ────────────────────────────────────────────────────────────────

    def track(self, can_id: int, pgn_name: str = "", dlc: int = 8):
        """Start tracking a new CAN ID — clears history.

        Passa a rastrear um novo CAN ID. Guarda o nome da PGN e o DLC e LIMPA
        todo o histórico anterior (os buffers), pois as curvas antigas pertenciam
        a outra mensagem.
        """
        self._tracked_id = can_id
        self._pgn_name   = pgn_name
        self._dlc        = dlc
        for buf in self._buffers:
            buf.clear()
        self.update()

    def feed(self, msg: CANMessage):
        """Feed a message — only stores data if ID matches tracked ID.

        Alimenta o gráfico com uma mensagem. Ignora silenciosamente se o ID não
        for o rastreado. Caso contrário, anexa cada byte (até 8) no seu buffer;
        como os deques têm maxlen, a amostra mais antiga sai automaticamente.
        """
        if msg.can_id != self._tracked_id:
            return
        self._dlc = msg.dlc
        for i, b in enumerate(msg.data[:8]):
            self._buffers[i].append(b)

    def clear(self):
        """Para de rastrear e zera todos os buffers; força um repaint para
        mostrar o estado vazio."""
        self._tracked_id = None
        for buf in self._buffers:
            buf.clear()
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        """Desenha o gráfico inteiro com QPainter. Chamado pelo Qt sempre que o
        widget precisa ser repintado (disparado pelo QTimer ou por resize).

        Sistema de coordenadas do Qt: origem (0,0) no canto SUPERIOR-ESQUERDO,
        X cresce para a direita, Y cresce para BAIXO. Por isso, para desenhar um
        valor (0..255) onde 255 fica no topo, invertemos o Y (ver fórmula abaixo).
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)  # suaviza as linhas (anti-serrilhamento)
        W, H = self.width(), self.height()       # dimensões atuais do widget em pixels

        # ── Background ────────────────────────────────────────────────────────
        p.fillRect(0, 0, W, H, QColor("#0d0d1a"))  # fundo escuro azulado cobrindo tudo

        if self._tracked_id is None:
            # Nada selecionado: mostra uma instrução centralizada e sai cedo.
            p.setPen(QColor("#404060"))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(QRect(0, 0, W, H), Qt.AlignCenter,
                       "Clique em uma linha\ndo monitor para ver o gráfico")
            p.end()
            return

        # ── Layout margins ────────────────────────────────────────────────────
        # Margens em torno da área útil de plotagem. A área de plot fica entre:
        #   x ∈ [MX, MX+PW]   e   y ∈ [MY, MY+PH]
        MX   = 42   # left  (Y-axis labels)  — espaço à esquerda para os rótulos do eixo Y
        MY   = 28   # top   (title)          — espaço no topo para o título
        MR   = 110  # right (legend)         — espaço à direita para a legenda dos bytes
        MB   = 22   # bottom (X-axis label)  — espaço embaixo para o rótulo do eixo X
        PW   = max(W - MX - MR, 10)  # Plot Width  — largura útil do gráfico (mín. 10 px p/ não inverter)
        PH   = max(H - MY - MB, 10)  # Plot Height — altura útil do gráfico

        # ── Title ─────────────────────────────────────────────────────────────
        # ID em hex: 8 dígitos se for 29-bit (> 0x7FF, faixa estendida), senão 4 dígitos (11-bit).
        id_str = f"0x{self._tracked_id:08X}" if self._tracked_id > 0x7FF else f"0x{self._tracked_id:04X}"
        title  = f"{id_str}   {self._pgn_name}" if self._pgn_name else id_str
        p.setPen(QColor("#c0c0e0"))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(QRect(MX, 4, PW, 18), Qt.AlignLeft | Qt.AlignVCenter, title)

        # ── Y-axis grid + labels ──────────────────────────────────────────────
        # Eixo Y representa o valor de um byte: 0 (embaixo) a 255 (em cima).
        # Desenha 5 linhas horizontais de grade igualmente espaçadas, rotuladas
        # com 255, 192, 129, 66, 3 (val = 255 - i*63), de cima para baixo.
        grid_pen = QPen(QColor("#1c1c30"), 1, Qt.DotLine)  # caneta pontilhada da grade
        for i in range(5):
            val  = 255 - i * 63                 # valor do rótulo nesta linha (topo=255)
            gy   = MY + int(i * PH / 4)         # Y em pixels: i=0 no topo (MY), i=4 na base (MY+PH)
            p.setPen(grid_pen)
            p.drawLine(MX, gy, MX + PW, gy)     # linha horizontal da grade, largura toda do plot
            p.setPen(QColor("#5a5a80"))
            p.setFont(QFont("Consolas", 8))
            p.drawText(QRect(2, gy - 9, MX - 5, 18),   # rótulo numérico à esquerda do eixo
                       Qt.AlignRight | Qt.AlignVCenter, str(val))

        # ── Plot border ───────────────────────────────────────────────────────
        p.setPen(QPen(QColor("#2d2d44"), 1))
        p.drawRect(MX, MY, PW, PH)   # retângulo que delimita a área de plotagem

        # ── Lines ─────────────────────────────────────────────────────────────
        # Desenha uma curva (polyline) para cada byte visível, da amostra mais
        # antiga (esquerda) até a mais recente (direita).
        n_bytes = min(self._dlc, 8)   # só desenha os bytes realmente presentes na mensagem
        for bi in range(n_bytes):
            if not self._visible[bi]:
                continue              # byte desligado pelo usuário na legenda → pula
            buf = self._buffers[bi]
            if len(buf) < 2:
                continue              # precisa de pelo menos 2 pontos para desenhar uma linha

            color = QColor(BYTE_COLORS[bi])
            p.setPen(QPen(color, 1.5))
            p.setBrush(Qt.NoBrush)

            # Monta o caminho ponto a ponto.
            path = QPainterPath()
            n    = len(buf)
            for si, val in enumerate(buf):
                # X: amostra si mapeada na largura do plot. Usa self.BUFFER (e não n)
                #    como divisor para que o eixo X tenha escala fixa — as amostras
                #    "andam" para a direita conforme o buffer enche.
                x = MX + si * PW / self.BUFFER
                # Y: valor 0..255 mapeado na altura, INVERTIDO (255 no topo, 0 na base).
                #    Em y=MY+PH (base) val=0; em y=MY (topo) val=255.
                y = MY + PH - val * PH / 255
                if si == 0:
                    path.moveTo(x, y)   # primeiro ponto: só posiciona a "caneta"
                else:
                    path.lineTo(x, y)   # demais pontos: traça segmento de reta
            p.drawPath(path)

            # Current-value dot — marca o último valor (ponta direita da curva) com um círculo.
            last = buf[-1]
            cx   = MX + (n - 1) * PW / self.BUFFER   # X do último ponto
            cy   = MY + PH - last * PH / 255          # Y do último ponto (mesma fórmula invertida)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            p.drawEllipse(QRectF(cx - 3.5, cy - 3.5, 7, 7))  # círculo de 7 px centrado no ponto

        # ── X-axis label ──────────────────────────────────────────────────────
        # Indica quantas amostras o eixo X cobre (o histórico vai da esquerda p/ direita).
        p.setPen(QColor("#404060"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRect(MX, H - MB + 4, PW, MB - 4),
                   Qt.AlignRight | Qt.AlignVCenter,
                   f"← {self.BUFFER} amostras")

        # ── Legend (right panel) ──────────────────────────────────────────────
        # Coluna de legenda à direita do plot: um item por byte com amostra de
        # cor + valor atual (decimal e hex). Itens desligados ficam esmaecidos.
        lx = MX + PW + 8   # X de início da legenda (logo após a área de plot)
        p.setFont(QFont("Consolas", 8))
        for bi in range(n_bytes):
            ly    = MY + bi * int((PH) / max(n_bytes, 1))   # Y do item, distribuído na altura
            color = QColor(BYTE_COLORS[bi])
            buf   = self._buffers[bi]

            # Dim if hidden — se o byte está oculto, usa cor apagada na amostra
            if not self._visible[bi]:
                color = QColor("#303040")

            # Colour swatch — quadradinho colorido que identifica a curva
            p.fillRect(lx, ly + 3, 10, 9, color)

            # Byte label + current value — "B0: 123 (7Bh)" ou "B0: ---" se vazio
            p.setPen(color if self._visible[bi] else QColor("#404050"))
            if buf:
                val_dec = buf[-1]
                val_hex = f"{val_dec:02X}h"
                text    = f"B{bi}: {val_dec:3d} ({val_hex})"
            else:
                text = f"B{bi}: ---"
            p.drawText(QRect(lx + 14, ly, MR - 16, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, text)

        p.end()   # finaliza o QPainter (libera o contexto de desenho)

    # ── Mouse click → toggle byte visibility ─────────────────────────────────
    def mousePressEvent(self, event):
        """Click on the legend area to toggle a byte line on/off.

        Trata o clique do mouse. Recalcula a geometria da legenda (idêntica à do
        paintEvent) e, se o clique caiu sobre um item, inverte a visibilidade
        daquele byte e repinta. Permite mostrar/ocultar curvas individualmente.
        """
        if self._tracked_id is None:
            return
        W, H = self.width(), self.height()
        MX, MY, MR, MB = 42, 28, 110, 22   # MESMAS margens do paintEvent (precisam casar)
        PW = W - MX - MR
        PH = max(H - MY - MB, 10)
        lx = MX + PW + 8
        n  = min(self._dlc, 8)

        for bi in range(n):
            ly = MY + bi * int(PH / max(n, 1))     # Y do item bi (igual à legenda)
            rect = QRect(lx, ly, MR - 8, 16)       # retângulo clicável do item
            if rect.contains(event.pos()):         # clique caiu dentro deste item?
                self._visible[bi] = not self._visible[bi]   # alterna ligado/desligado
                self.update()
                break


# ─────────────────────────────────────────────────────────────────────────────
class ByteGraphCell(QWidget):
    """Mini sparkline isolado para UM byte específico.

    Cada célula desenha a curva de apenas um byte (definido por byte_index) da
    mensagem rastreada, com sua própria cor, cabeçalho e mini-eixo. Oito dessas
    células formam o CANGraphPanel (grade 4x2). Mantém um único deque com o
    histórico daquele byte.
    """

    BUFFER = 200   # nº de amostras mantidas no histórico (largura do eixo X)

    def __init__(self, byte_index: int, color: str, parent=None):
        """Cria a célula para o byte `byte_index`, com a cor `color`."""
        super().__init__(parent)
        self._byte_index   = byte_index        # qual byte (0..7) esta célula representa
        self._color        = QColor(color)      # cor da curva/cabeçalho/borda
        self._buffer       = deque(maxlen=self.BUFFER)   # histórico de valores deste byte
        self._tracked_id   = None               # CAN ID rastreado (None = inativo)
        self._dlc          = 0                   # nº de bytes válidos da mensagem rastreada
        self.setMinimumSize(180, 100)

    def track(self, can_id: int, dlc: int):
        """Passa a rastrear `can_id`, guarda o DLC e limpa o histórico anterior."""
        self._tracked_id = can_id
        self._dlc        = dlc
        self._buffer.clear()
        self.update()

    def feed(self, msg: CANMessage):
        """Alimenta a célula com uma mensagem. Ignora se o ID não bate; senão,
        anexa o valor do byte correspondente (se ele existir nessa mensagem)."""
        if msg.can_id != self._tracked_id:
            return
        if self._byte_index < len(msg.data):
            self._buffer.append(msg.data[self._byte_index])

    def clear(self):
        """Para de rastrear, zera o histórico e repinta (estado vazio)."""
        self._tracked_id = None
        self._buffer.clear()
        self.update()

    def paintEvent(self, _event):
        """Desenha a mini-célula: fundo, borda colorida, cabeçalho com o valor
        atual, mini-eixo Y (255/128/0) e a curva do byte.

        Mesmas convenções de coordenadas do CANGraphWidget: origem no canto
        superior-esquerdo, Y crescendo para baixo, valor 0..255 mapeado com
        inversão (255 no topo, 0 na base)."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        # Fundo
        p.fillRect(0, 0, W, H, QColor("#0d0d1a"))

        # Borda colorida do cell (versão mais escura da cor do byte)
        p.setPen(QPen(self._color.darker(200), 1))
        p.drawRect(0, 0, W - 1, H - 1)

        # Cabeçalho: B0: 123 (7Bh) — valor atual em decimal e hex (ou só "B0" se vazio)
        current = self._buffer[-1] if self._buffer else None
        if current is not None:
            title = f"B{self._byte_index}: {current:3d}  ({current:02X}h)"
        else:
            title = f"B{self._byte_index}"
        p.setPen(self._color)
        p.setFont(QFont("Consolas", 9, QFont.Bold))
        p.drawText(QRect(4, 2, W - 8, 16), Qt.AlignLeft | Qt.AlignVCenter, title)

        # Área de plot — margens menores que o gráfico grande (célula é compacta)
        MX, MY, MR, MB = 28, 20, 4, 4   # esquerda(rótulos Y), topo(cabeçalho), direita, base
        PW = max(W - MX - MR, 10)        # largura útil de plotagem
        PH = max(H - MY - MB, 10)        # altura útil de plotagem

        # Y-axis: linhas em 0, 128, 255 — três linhas de grade com rótulos.
        # i=0 → topo (255), i=1 → meio (128), i=2 → base (0); gy = MY + i*PH/2.
        p.setFont(QFont("Consolas", 7))
        for i, val in enumerate([255, 128, 0]):
            gy = MY + int(i * PH / 2)
            p.setPen(QPen(QColor("#1c1c30"), 1, Qt.DotLine))
            p.drawLine(MX, gy, MX + PW, gy)     # linha pontilhada da grade
            p.setPen(QColor("#5a5a80"))
            p.drawText(QRect(2, gy - 6, MX - 4, 12),   # rótulo numérico à esquerda
                       Qt.AlignRight | Qt.AlignVCenter, str(val))

        # Frame da área de plot
        p.setPen(QPen(QColor("#2d2d44"), 1))
        p.drawRect(MX, MY, PW, PH)

        # Linha do byte — só desenha se: há ID rastreado, o byte existe na
        # mensagem (byte_index < dlc) e há pelo menos 2 amostras para ligar.
        if (self._tracked_id is not None
                and self._byte_index < self._dlc
                and len(self._buffer) >= 2):
            p.setPen(QPen(self._color, 1.5))
            p.setBrush(Qt.NoBrush)
            path = QPainterPath()
            buf  = self._buffer
            n    = len(buf)
            for si, val in enumerate(buf):
                x = MX + si * PW / self.BUFFER       # X: amostra na escala fixa do buffer
                y = MY + PH - val * PH / 255          # Y: valor 0..255 invertido (255 no topo)
                if si == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            p.drawPath(path)
            # Ponto do último valor — círculo na ponta direita (valor mais recente)
            cx = MX + (n - 1) * PW / self.BUFFER
            cy = MY + PH - buf[-1] * PH / 255
            p.setPen(Qt.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(QRectF(cx - 3, cy - 3, 6, 6))

        elif self._tracked_id is not None and self._byte_index >= self._dlc:
            # Byte fora do range do DLC desta mensagem — a mensagem tem menos
            # bytes que o índice desta célula, então não há dado para mostrar.
            p.setPen(QColor("#404060"))
            p.setFont(QFont("Segoe UI", 8, QFont.Bold))
            p.drawText(QRect(MX, MY, PW, PH), Qt.AlignCenter, "— sem dados —")

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
class CANGraphPanel(QWidget):
    """Grade de 8 mini-gráficos (um por byte), arrumados em 4×2.

    Container que agrupa 8 ByteGraphCell em uma grade de 4 colunas × 2 linhas.
    Expõe a MESMA API pública de CANGraphWidget (track/feed/clear) e simplesmente
    repassa as chamadas para todas as células. É este painel que aparece na aba.
    """

    def __init__(self, parent=None):
        """Cria a grade 4x2 de células e um timer único que repinta as 8 de uma vez."""
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setSpacing(4)
        grid.setContentsMargins(2, 2, 2, 2)
        self._cells: list[ByteGraphCell] = []
        for i in range(8):
            cell = ByteGraphCell(i, BYTE_COLORS[i])   # uma célula por byte, com sua cor
            self._cells.append(cell)
            row, col = divmod(i, 4)   # layout 4 colunas × 2 linhas (i=0..3 linha 0, i=4..7 linha 1)
            grid.addWidget(cell, row, col)
        self._tracked_id: int | None = None
        # Timer leve de repaint para os 8 cells — um só timer no painel evita ter
        # 8 timers independentes; repinta todas as células a ~12 Hz (80 ms).
        self._repaint_timer = QTimer(self)
        self._repaint_timer.timeout.connect(self._repaint_all)
        self._repaint_timer.start(80)

    # ── API compatível com CANGraphWidget ─────────────────────────────────────

    def track(self, can_id: int, pgn_name: str = "", dlc: int = 8):
        """Rastreia um novo CAN ID em todas as células (pgn_name é aceito por
        compatibilidade de API, mas as células só usam can_id e dlc)."""
        self._tracked_id = can_id
        for cell in self._cells:
            cell.track(can_id, dlc)

    def feed(self, msg: CANMessage):
        """Repassa a mensagem a todas as células (cada uma filtra/extrai seu byte).
        Faz uma checagem rápida do ID antes para evitar trabalho desnecessário."""
        if msg.can_id != self._tracked_id:
            return
        for cell in self._cells:
            cell.feed(msg)

    def clear(self):
        """Limpa todas as células e zera o ID rastreado."""
        self._tracked_id = None
        for cell in self._cells:
            cell.clear()

    def _repaint_all(self):
        """Slot do timer: solicita repaint de cada célula."""
        for cell in self._cells:
            cell.update()


# ─────────────────────────────────────────────────────────────────────────────
class MonitorTab(QWidget):
    """Shows live decoded CAN traffic + real-time byte graph.

    Widget principal da aba "Monitor CAN". Junta o painel de gráficos (em cima)
    e a tabela de mensagens (embaixo) num splitter vertical, além da barra de
    ferramentas (pausar, limpar, ordenar, navegação, filtros) e da barra de
    estatísticas (contagem, IDs únicos, taxa).

    Modelo de threads (essencial para não travar com tráfego alto):
      • on_message()     é chamado pela THREAD DO CAN — só enfileira em _pending.
      • _refresh_table() é chamado por um QTimer na THREAD QT — consome o deque
        em lotes e atualiza os widgets. Só a thread Qt toca em widgets.
    """

    def __init__(self, parent=None):
        """Inicializa estado interno (mapeamentos, buffers), monta a UI e arma o
        timer periódico de atualização da tabela."""
        super().__init__(parent)
        self._rows:        dict[int, int]      = {}   # can_id → índice da linha na tabela (uma linha por ID)
        self._id_meta:     dict[int, tuple]    = {}   # can_id -> (pgn_name, dlc)  — metadados p/ o gráfico
        # deques com limite — thread-safe (append/popleft atômicos) e nunca estouram
        # _pending é a FILA de mensagens cruas aguardando processamento na thread Qt.
        self._pending:     deque               = deque(maxlen=20000)
        self._paused       = False   # quando True, on_message não enfileira novas mensagens
        self._filter_text  = ""      # texto do filtro (em minúsculas)
        self._total_count  = 0       # total de mensagens recebidas (mesmo pausado/filtrado)
        self._log_file     = None    # handle de arquivo de log (None = sem log)
        # Timestamps das mensagens do último ~1 s, para calcular a taxa (msg/s).
        self._rate_samples: deque              = deque(maxlen=20000)
        self._setup_ui()

        # Timer da thread Qt: dispara _refresh_table a cada 100 ms. É AQUI (e não
        # em on_message) que os widgets são tocados — garante thread safety.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_table)
        self._refresh_timer.start(100)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        """Monta toda a interface: barra de ferramentas, barra de estatísticas,
        splitter com o painel de gráficos e a tabela, e conecta os sinais."""
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── Toolbar ───────────────────────────────────────────────────────────
        toolbar = QHBoxLayout()

        # Botão Pausar (checkable): congela o enfileiramento de novas mensagens.
        self._btn_pause = QPushButton("⏸  Pausar")
        self._btn_pause.setCheckable(True)
        self._btn_pause.toggled.connect(self._on_pause)
        toolbar.addWidget(self._btn_pause)

        # Botão Limpar: zera tabela, gráfico, contadores e buffers.
        self._btn_clear = QPushButton("🗑  Limpar")
        self._btn_clear.clicked.connect(self._clear)
        toolbar.addWidget(self._btn_clear)

        # Botão Ordenar: reorganiza a tabela em ordem alfabética por PGN/Nome.
        self._btn_sort = QPushButton("🔠  Ordenar A→Z")
        self._btn_sort.setToolTip("Reorganiza a tabela alfabeticamente por PGN/Nome")
        self._btn_sort.clicked.connect(self._sort_table)
        toolbar.addWidget(self._btn_sort)

        # ── Navegação rápida entre PGNs (sem rolar a tabela) ──────────────────
        # Setas ◀ ▶ percorrem as linhas uma a uma; equivalem a Ctrl+↑ / Ctrl+↓.
        # Cada navegação seleciona a linha, atualiza o gráfico e a caixa de PGN.
        toolbar.addSpacing(8)
        self._btn_prev = QPushButton("◀")
        self._btn_prev.setToolTip("PGN anterior — atalho: Ctrl+↑")
        self._btn_prev.setMaximumWidth(36)
        self._btn_prev.clicked.connect(self._select_prev)
        toolbar.addWidget(self._btn_prev)

        self._btn_next = QPushButton("▶")
        self._btn_next.setToolTip("PGN próxima — atalho: Ctrl+↓")
        self._btn_next.setMaximumWidth(36)
        self._btn_next.clicked.connect(self._select_next)
        toolbar.addWidget(self._btn_next)

        # Label "linha atual / total" (ex: "3/27"), atualizado a cada seleção.
        self._lbl_nav = QLabel("—/—")
        self._lbl_nav.setStyleSheet(
            f"color: {COLORS['accent']}; font-weight: bold; padding: 0 6px;")
        self._lbl_nav.setMinimumWidth(60)
        toolbar.addWidget(self._lbl_nav)

        # ── Caixinha indicando qual PGN está no gráfico ───────────────────────
        self._lbl_pgn_box = QLabel("📊 —")
        self._lbl_pgn_box.setStyleSheet(f"""
            QLabel {{
                background-color: {COLORS['bg_card']};
                color: {COLORS['accent']};
                border: 1px solid {COLORS['accent']};
                border-radius: 4px;
                padding: 4px 10px;
                font-family: Consolas, monospace;
                font-weight: bold;
                font-size: 12px;
            }}
        """)
        self._lbl_pgn_box.setMinimumWidth(180)
        self._lbl_pgn_box.setToolTip("PGN / CAN ID atualmente exibido no gráfico")
        toolbar.addWidget(self._lbl_pgn_box)

        # Checkbox "Apenas J1939": esconde (não remove) linhas que não são J1939.
        # Reaplica os filtros sempre que muda — ver _apply_filters.
        self._chk_j1939  = QCheckBox("Apenas J1939")
        self._chk_j1939.stateChanged.connect(self._apply_filters)
        toolbar.addWidget(self._chk_j1939)

        # Checkbox "Decodificar J1939": liga/desliga a coluna de valores físicos
        # (SPNs). Ao alternar, reconstrói só a coluna 6 — ver _refresh_decoded_column.
        self._chk_decoded = QCheckBox("Decodificar J1939")
        self._chk_decoded.setChecked(True)
        self._chk_decoded.stateChanged.connect(self._refresh_decoded_column)
        toolbar.addWidget(self._chk_decoded)

        toolbar.addStretch()   # empurra o campo de filtro para a direita da barra

        # Campo de filtro de texto. Casa contra hex, decimal (CAN ID/PGN) e sigla
        # — ver _apply_filters. textChanged dispara o filtro a cada tecla.
        toolbar.addWidget(QLabel("Filtro:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("hex, decimal, PGN ou sigla...")
        self._filter_edit.setMaximumWidth(200)
        self._filter_edit.setToolTip(
            "Filtra por:\n"
            "  • Hex     (ex: 18F00400 ou f00)\n"
            "  • Decimal CAN ID (ex: 532)\n"
            "  • PGN     (ex: 61444)\n"
            "  • Sigla   (ex: eec1, ccvs)"
        )
        self._filter_edit.textChanged.connect(self._on_filter_text_changed)
        toolbar.addWidget(self._filter_edit)

        root.addLayout(toolbar)

        # ── Stats bar ─────────────────────────────────────────────────────────
        stats = QHBoxLayout()
        self._lbl_count = QLabel("Mensagens: 0")
        self._lbl_ids   = QLabel("IDs únicos: 0")
        self._lbl_rate  = QLabel("Taxa: 0 msg/s")
        self._lbl_graph_hint = QLabel(
            "💡 Clique em uma linha da tabela para rastrear uma PGN no gráfico")
        self._lbl_graph_hint.setStyleSheet(
            f"color: {COLORS['accent']}; font-size: 11px;")
        for lbl in (self._lbl_count, self._lbl_ids, self._lbl_rate):
            lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")
            stats.addWidget(lbl)
        stats.addStretch()
        stats.addWidget(self._lbl_graph_hint)
        root.addLayout(stats)

        # ── Splitter vertical: gráfico (cima) | tabela (baixo) ───────────────
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(5)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background: #2d2d44;
            }
            QSplitter::handle:hover {
                background: #5555aa;
            }
        """)

        # ── Graph panel (topo — largura total) ────────────────────────────────
        graph_panel = QWidget()
        graph_panel.setMinimumHeight(180)
        graph_layout = QVBoxLayout(graph_panel)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        graph_layout.setSpacing(3)

        graph_hdr = QHBoxLayout()
        graph_title = QLabel("📊  Gráfico — Bytes em Tempo Real")
        graph_title.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 11px; font-weight: bold;")
        graph_hdr.addWidget(graph_title)
        graph_hdr.addStretch()
        hint_lbl = QLabel("Cada byte em uma caixa separada • Layout 4×2")
        hint_lbl.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 10px;")
        graph_hdr.addWidget(hint_lbl)
        graph_layout.addLayout(graph_hdr)

        self._graph = CANGraphPanel()
        self._graph.setMinimumHeight(220)
        graph_layout.addWidget(self._graph, 1)

        splitter.addWidget(graph_panel)

        # ── Table (baixo) ─────────────────────────────────────────────────────
        # Tabela de 7 colunas, começando vazia (0 linhas). Cada linha = um CAN ID
        # único; chegadas repetidas do mesmo ID atualizam a linha existente.
        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels([
            "CAN ID", "Tipo", "PGN / Nome", "SA", "DLC",
            "Dados (hex)", "Valores Decodificados",
        ])
        # Modo de redimensionamento por coluna: a maioria ajusta ao conteúdo;
        # PGN/Nome e Decodificados esticam (Stretch); a coluna de hex tem largura fixa.
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.Fixed)
        self._table.setColumnWidth(5, 190)
        hdr.setSectionResizeMode(6, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)   # esconde a numeração de linhas à esquerda
        self._table.setAlternatingRowColors(True)         # listras alternadas para legibilidade
        self._table.setSelectionBehavior(QTableWidget.SelectRows)  # clicar seleciona a linha inteira
        # ── Listras escuras sutis (sem branco) ────────────────────────────────
        self._table.setStyleSheet("""
            QTableWidget {
                background-color: #16213e;
                alternate-background-color: #1c2745;
                color: #d0d0e8;
                gridline-color: #2a2a44;
                selection-background-color: #6c63ff;
                selection-color: #ffffff;
            }
            QTableWidget::item {
                padding: 4px 6px;
            }
            QHeaderView::section {
                background-color: #1a1a2e;
                color: #c0c0e0;
                padding: 6px;
                border: 1px solid #2a2a44;
                font-weight: bold;
            }
        """)
        # Ordenação nativa do Qt DESLIGADA de propósito: nós gerenciamos as linhas
        # manualmente via _rows e fazemos a ordenação na mão (ver _sort_table),
        # porque o sort automático embaralharia nosso mapeamento can_id→linha.
        self._table.setSortingEnabled(False)
        # cellClicked só dispara em clique real do usuário — mais confiável que
        # itemSelectionChanged (que dispara também em atualizações programáticas)
        self._table.cellClicked.connect(self._on_cell_clicked)

        splitter.addWidget(self._table)

        # Proporção inicial: gráfico 280px, tabela o resto
        splitter.setSizes([280, 500])
        root.addWidget(splitter, 1)

    # ── Public API ────────────────────────────────────────────────────────────

    def on_message(self, msg: CANMessage):
        """Recebe uma mensagem CAN. CHAMADO PELA THREAD DO CAN (não pela thread Qt).

        REGRA DE OURO: este método NÃO pode tocar em nenhum widget Qt (não são
        thread-safe). Ele só faz operações atômicas e baratas: incrementa o
        contador, registra o timestamp para a taxa e — se não estiver pausado —
        enfileira a mensagem em _pending (deque). O processamento pesado (criar/
        atualizar linhas, desenhar) fica todo em _refresh_table, na thread Qt.
        Esse desacoplamento é o que evita travamentos com tráfego alto: a thread
        do CAN nunca bloqueia esperando a GUI, e a GUI consome no seu próprio ritmo.
        """
        # Chamado da thread do CAN — apenas enfileira, NÃO acessa widgets Qt
        self._total_count += 1
        self._rate_samples.append(time.time())
        if not self._paused:
            self._pending.append(msg)   # append em deque é atômico (GIL) → seguro sem lock

    def start_logging(self, path: str):
        """Abre um arquivo em modo append para registrar as mensagens em texto."""
        self._log_file = open(path, 'a', encoding='utf-8')

    def stop_logging(self):
        """Fecha o arquivo de log, se houver."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    # ── Internals ─────────────────────────────────────────────────────────────

    @pyqtSlot()
    def _refresh_table(self):
        """Consome a fila _pending e atualiza a GUI. RODA NA THREAD QT (via QTimer
        a cada 100 ms). É o único lugar autorizado a tocar nos widgets.

        Faz duas coisas:
          1) Atualiza as estatísticas (mensagens, IDs únicos, taxa msg/s).
          2) Drena _pending em LOTES de no máximo MAX_PER_CYCLE mensagens por
             ciclo, alimentando o gráfico e atualizando/criando linhas na tabela.

        Por que processar em lote (e não tudo de uma vez)? Em barramentos muito
        ativos — ou ao despausar após acumular muita coisa — _pending pode ter
        milhares de mensagens. Atualizar tudo num único ciclo congelaria a UI.
        Limitando a 800 por ciclo (e repintando o resto nos ciclos seguintes), a
        interface continua responsiva mesmo sob rajadas. As mensagens que sobram
        ficam no deque e são tratadas no próximo disparo do timer.
        """
        now    = time.time()
        cutoff = now - 1.0   # janela de 1 segundo para o cálculo da taxa
        # Remove amostras antigas do início do deque (popleft é atômico/seguro)
        # Sobram apenas os timestamps do último segundo → len(rs) = msg/s.
        rs = self._rate_samples
        try:
            while rs and rs[0] < cutoff:
                rs.popleft()
        except IndexError:
            pass   # corrida benigna com a thread do CAN: se esvaziou, ignora
        self._lbl_count.setText(f"Mensagens: {self._total_count:,}")
        self._lbl_ids.setText(  f"IDs únicos: {len(self._rows)}")
        self._lbl_rate.setText( f"Taxa: {len(rs)} msg/s")

        if not self._pending:
            return   # nada na fila → sai cedo (só atualizou os contadores acima)

        # Processa em LOTE limitado para nunca travar a interface, mesmo com
        # rajadas grandes (ex: ao despausar num barramento muito ativo)
        MAX_PER_CYCLE = 800   # teto de mensagens processadas por ciclo do timer
        processed = 0
        # Desliga repaint da tabela durante o lote inteiro: muito mais rápido que
        # deixar a tabela repintar a cada item; reativa no finally garantidamente.
        self._table.setUpdatesEnabled(False)
        try:
            while self._pending and processed < MAX_PER_CYCLE:
                try:
                    msg = self._pending.popleft()   # tira da frente da fila (atômico)
                except IndexError:
                    break   # esvaziou no meio do laço (corrida benigna) → encerra o lote
                try:
                    self._graph.feed(msg)   # alimenta os 8 mini-gráficos
                except Exception:
                    pass   # nunca deixa uma msg malformada quebrar o ciclo
                try:
                    self._update_row(msg)   # cria/atualiza a linha da tabela
                except Exception:
                    pass
                processed += 1
        finally:
            self._table.setUpdatesEnabled(True)   # reativa o repaint da tabela (sempre)

    def _update_row(self, msg: CANMessage):
        """Cria ou atualiza a linha da tabela correspondente a uma mensagem.

        RODA NA THREAD QT (chamado por _refresh_table). Passos:
          1) Aplica os filtros logo no início (descarta cedo se não passar).
          2) Decodifica info J1939 (PGN, sigla, endereço de origem) ou exibe o
             CAN ID em decimal para 11-bit/proprietário.
          3) Localiza a linha existente para o can_id ou cria uma nova no fim.
          4) Preenche/atualiza as 7 colunas, dá um "flash" de destaque e grava no log.
        """
        cid  = msg.can_id
        filt = self._filter_text
        # ID em hex: 8 dígitos para 29-bit (estendido), 4 para 11-bit (padrão).
        id_str = f"0x{cid:08X}" if msg.is_extended else f"0x{cid:04X}"

        # Filtro rápido por hex do ID: se não bate, ainda pode bater no PGN/nome
        # mais abaixo — por isso só descarta aqui se o filtro NÃO casa o id_str.
        if filt and filt not in id_str.lower():
            return

        # Filtro "Apenas J1939": descarta não-J1939 quando o checkbox está marcado.
        only_j1939 = self._chk_j1939.isChecked()
        if only_j1939 and not is_j1939(cid, msg.is_extended):
            return

        # Decode J1939 info — ou mostra CAN ID em decimal para 11-bit/proprietário
        pgn_str, sa_str, pgn_name = "—", "—", ""
        if is_j1939(cid, msg.is_extended):
            _, pgn, sa = decode_29bit_id(cid)          # extrai PGN e endereço de origem (SA)
            pgn_info = PGN_DATABASE.get(pgn)            # metadados do PGN (pode não existir)
            pgn_name = pgn_info.acronym if pgn_info else ""   # sigla, ex: "EEC1"
            pgn_str  = f"{pgn} — {pgn_name}" if pgn_name else f"PGN {pgn}"
            sa_str   = f"0x{sa:02X} ({sa}) · {source_address_name(sa)}"   # SA em hex/dec + nome
        else:
            # CAN 11-bit ou 29-bit não-J1939 → mostra ID em decimal
            pgn_str  = f"ID {cid}"

        # Segundo estágio do filtro de texto: passa se casar no PGN/nome OU no hex.
        if filt and filt not in pgn_str.lower() and filt not in id_str.lower():
            return

        # Store meta for graph — guarda (sigla, dlc) p/ quando esta linha for
        # selecionada e o gráfico precisar do nome e do nº de bytes.
        self._id_meta[cid] = (pgn_name, msg.dlc)

        # ── Nova linha: sempre adiciona no fim ────────────────────────────────
        # Se é a primeira vez que vemos este can_id, cria a linha e registra o
        # mapeamento can_id→índice. Caso contrário, reaproveita a linha existente.
        if cid not in self._rows:
            new_row = self._table.rowCount()
            self._table.insertRow(new_row)
            self._rows[cid] = new_row

        row = self._rows[cid]
        ext = msg.is_extended
        # Rótulo de tipo conforme o ID: J1939, estendido genérico ou padrão 11-bit.
        type_str = "J1939 (29b)" if is_j1939(cid, ext) else ("Ext (29b)" if ext else "Std (11b)")

        # Decoded values — monta a string da coluna 6 com os SPNs decodificados.
        decoded_str = ""
        if self._chk_decoded.isChecked():
            try:
                decoded = decode_message(cid, msg.data, ext)   # converte bytes → SPNs
                if decoded and decoded["spns"]:
                    parts = []
                    for name, info in decoded["spns"].items():
                        try:
                            v, u = info['value'], info['unit']   # valor físico e unidade
                            if v is None:
                                continue
                            # Proteção contra valores Inf/NaN
                            # (v != v é True só para NaN; abs(v) > 1e15 descarta absurdos)
                            if not isinstance(v, (int, float)) or v != v or abs(v) > 1e15:
                                continue
                            if u:
                                parts.append(f"{name}: {v:.1f} {u}")   # com unidade: 1 casa decimal
                            else:
                                parts.append(f"{name}: {int(v)}")      # sem unidade: inteiro
                        except (ValueError, TypeError, OverflowError):
                            continue   # ignora um SPN problemático sem perder os outros
                    decoded_str = "  |  ".join(parts)   # junta os SPNs separados por " | "
            except Exception:
                decoded_str = ""   # decodificação falhou → coluna vazia

        # Bytes de dados em hex, separados por espaço (ex: "18 F0 04 00").
        hex_str = " ".join(f"{b:02X}" for b in msg.data)

        # Texto e alinhamento de cada uma das 7 colunas, na ordem do cabeçalho.
        cols = [id_str, type_str, pgn_str, sa_str, str(msg.dlc), hex_str, decoded_str]
        aligns = [Qt.AlignCenter, Qt.AlignCenter, Qt.AlignLeft, Qt.AlignCenter,
                  Qt.AlignCenter, Qt.AlignLeft, Qt.AlignLeft]

        # Reusa o QTableWidgetItem existente quando possível (mais rápido); só cria
        # um novo na primeira vez. Itens são marcados como não-editáveis.
        for col, (text, align) in enumerate(zip(cols, aligns)):
            item = self._table.item(row, col)
            if item is None:
                item = QTableWidgetItem(text)
                item.setTextAlignment(align | Qt.AlignVCenter)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)   # remove flag de edição
                self._table.setItem(row, col, item)
            else:
                item.setText(text)   # linha já existia → só atualiza o texto

        # Flash highlight — pinta a linha de azul brevemente para sinalizar que
        # acabou de ser atualizada; um timer único restaura a cor após 180 ms.
        for col in range(7):
            item = self._table.item(row, col)
            if item:
                item.setBackground(QColor("#1e3050"))
        QTimer.singleShot(180, lambda r=row: self._reset_row_color(r))

        # Log — se ativo, grava timestamp + ID + bytes em hex, uma linha por msg.
        if self._log_file:
            ts = time.strftime("%H:%M:%S")
            self._log_file.write(f"{ts}  {id_str}  {hex_str}\n")

    @pyqtSlot()
    def _sort_table(self):
        """
        Reordena toda a tabela alfabeticamente pela coluna PGN/Nome (col 2).
        Salva todos os dados, limpa a tabela, e re-insere em ordem.
        Executado só quando o usuário clica o botão (não em cada mensagem).
        """
        if not self._rows:
            return   # tabela vazia → nada a ordenar
        # 1) Salva todos os dados existentes (can_id -> [textos das colunas])
        #    Lê o texto de cada célula para reconstruir as linhas depois.
        data: list[tuple[int, list[str]]] = []
        for cid, r in self._rows.items():
            texts = []
            for c in range(self._table.columnCount()):
                item = self._table.item(r, c)
                texts.append(item.text() if item else "")
            data.append((cid, texts))
        # 2) Ordena pela coluna 2 (PGN/Nome) em ordem alfabética (case-insensitive)
        data.sort(key=lambda x: x[1][2].lower())
        # 3) Pausa atualizações + limpa + re-insere
        #    Esvazia a tabela e o mapeamento, depois reinsere na nova ordem,
        #    reconstruindo _rows com os novos índices de linha. setUpdatesEnabled
        #    evita repaints intermediários (mais rápido); reativado no finally.
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)   # remove todas as linhas
            self._rows.clear()           # zera o mapeamento can_id→linha
            for cid, texts in data:
                row = self._table.rowCount()
                self._table.insertRow(row)
                self._rows[cid] = row    # registra o novo índice desta linha
                aligns = [Qt.AlignCenter, Qt.AlignCenter, Qt.AlignLeft, Qt.AlignCenter,
                          Qt.AlignCenter, Qt.AlignLeft, Qt.AlignLeft]
                for c, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(aligns[c] | Qt.AlignVCenter)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    # Recoloca a cor de fundo conforme a paridade da nova linha
                    # (mantém o efeito de listras alternadas após reordenar).
                    item.setBackground(QColor("#16213e" if row % 2 == 0 else "#1c2745"))
                    self._table.setItem(row, c, item)
        finally:
            self._table.setUpdatesEnabled(True)

    def _rebuild_rows_mapping(self):
        """
        Reconstrói o dict `_rows` (can_id → row index) lendo a coluna 0 da
        tabela. Necessário após `sortItems()` pois Qt reordena as linhas
        fisicamente mas não atualiza o nosso mapeamento.
        """
        new_rows: dict[int, int] = {}
        for r in range(self._table.rowCount()):
            id_item = self._table.item(r, 0)   # coluna 0 = CAN ID em texto
            if id_item is None:
                continue
            txt = id_item.text().strip()
            try:
                # Aceita "0x18F00400" ou "0x214" — sempre interpretado como hex
                if txt.lower().startswith("0x"):
                    can_id = int(txt[2:], 16)   # remove o prefixo "0x" e converte
                else:
                    can_id = int(txt, 16)
                new_rows[can_id] = r            # mapeia o ID lido → índice físico atual
            except ValueError:
                continue   # texto inesperado nessa célula → pula a linha
        self._rows = new_rows   # substitui o mapeamento antigo pelo reconstruído

    def _reset_row_color(self, row: int):
        """Restaura a cor de fundo padrão (listra clara/escura conforme paridade)
        de uma linha após o "flash" de destaque temporário."""
        for col in range(7):
            item = self._table.item(row, col)
            if item:
                item.setBackground(
                    QColor("#16213e" if row % 2 == 0 else "#1c2745"))

    @pyqtSlot(int, int)
    def _on_cell_clicked(self, row: int, col: int):
        """cellClicked(row, col) — só dispara em clique real do usuário.

        Usamos cellClicked (e não itemSelectionChanged) justamente porque ele NÃO
        dispara em mudanças programáticas de seleção, evitando loops/recursão
        quando _select_row chama selectRow internamente.
        """
        self._select_row(row)

    def _select_row(self, row: int):
        """Seleciona a linha indicada e atualiza o gráfico + label de navegação.

        Ponto central da navegação. A partir do índice de linha, descobre o
        can_id (varrendo _rows), manda o gráfico rastrear esse ID, atualiza os
        rótulos (dica, navegação "x/n" e a caixinha de PGN) e rola a tabela para
        deixar a linha visível e centralizada — sem que o usuário precise rolar.
        """
        if row < 0 or row >= self._table.rowCount():
            return   # índice fora dos limites → ignora
        # Busca o can_id correspondente ao número da linha (mapeamento reverso de _rows)
        can_id = next((cid for cid, r in self._rows.items() if r == row), None)
        if can_id is None:
            return
        pgn_name, dlc = self._id_meta.get(can_id, ("", 8))   # metadados p/ o gráfico
        self._graph.track(can_id, pgn_name, dlc)             # gráfico passa a seguir este ID
        is_extended = can_id > 0x7FF                          # >0x7FF ⇒ ID estendido (29-bit)
        id_str = f"0x{can_id:08X}" if is_extended else f"0x{can_id:04X}"
        label  = f"Rastreando: {id_str}  {pgn_name}".strip()
        self._lbl_graph_hint.setText(label)
        self._lbl_graph_hint.setStyleSheet(
            f"color: {COLORS['success']}; font-size: 11px;")
        # Atualiza label de navegação ("posição atual / total de linhas")
        self._lbl_nav.setText(f"{row + 1}/{self._table.rowCount()}")
        # Atualiza caixinha da PGN no gráfico (mostra PGN+sigla para J1939, ou ID)
        if is_j1939(can_id, is_extended):
            _, pgn_dec, _ = decode_29bit_id(can_id)
            box_txt = f"📊 PGN {pgn_dec}"
            if pgn_name:
                box_txt += f" · {pgn_name}"
        else:
            box_txt = f"📊 ID {can_id}  ({id_str})"
        self._lbl_pgn_box.setText(box_txt)
        # Garante que a linha selecionada está visível e marcada
        self._table.selectRow(row)               # destaca visualmente a linha
        item = self._table.item(row, 0)
        if item is not None:
            from PyQt5.QtWidgets import QAbstractItemView
            # Rola até centralizar a linha na viewport da tabela.
            self._table.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    @pyqtSlot()
    def _select_prev(self):
        """Vai para a PGN/linha anterior (com wrap-around).

        Decrementa o índice atual; se já estava na primeira (ou nada selecionado),
        dá a volta para a última linha (operador % faz o wrap-around).
        """
        n = self._table.rowCount()
        if n == 0:
            return
        cur = self._table.currentRow()
        new = (cur - 1) % n if cur >= 0 else n - 1   # sem seleção → começa pela última
        self._select_row(new)

    @pyqtSlot()
    def _select_next(self):
        """Vai para a próxima PGN/linha (com wrap-around).

        Incrementa o índice atual; ao passar da última, volta para a primeira.
        """
        n = self._table.rowCount()
        if n == 0:
            return
        cur = self._table.currentRow()
        new = (cur + 1) % n if cur >= 0 else 0   # sem seleção → começa pela primeira
        self._select_row(new)

    def keyPressEvent(self, event):
        """Atalhos de teclado: Ctrl+↑/↓ navegam entre PGNs.

        Intercepta Ctrl+Seta para cima/baixo e chama a navegação; qualquer outra
        tecla é repassada ao comportamento padrão do Qt (super()).
        """
        from PyQt5.QtCore import Qt
        if event.modifiers() & Qt.ControlModifier:
            if event.key() == Qt.Key_Up:
                self._select_prev()
                return
            elif event.key() == Qt.Key_Down:
                self._select_next()
                return
        super().keyPressEvent(event)

    @pyqtSlot(str)
    def _on_filter_text_changed(self, text: str):
        """Reage ao texto digitado no campo de filtro.

        Guarda o texto em minúsculas (a comparação é case-insensitive) e reaplica
        os filtros imediatamente sobre as linhas já existentes.
        """
        self._filter_text = text.lower()
        self._apply_filters()

    @pyqtSlot()
    def _apply_filters(self):
        """
        Aplica filtros (Apenas J1939 + texto) nas linhas EXISTENTES da tabela,
        usando setRowHidden em vez de remover. Não perde histórico.

        O texto do filtro casa em:
          • CAN ID em hex     (ex: 0x18F00400 ou f00400)
          • CAN ID em decimal (ex: 532)         — para 11-bit proprietário
          • PGN em decimal    (ex: 61444)       — para J1939
          • Sigla da PGN      (ex: eec1, ccvs)
        """
        only_j1939 = self._chk_j1939.isChecked()
        filt = self._filter_text
        for can_id, row in self._rows.items():
            is_extended = can_id > 0x7FF
            is_j_msg    = is_j1939(can_id, is_extended)
            id_hex      = (f"0x{can_id:08X}" if is_extended
                           else f"0x{can_id:04X}").lower()
            pgn_name, _ = self._id_meta.get(can_id, ("", 8))

            # PGN decimal (se J1939) e CAN ID decimal (sempre, útil em 11-bit)
            if is_j_msg:
                _, pgn_dec, _ = decode_29bit_id(can_id)
                pgn_str = str(pgn_dec)
            else:
                pgn_str = str(can_id)   # 11-bit: CAN ID em decimal

            # Decide se a linha fica oculta. Importante: usamos setRowHidden em vez
            # de remover a linha — assim o histórico é preservado e basta limpar o
            # filtro para a linha reaparecer.
            hidden = False
            if only_j1939 and not is_j_msg:
                hidden = True   # filtro "Apenas J1939" e a msg não é J1939 → esconde
            if filt:
                # "Palheiro" (haystack) com todas as representações pesquisáveis
                # concatenadas; basta o filtro casar em qualquer uma delas:
                #   id_hex      → "0x18f00400" (hex do CAN ID)
                #   str(can_id) → CAN ID em decimal (útil p/ 11-bit proprietário)
                #   pgn_str     → PGN em decimal (J1939) ou CAN ID decimal
                #   pgn_name    → sigla da PGN (ex: "eec1")
                hay = (id_hex + " " +
                       str(can_id) + " " +
                       pgn_str + " " +
                       (pgn_name or "").lower())
                if filt not in hay:
                    hidden = True   # texto não casou em nenhuma representação → esconde
            self._table.setRowHidden(row, hidden)

    @pyqtSlot()
    def _refresh_decoded_column(self):
        """
        Reconstrói a coluna 'Valores Decodificados' (col 6) para todas as linhas
        existentes, com base no estado atual do checkbox 'Decodificar J1939'.
        """
        show_decoded = self._chk_decoded.isChecked()
        for can_id, row in self._rows.items():
            decoded_str = ""
            if show_decoded:
                is_extended = can_id > 0x7FF
                # Tenta usar o dado atual da coluna 5 (hex) para decodificar.
                # Reaproveita o hex já exibido em vez de guardar os bytes crus.
                hex_item = self._table.item(row, 5)
                if hex_item and is_j1939(can_id, is_extended):
                    try:
                        # Reconverte o texto hex "18 F0 04 00" de volta em bytes
                        data = bytes(int(b, 16) for b in hex_item.text().split())
                        dec = decode_message(can_id, data, is_extended)
                        if dec and dec.get("spns"):
                            parts = []
                            for name, info in dec["spns"].items():
                                v, u = info['value'], info['unit']
                                parts.append(
                                    f"{name}: {v:.1f} {u}" if u else f"{name}: {int(v)}")
                            decoded_str = "  |  ".join(parts)
                    except (ValueError, IndexError):
                        pass   # hex inválido/incompleto → deixa a coluna vazia
            # Atualiza só a coluna 6, criando o item se ainda não existir.
            item = self._table.item(row, 6)
            if item is None:
                item = QTableWidgetItem(decoded_str)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self._table.setItem(row, 6, item)
            else:
                item.setText(decoded_str)

    @pyqtSlot(bool)
    def _on_pause(self, checked: bool):
        """Liga/desliga a pausa. Quando pausado, on_message para de enfileirar
        novas mensagens (mas o contador total continua somando). Atualiza o rótulo
        do botão."""
        self._paused = checked
        self._btn_pause.setText("▶  Continuar" if checked else "⏸  Pausar")

    @pyqtSlot()
    def _clear(self):
        """Limpa tudo: tabela, mapeamentos, fila pendente, contador, gráfico e
        rótulos voltam ao estado inicial."""
        self._table.setRowCount(0)
        self._rows.clear()
        self._id_meta.clear()
        self._pending.clear()
        self._total_count = 0
        self._graph.clear()
        self._lbl_graph_hint.setText(
            "💡 Clique em uma linha para ver o gráfico em tempo real")
        self._lbl_graph_hint.setStyleSheet(
            f"color: {COLORS['accent']}; font-size: 11px;")
        self._lbl_nav.setText("—/—")
        self._lbl_pgn_box.setText("📊 —")
