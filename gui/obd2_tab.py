"""
Aba "OBD-II / UDS" (PyQt5) — leitura de diagnóstico por pergunta/resposta.

DIFERENÇA FUNDAMENTAL para as outras abas:
    O Monitor CAN e a Descoberta de Sinais são PASSIVOS (só escutam o que as
    ECUs transmitem sozinhas). Esta aba é a ÚNICA que TRANSMITE: diagnóstico
    é pergunta/resposta, a ECU só fala se for perguntada.

    Por isso, ela exige que a conexão esteja com "Listen-Only" DESMARCADO.
    Em modo Simulação, o CANBus fabrica respostas sintéticas, permitindo
    testar a aba sem hardware.

DOIS SERVIÇOS, O MESMO CANAL:
    • OBD-II modo 01 (SAE J1979) — PIDs de 1 byte, resposta 0x41. É o
      diagnóstico legislado, igual em qualquer veículo.
    • UDS $22 (ISO 14229) — DIDs de 2 bytes, resposta 0x62. É onde ficam os
      sinais proprietários das montadoras (ex.: a lista da VWCO).
    Os dois usam as mesmas IDs (0x7DF → 0x7E8..0x7EF) e o mesmo transporte
    ISO-TP, então a tabela lista PIDs e DIDs juntos, com a coluna "Tipo"
    dizendo qual serviço cada linha usa.

REGRAS DE SEGURANÇA QUE ESTA ABA RESPEITA:
    1) Só serviços de LEITURA (0x01 e 0x22). A lista branca de transmissão do
       CANBus (tx_policy_check) recusa qualquer outro no portão.
    2) NÃO troca a sessão de diagnóstico. Se um DID responder que exige sessão
       estendida, a aba REPORTA o código negativo e para — escalar a sessão
       poderia degradar o veículo.
    3) UM REQUEST PENDENTE POR VEZ. Sem isso, uma transferência multi-frame se
       misturaria com o request seguinte, e uma resposta negativa (que não
       repete o DID) não teria dono.
    4) Flow Control só é enviado quando HÁ um request nosso pendente — assim
       não atropelamos a transferência de outro equipamento de diagnóstico
       conectado no mesmo barramento.

REPARTIÇÃO ENTRE THREADS:
    on_message() roda na thread do CAN e só faz uma coisa: enfileirar os
    quadros que estão na faixa de resposta de diagnóstico. TODA a máquina de
    estados (ISO-TP, request pendente, timeouts) e toda a atualização de
    widgets acontecem em _drain_responses(), na thread da interface.
"""

import os
import time
from collections import deque
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QMessageBox,
    QFileDialog, QInputDialog,
)

from core.can_bus import CANMessage
from core.obd2 import (
    PID_DATABASE, MODE9_PIDS, ECU_NAMES, PROTOCOL_NOTES, VS_FIELD_DOC,
    LIB_MAX_FILTERS, OBD_REQUEST_FUNCTIONAL, OBD_REQUEST_PHYSICAL_BASE,
    OBD_RESP_MIN, OBD_RESP_MAX, RESP_MODE_09, build_request,
    build_mode09_request, decode_mode01_payload, decode_mode09_payload,
    ecu_name, formula_text, build_can_library,
)
from core.uds import (
    DID_DATABASE, PRIORITY_DIDS, TEXT_DIDS, UDS_PROTOCOL_NOTES,
    FLOW_CONTROL_CTS, IsoTpReader, build_read_did_request,
    parse_read_did_response, flow_control_id_for, interpret, raw_to_int,
    format_hex, nrc_name,
)
from gui.styles import COLORS

# Pasta padrão sugerida ao salvar a documentação (a mesma usada pelos logs de
# sessão da descoberta de sinais, para o usuário achar tudo no mesmo lugar).
DOC_DIR = os.path.join(os.path.expanduser("~"), "Documents", "IxxatInterface")

# Tipos de item da tabela. Um item é a tupla (KIND, número):
#   ("pid",  0x0C)   → OBD-II modo 01, PID 0x0C     (dados atuais)
#   ("pid9", 0x02)   → OBD-II modo 09, PID 0x02     (informações do veículo)
#   ("did",  0x0101) → UDS $22, DID 0x0101          (proprietário)
KIND_PID = "pid"
KIND_PID9 = "pid9"
KIND_DID = "did"

# PIDs e DIDs já marcados ao abrir o programa: os mais usados do OBD-II mais
# os sinais que a montadora marcou como prioritários. O chassi (modo 09 e DID
# 0xF190) vem marcado porque identifica o veículo de toda a coleta.
DEFAULT_PIDS = (0x0C, 0x0D, 0x05, 0x11)


class OBD2Tab(QWidget):
    """Aba de leitura de diagnóstico: PIDs OBD-II (modo 01) e DIDs UDS ($22)."""

    # Colunas da tabela
    (COL_ATIVO, COL_TIPO, COL_ID, COL_NOME, COL_VALOR,
     COL_UNID, COL_BRUTO, COL_FONTE, COL_STATUS) = range(9)

    # Cadência do despachante de requests e da atualização da tabela.
    DISPATCH_MS = 60
    DRAIN_MS = 60
    # Intervalo mínimo entre requests durante o stream (~8 req/s): rápido o
    # bastante para acompanhar o sinal e gentil com o gateway do veículo.
    STREAM_MIN_GAP = 0.125
    # Tempo que esperamos a resposta antes de desistir do request pendente.
    # Acima do N_Bs/N_Cr típico do ISO-TP (1 s) para dar margem ao gateway.
    REQ_TIMEOUT = 1.2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bus = None

        # ── Comunicação entre threads ────────────────────────────────────────
        # A thread do CAN só empilha aqui; a thread da GUI consome. deque com
        # limite evita crescimento sem controle se a GUI ficar ocupada.
        self._frames: deque = deque(maxlen=4000)

        # ── Estado da tabela ─────────────────────────────────────────────────
        self._rows: dict[tuple, int] = {}          # item -> linha da tabela
        # item -> conjunto de ECUs que responderam. Em broadcast VÁRIAS ECUs
        # podem responder o mesmo PID/DID com valores diferentes; guardamos as
        # origens para avisar o operador em vez de sobrescrever a célula.
        self._sources: dict[tuple, set] = {}

        # ── Máquina de estados das consultas (só thread da GUI) ──────────────
        self._isotp = IsoTpReader()        # remontagem multi-frame
        self._queue: list = []             # fila do "Ler uma vez"
        self._poll_order: list = []        # rodízio do stream
        self._poll_idx = 0
        self._streaming = False
        # Request pendente: {"item", "can_id", "data", "sent"} ou None.
        # É o coração da regra "um request por vez".
        self._inflight = None
        self._last_sent = 0.0

        # ── Registro para a DOCUMENTAÇÃO exportável ──────────────────────────
        # Acumula, ao longo de toda a sessão, o que realmente circulou no
        # barramento. É a matéria-prima do botão "Exportar Documentação" — sem
        # isso o arquivo seria só teoria, não o protocolo observado.
        #   _doc_req: (kind, num, id do request) -> {count, data, first}
        #   _doc_obs: (kind, num, id da ECU)     -> {count, valores, bytes}
        #   _doc_nrc: (kind, num)                -> {nrc, count, src}
        self._doc_req: dict[tuple, dict] = {}
        self._doc_obs: dict[tuple, dict] = {}
        self._doc_nrc: dict[tuple, dict] = {}
        self._doc_start: float = 0.0    # instante do 1º request da sessão
        self._doc_fc_sent = 0           # quantos Flow Control transmitimos
        self._doc_timeouts = 0          # requests que estouraram o timeout
        # Modelo do veículo usado na folha "Biblioteca CAN" (o usuário informa
        # na hora de exportar; guardamos para não digitar de novo).
        self._doc_model = "VEICULO"

        self._setup_ui()

        # Timer que processa as respostas e atualiza a tabela (thread da GUI).
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._drain_responses)
        self._ui_timer.start(self.DRAIN_MS)

        # Timer do despachante: envia no máximo um request por vez.
        self._dispatch_timer = QTimer(self)
        self._dispatch_timer.timeout.connect(self._dispatch_next)
        self._dispatch_timer.start(self.DISPATCH_MS)

    def set_bus(self, bus):
        """Recebe a referência do CANBus (usada para transmitir os requests)."""
        self._bus = bus

    # ════════════════════════════════════════════════════════════════════════
    #  Construção da interface
    # ════════════════════════════════════════════════════════════════════════

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Cabeçalho
        hdr = QHBoxLayout()
        title = QLabel("Leitura de Diagnóstico — OBD-II (modo 01) e UDS ($22)")
        title.setObjectName("label_title")
        hdr.addWidget(title)
        hdr.addStretch()

        self._btn_all = QPushButton("☑  Marcar Todos")
        self._btn_all.clicked.connect(lambda: self._set_all_checked(True))
        hdr.addWidget(self._btn_all)

        self._btn_none = QPushButton("☐  Desmarcar")
        self._btn_none.clicked.connect(lambda: self._set_all_checked(False))
        hdr.addWidget(self._btn_none)

        self._btn_once = QPushButton("📥  Ler Uma Vez")
        self._btn_once.setObjectName("btn_success")
        self._btn_once.clicked.connect(self._read_once)
        hdr.addWidget(self._btn_once)

        self._btn_stream = QPushButton("▶  Stream Contínuo")
        self._btn_stream.setCheckable(True)
        self._btn_stream.toggled.connect(self._toggle_stream)
        hdr.addWidget(self._btn_stream)

        # Exporta um arquivo documentando o diálogo observado.
        self._btn_doc = QPushButton("📄  Exportar Documentação")
        self._btn_doc.clicked.connect(self._export_doc)
        self._btn_doc.setToolTip(
            "Gera um arquivo (.xlsx ou .txt) documentando o protocolo desta\n"
            "sessão: requests transmitidos, respostas por ECU, bytes crus,\n"
            "respostas negativas (NRC), conversões, biblioteca CAN pronta e a\n"
            "referência das normas SAE J1979 / ISO 14229 / ISO 15765-4."
        )
        hdr.addWidget(self._btn_doc)
        layout.addLayout(hdr)

        # ── Seletor de ECU alvo ──────────────────────────────────────────────
        # Com "Todas" (0x7DF) o request é broadcast e vários módulos respondem
        # o mesmo PID/DID, cada um com seu valor. Endereçar uma ECU específica
        # (0x7E0+n) elimina essa ambiguidade.
        ecu_row = QHBoxLayout()
        ecu_row.addWidget(QLabel("ECU alvo:"))
        self._cmb_ecu = QComboBox()
        # Cada módulo tem DUAS IDs: uma onde escuta (0x7E0+n) e outra onde
        # responde (0x7E8+n). O rótulo mostra as duas porque a coluna "Fonte"
        # exibe a de resposta — sem isso o operador vê 0x7E8 na tabela, não
        # encontra esse número no seletor e conclui que é outra ECU.
        self._cmb_ecu.addItem("Todas as ECUs (broadcast 0x7DF)", None)
        for n in range(8):
            self._cmb_ecu.addItem(
                f"Somente ECU {n + 1}  (pergunta 0x{0x7E0 + n:03X} → "
                f"responde 0x{OBD_RESP_MIN + n:03X})", n)
        self._cmb_ecu.setToolTip(
            "Broadcast: todas as ECUs respondem — o MESMO PID/DID pode voltar\n"
            "com valores diferentes de módulos diferentes (a coluna Fonte\n"
            "mostra a origem e avisa quando há conflito).\n"
            "ECU específica: só aquele módulo responde, sem ambiguidade.\n\n"
            "As duas IDs são do MESMO módulo: ele escuta em 0x7E0+n e responde\n"
            "em 0x7E8+n. Por isso a coluna Fonte mostra 0x7E8 quando você\n"
            "consulta a ECU 1."
        )
        self._cmb_ecu.setMinimumWidth(360)
        ecu_row.addWidget(self._cmb_ecu)
        ecu_row.addStretch()
        layout.addLayout(ecu_row)

        # Aviso sobre transmissão / listen-only
        self._lbl_warn = QLabel(
            "⚠️  Esta aba TRANSMITE requests de leitura (serviços 0x01 e 0x22). "
            "Conecte com 'Listen-Only' DESMARCADO (ou use Modo Simulação para "
            "testar). Nenhum serviço de escrita, atuação ou troca de sessão é "
            "usado — a lista branca do programa recusa todos eles."
        )
        self._lbl_warn.setStyleSheet(
            f"color: {COLORS['warning']}; font-size: 11px; padding: 4px;")
        self._lbl_warn.setWordWrap(True)
        layout.addWidget(self._lbl_warn)

        # Barra de status
        self._lbl_status = QLabel("Pronto.")
        self._lbl_status.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 12px;")
        self._lbl_status.setWordWrap(True)
        layout.addWidget(self._lbl_status)

        # Tabela de PIDs e DIDs
        self._table = QTableWidget(0, 9)
        self._table.setHorizontalHeaderLabels(
            ["Ler", "Tipo", "PID / DID", "Sinal", "Valor", "Unidade",
             "Dados (bruto)", "Fonte (ECU)", "Status"])
        h = self._table.horizontalHeader()
        for col in (self.COL_ATIVO, self.COL_TIPO, self.COL_ID, self.COL_VALOR,
                    self.COL_UNID, self.COL_BRUTO, self.COL_FONTE,
                    self.COL_STATUS):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_NOME, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet("""
            QTableWidget {
                background-color: #16213e;
                alternate-background-color: #1c2745;
                color: #d0d0e8;
                gridline-color: #2a2a44;
                selection-background-color: #6c63ff;
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background-color: #1a1a2e;
                color: #c0c0e0;
                padding: 6px;
                border: 1px solid #2a2a44;
                font-weight: bold;
            }
        """)
        layout.addWidget(self._table)

        self._populate_table()

    def _populate_table(self):
        """
        Cria uma linha por PID e por DID.

        Os PIDs vêm primeiro (ordenados pelo número), depois os DIDs. Os DIDs
        prioritários da montadora e os PIDs mais usados já vêm marcados, de
        forma que o operador possa apertar "Ler Uma Vez" sem configurar nada.
        """
        self._table.setRowCount(0)
        self._rows.clear()

        itens = ([(KIND_PID, pid) for pid in sorted(PID_DATABASE)]
                 + [(KIND_PID9, pid) for pid in sorted(MODE9_PIDS)]
                 + [(KIND_DID, did) for did in sorted(DID_DATABASE)])

        for item in itens:
            kind, num = item
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[item] = row

            # Coluna 0: checkbox de seleção (widget próprio, centralizado)
            chk = QCheckBox()
            if kind == KIND_PID:
                chk.setChecked(num in DEFAULT_PIDS)
            elif kind == KIND_PID9:
                chk.setChecked(True)          # chassi: sempre útil na coleta
            else:
                # Prioritários da montadora + o chassi padronizado (0xF190).
                chk.setChecked(num in PRIORITY_DIDS or num in TEXT_DIDS)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.addWidget(chk)
            hl.setAlignment(Qt.AlignCenter)
            hl.setContentsMargins(0, 0, 0, 0)
            self._table.setCellWidget(row, self.COL_ATIVO, holder)

            for col, text in (
                (self.COL_TIPO,   self._item_service(item)),
                (self.COL_ID,     self._item_label(item)),
                (self.COL_NOME,   self._item_name(item)),
                (self.COL_VALOR,  "—"),
                (self.COL_UNID,   self._item_unit(item)),
                (self.COL_BRUTO,  "—"),
                (self.COL_FONTE,  "—"),
                (self.COL_STATUS, "aguardando"),
            ):
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                align = Qt.AlignLeft if col == self.COL_NOME else Qt.AlignCenter
                cell.setTextAlignment(align | Qt.AlignVCenter)
                self._table.setItem(row, col, cell)

            # Tooltip explica a conversão (e avisa quando é hipótese).
            nome_cell = self._table.item(row, self.COL_NOME)
            if nome_cell is not None:
                nome_cell.setToolTip(self._item_conversion(item))

    # ════════════════════════════════════════════════════════════════════════
    #  Helpers de item (PID ou DID)
    # ════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _ecu_label(resp_id: int) -> str:
        """
        Nome curto do módulo a partir da ID de RESPOSTA.

        0x7E8 → "ECU 1", 0x7E9 → "ECU 2" … A numeração é a mesma usada no
        seletor "ECU alvo", que fala em IDs de pergunta (0x7E0+n).
        """
        if OBD_RESP_MIN <= resp_id <= OBD_RESP_MAX:
            return f"ECU {resp_id - OBD_RESP_MIN + 1}"
        return f"0x{resp_id:03X}"

    @staticmethod
    def _item_service(item: tuple) -> str:
        """Serviço usado pelo item, como aparece na coluna 'Tipo'."""
        return {KIND_PID: "PID 01", KIND_PID9: "PID 09"}.get(item[0], "DID 22")

    @staticmethod
    def _item_label(item: tuple) -> str:
        """
        Identificação do item.

        PID sai com o decimal ao lado porque a biblioteca do equipamento
        (VIRLOC) referencia PIDs em decimal; DID sai só em hexadecimal, como a
        montadora documenta.
        """
        kind, num = item
        if kind == KIND_DID:
            return f"0x{num:04X}"
        return f"0x{num:02X} ({num})"

    @staticmethod
    def _item_name(item: tuple) -> str:
        kind, num = item
        if kind == KIND_PID:
            info = PID_DATABASE.get(num)
            return info.name if info else "—"
        if kind == KIND_PID9:
            info = MODE9_PIDS.get(num)
            return info[0] if info else "—"
        info = DID_DATABASE.get(num)
        return info.name if info else "—"

    @staticmethod
    def _item_unit(item: tuple) -> str:
        kind, num = item
        if kind == KIND_PID9:
            return ""                     # informação do veículo é texto
        info = PID_DATABASE.get(num) if kind == KIND_PID else DID_DATABASE.get(num)
        return (info.unit if info else "") or ""

    @staticmethod
    def _item_conversion(item: tuple) -> str:
        """
        Texto da conversão do item.

        Para PID do modo 01 é a fórmula da norma (valor confiável); para o
        modo 09 é texto ASCII; para DID é a hipótese registrada em
        core/uds.py — e o texto diz isso, porque a montadora não informou as
        escalas dos DIDs proprietários.
        """
        kind, num = item
        if kind == KIND_PID:
            return f"SAE J1979: {formula_text(num)}"
        if kind == KIND_PID9:
            return "SAE J1979 modo 09: texto ASCII (sem conversão numérica)"
        info = DID_DATABASE.get(num)
        if info is None:
            return "—"
        if info.kind == "ascii":
            return (f"ISO 14229-1: {info.hypothesis or 'texto ASCII'} "
                    f"(sem conversão numérica)")
        return (f"UDS $22 — conversão: {info.hypothesis or 'desconhecida'}"
                f"\nEscala não informada pela montadora: confira o valor bruto.")

    def _build_item_request(self, item: tuple, target) -> tuple:
        """Monta (can_id, data) do request do item, conforme o serviço."""
        kind, num = item
        if kind == KIND_PID:
            return build_request(num, target_ecu=target)
        if kind == KIND_PID9:
            return build_mode09_request(num, target_ecu=target)
        return build_read_did_request(num, target_ecu=target)

    # ── Seleção na tabela ────────────────────────────────────────────────────

    def _checkbox_at(self, row: int) -> QCheckBox:
        """Devolve o QCheckBox da coluna 'Ler' de uma linha."""
        holder = self._table.cellWidget(row, self.COL_ATIVO)
        return holder.findChild(QCheckBox) if holder else None

    def _set_all_checked(self, checked: bool):
        for row in range(self._table.rowCount()):
            chk = self._checkbox_at(row)
            if chk:
                chk.setChecked(checked)

    def _active_items(self) -> list:
        """Itens (PIDs e DIDs) marcados pelo usuário, na ordem da tabela."""
        return [item for item, row in sorted(self._rows.items(),
                                             key=lambda kv: kv[1])
                if (chk := self._checkbox_at(row)) and chk.isChecked()]

    def _active_pids(self) -> list[int]:
        """Só os PIDs marcados (usado pela biblioteca CAN, que é de PIDs)."""
        return [num for (kind, num) in self._active_items() if kind == KIND_PID]

    def _target_ecu(self):
        """ECU alvo escolhida no combo: None = broadcast, 0..7 = física."""
        return self._cmb_ecu.currentData()

    # ════════════════════════════════════════════════════════════════════════
    #  Envio de requests (um por vez)
    # ════════════════════════════════════════════════════════════════════════

    def _check_ready(self) -> bool:
        """Valida que dá para transmitir; explica ao usuário se não der."""
        if self._bus is None or not self._bus.is_connected:
            QMessageBox.warning(self, "Sem conexão",
                                "Conecte ao barramento antes de consultar.")
            return False
        # Em hardware real, listen-only impede transmitir.
        if (not self._bus.is_simulation) and self._bus.is_listen_only:
            QMessageBox.warning(
                self, "Listen-Only ativo",
                "A leitura de diagnóstico precisa TRANSMITIR requests para a "
                "ECU responder.\n\n"
                "Desconecte, DESMARQUE a caixa 'Listen-Only' e conecte de novo."
            )
            return False
        if not self._active_items():
            QMessageBox.information(
                self, "Nada marcado",
                "Marque ao menos um PID ou DID na coluna 'Ler'.")
            return False
        return True

    @pyqtSlot()
    def _read_once(self):
        """
        Enfileira uma rodada de consultas dos itens marcados.

        Não transmite aqui: quem transmite é o despachante (_dispatch_next),
        um request por vez. Mandar todos de uma vez embaralharia as respostas
        multi-frame e deixaria as respostas negativas sem dono.
        """
        if not self._check_ready():
            return
        itens = self._active_items()
        self._sources.clear()      # nova leitura recomeça a detecção de conflito
        self._queue = list(itens)
        for item in itens:
            self._set_status(item, "na fila", COLORS['text_muted'])
        self._status_msg(f"{len(itens)} consulta(s) na fila "
                         f"(uma por vez).", COLORS['accent'])

    @pyqtSlot(bool)
    def _toggle_stream(self, checked: bool):
        """Liga/desliga a consulta contínua em rodízio."""
        if checked:
            if not self._check_ready():
                self._btn_stream.setChecked(False)
                return
            self._poll_order = self._active_items()
            self._poll_idx = 0
            self._streaming = True
            self._sources.clear()
            self._btn_stream.setText("⏸  Parar Stream")
            self._status_msg(
                f"Stream ativo — {len(self._poll_order)} item(ns) em rodízio, "
                f"um request por vez.", COLORS['success'])
        else:
            self._streaming = False
            self._btn_stream.setText("▶  Stream Contínuo")
            self._status_msg("Stream parado.", COLORS['text_muted'])

    @pyqtSlot()
    def _dispatch_next(self):
        """
        Despacha o próximo request, respeitando "um pendente por vez".

        Ordem de prioridade: a fila do "Ler uma vez" primeiro, depois o rodízio
        do stream. Enquanto houver request pendente (e dentro do timeout), não
        sai nada novo — é isso que mantém o diálogo ISO-TP íntegro.
        """
        if self._bus is None or not self._bus.is_connected:
            return
        if self._inflight is not None:
            return                      # ainda esperando resposta
        now = time.time()
        gap = self.STREAM_MIN_GAP if self._streaming else self.DISPATCH_MS / 1000.0
        if now - self._last_sent < gap:
            return

        if self._queue:
            item = self._queue.pop(0)
        elif self._streaming and self._poll_order:
            item = self._poll_order[self._poll_idx % len(self._poll_order)]
            self._poll_idx += 1
        else:
            return

        can_id, data = self._build_item_request(item, self._target_ecu())
        ok, msg = self._bus.send(can_id, data, is_extended=False)
        if not ok:
            # Falha de transmissão: para tudo e explica. Pode ser listen-only,
            # barramento fora do ar ou recusa da lista branca de segurança.
            self._queue.clear()
            if self._streaming:
                self._btn_stream.setChecked(False)
            self._status_msg(f"Transmissão interrompida: {msg}", COLORS['error'])
            return

        self._last_sent = now
        self._inflight = {"item": item, "can_id": can_id,
                          "data": bytes(data), "sent": now}
        self._isotp.reset()     # nenhuma transferência antiga nos interessa
        self._record_request(item, can_id, data)
        self._set_status(item, "consultando...", COLORS['warning'])

    def _send_flow_control(self, resp_id: int, ts: float = 0.0):
        """
        Responde o Flow Control de uma transferência multi-frame.

        SÓ envia quando o First Frame pode ser resposta ao NOSSO request: tem
        que haver request pendente E o quadro tem que ter chegado depois dele.
        Um First Frame de outro equipamento de diagnóstico não é nosso, e
        mandar Flow Control nele atropelaria a sessão do outro.

        O FC vai endereçado à ECU que está transmitindo (0x7E0+n), nunca na ID
        funcional 0x7DF.
        """
        if self._inflight is None or ts < self._inflight["sent"]:
            self._status_msg(
                f"First Frame de 0x{resp_id:03X} ignorado — não é resposta a "
                f"um request nosso (provável tráfego de outro equipamento).",
                COLORS['text_muted'])
            return
        fc_id = flow_control_id_for(resp_id)
        ok, msg = self._bus.send(fc_id, FLOW_CONTROL_CTS, is_extended=False)
        if ok:
            self._doc_fc_sent += 1
        else:
            self._status_msg(f"Falha ao enviar Flow Control: {msg}",
                             COLORS['error'])

    # ════════════════════════════════════════════════════════════════════════
    #  Recepção de respostas
    # ════════════════════════════════════════════════════════════════════════

    def on_message(self, msg: CANMessage):
        """
        Chamado da THREAD DO CAN para cada mensagem recebida.

        Faz o mínimo possível: se o quadro está na faixa de resposta de
        diagnóstico, enfileira uma cópia. NÃO interpreta e NÃO toca em widgets
        Qt — toda a máquina de estados roda na thread da interface.
        """
        try:
            if msg.is_extended:
                return
            if not (OBD_RESP_MIN <= msg.can_id <= OBD_RESP_MAX):
                return
            # O instante de RECEBIMENTO é guardado junto: é ele que permite
            # descartar um quadro que chegou ANTES do request atual sair (ver
            # _handle_payload) e assim não atribuir resposta ao item errado.
            self._frames.append((msg.can_id, bytes(msg.data), msg.dlc,
                                 time.time()))
        except Exception:
            return

    @pyqtSlot()
    def _drain_responses(self):
        """
        Processa os quadros recebidos e atualiza a tabela (thread da GUI).

        Todo quadro passa pelo remontador ISO-TP, que trata igualmente Single
        Frame e multi-frame. Quando um payload fica completo, o primeiro byte
        diz o que ele é: 0x41 = resposta do modo 01, 0x62 = resposta do $22,
        0x7F = resposta negativa.
        """
        now = time.time()
        while self._frames:
            try:
                can_id, data, dlc, ts = self._frames.popleft()
            except IndexError:
                break
            try:
                ev = self._isotp.feed(can_id, data, now)
            except Exception:
                continue

            if ev.kind == "need_fc":
                self._send_flow_control(can_id, ts)
            elif ev.kind == "complete":
                # ev.first_frame é o 1º quadro da resposta (não o último a
                # chegar): é ele que vai para a documentação como payload RX.
                self._handle_payload(can_id, ev.payload,
                                     ev.first_frame or data, ev.frames, ts)
            elif ev.kind == "error":
                self._status_msg(f"ISO-TP (0x{can_id:03X}): {ev.detail}",
                                 COLORS['warning'])

        # Transferências multi-frame que pararam no meio.
        for src in self._isotp.purge(now):
            self._status_msg(
                f"Resposta multi-frame incompleta de 0x{src:03X} — descartada.",
                COLORS['warning'])

        # Request pendente que não recebeu resposta dentro do prazo.
        if self._inflight and now - self._inflight["sent"] > self.REQ_TIMEOUT:
            item = self._inflight["item"]
            self._inflight = None
            self._doc_timeouts += 1
            self._set_status(item, "sem resposta", COLORS['text_muted'])

    def _handle_payload(self, src: int, payload: bytes,
                        first_frame: bytes, frames: int, ts: float = 0.0):
        """
        Interpreta um payload ISO-TP completo vindo de uma ECU.

        O parâmetro ts (instante em que o quadro foi recebido) só importa para
        a resposta NEGATIVA — ver o comentário nesse trecho.
        """
        if not payload:
            return
        sid = payload[0]

        # ── Resposta do OBD-II modo 01 ───────────────────────────────────────
        if sid == 0x41:
            res = decode_mode01_payload(payload)
            if res is None:
                return
            item = (KIND_PID, res["pid"])
            self._update_item(item, src, res["value"], res["data"],
                              first_frame, frames)
            return

        # ── Resposta do OBD-II modo 09 (informações do veículo) ──────────────
        if sid == RESP_MODE_09:
            res = decode_mode09_payload(payload)
            if res is None:
                return
            item = (KIND_PID9, res["pid"])
            # O valor aqui é TEXTO (ex.: o chassi), não número.
            self._update_item(item, src, res["value"], res["data"],
                              first_frame, frames)
            return

        # ── Resposta positiva do UDS $22 ─────────────────────────────────────
        if sid == 0x62:
            res = parse_read_did_response(payload)
            if res is None or res.get("kind") != "positive":
                return
            did = res["did"]
            item = (KIND_DID, did)
            if item not in self._rows:
                # DID que não está no nosso banco: registra como desconhecido
                # em vez de descartar — é informação de campo valiosa.
                self._status_msg(
                    f"DID 0x{did:04X} respondeu mas não está no banco do "
                    f"programa: dados {format_hex(res['data'])}",
                    COLORS['warning'])
                return
            valor, _ = interpret(did, res["data"])
            self._update_item(item, src, valor, res["data"],
                              first_frame, frames)
            return

        # ── Resposta negativa ────────────────────────────────────────────────
        if sid == 0x7F:
            res = parse_read_did_response(payload)
            if res is None or res.get("kind") != "negative":
                return
            # A negativa NÃO repete o DID: só sabemos de quem é porque
            # mantemos um único request pendente por vez.
            #
            # E só vale se o quadro chegou DEPOIS do request pendente sair.
            # Sem essa checagem, uma negativa atrasada (de um request que já
            # estourou o timeout) seria creditada ao item seguinte — o
            # operador veria "recusado pela ECU" num DID que nem foi
            # respondido ainda.
            if self._inflight is None or ts < self._inflight["sent"]:
                self._status_msg(
                    f"Resposta negativa de 0x{src:03X} descartada: chegou fora "
                    f"da janela do request pendente.", COLORS['text_muted'])
                return
            item = self._inflight["item"]
            nrc = res["nrc"]
            self._inflight = None
            self._doc_nrc[item] = {
                "nrc": nrc, "name": res["nrc_name"], "src": src,
                "count": self._doc_nrc.get(item, {}).get("count", 0) + 1,
            }
            self._set_status(item, f"NRC 0x{nrc:02X}", COLORS['error'])
            cell = self._table.item(self._rows[item], self.COL_STATUS)
            if cell is not None:
                cell.setToolTip(f"Resposta negativa da ECU 0x{src:03X}:\n"
                                f"{res['nrc_name']}")
            self._status_msg(
                f"{self._item_label(item)} {self._item_name(item)}: recusado "
                f"pela ECU 0x{src:03X} — {res['nrc_name']}", COLORS['error'])

    def _update_item(self, item: tuple, src: int, value,
                     data: bytes, first_frame: bytes, frames: int):
        """Atualiza a linha do item com uma resposta positiva e registra tudo."""
        row = self._rows.get(item)
        if row is None:
            return

        # Encerra o request pendente, se esta resposta é a dele.
        if self._inflight is not None and self._inflight["item"] == item:
            self._inflight = None

        srcs = self._sources.setdefault(item, set())
        srcs.add(src)

        self._record_response(item, src, value, data, first_frame, frames)

        # Valor: para DID sem escala conhecida mostramos o bruto em decimal,
        # com "~" quando o número vem de uma hipótese de conversão. Itens de
        # TEXTO (chassi) exibem a string decodificada como está.
        cell = self._table.item(row, self.COL_VALOR)
        if cell is not None:
            if isinstance(value, str):
                cell.setText(value or "—")
                cell.setForeground(QColor(COLORS['success']))
                cell.setToolTip(self._item_conversion(item))
            elif value is None:
                cell.setText(str(raw_to_int(data)) if data else "—")
                cell.setForeground(QColor(COLORS['warning']))
                cell.setToolTip("Sem escala conhecida — valor BRUTO em decimal.")
            else:
                txt = self._fmt_num(value)
                if item[0] == KIND_DID:
                    txt = "~" + txt      # sinaliza conversão hipotética
                    cell.setToolTip(self._item_conversion(item))
                cell.setText(txt)
                cell.setForeground(QColor(COLORS['success']))

        # Bytes crus do dado — é o que a montadora pede para validação.
        bruto = self._table.item(row, self.COL_BRUTO)
        if bruto is not None:
            bruto.setText(format_hex(data))
            if frames > 1:
                bruto.setToolTip(f"Resposta remontada de {frames} quadros CAN "
                                 f"(ISO-TP multi-frame).")

        # Coluna Fonte: qual ECU respondeu (e aviso se houver mais de uma).
        fonte = self._table.item(row, self.COL_FONTE)
        if fonte is not None:
            if len(srcs) > 1:
                lista = ", ".join(f"{self._ecu_label(s)} (0x{s:03X})"
                                  for s in sorted(srcs))
                fonte.setText(f"⚠ {len(srcs)} módulos: {lista}")
                fonte.setForeground(QColor(COLORS['warning']))
                fonte.setToolTip(
                    "Vários módulos respondem este item com valores próprios.\n"
                    "O valor exibido é o da última resposta recebida.\n"
                    "Escolha uma ECU específica no seletor 'ECU alvo' para\n"
                    "obter uma leitura sem ambiguidade.")
            else:
                # Mostra o número DO MÓDULO junto da ID de resposta: o
                # seletor "ECU alvo" fala em ECU 1..8, a resposta chega em
                # 0x7E8+n, e sem relacionar os dois o operador não liga uma
                # coisa à outra.
                fonte.setText(f"{self._ecu_label(src)} · 0x{src:03X}")
                fonte.setForeground(QColor(COLORS['text_muted']))
                fonte.setToolTip(
                    f"{ecu_name(src)}\n"
                    f"Escuta em 0x{OBD_REQUEST_PHYSICAL_BASE + (src - OBD_RESP_MIN):03X}, "
                    f"responde em 0x{src:03X} — é o mesmo módulo.")

        if len(srcs) > 1:
            self._set_status(item, "⚠ conflito", COLORS['warning'])
        else:
            self._set_status(item, "OK" if frames == 1 else f"OK ({frames}q)",
                             COLORS['success'])

    def _set_status(self, item: tuple, text: str, color: str):
        """Atualiza a coluna Status de um item."""
        row = self._rows.get(item)
        if row is None:
            return
        cell = self._table.item(row, self.COL_STATUS)
        if cell:
            cell.setText(text)
            cell.setForeground(QColor(color))

    def _status_msg(self, text: str, color: str = None):
        """Escreve na barra de status da aba."""
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(
            f"color: {color or COLORS['text_muted']}; font-size: 12px;")

    # ════════════════════════════════════════════════════════════════════════
    #  Registro do tráfego (matéria-prima da documentação)
    # ════════════════════════════════════════════════════════════════════════

    def _record_request(self, item: tuple, can_id: int, data: bytes):
        """
        Anota um request que FOI transmitido com sucesso.

        A chave inclui a ID usada (0x7DF ou 0x7E0+n) porque o operador pode
        trocar de endereçamento no meio da sessão — e a documentação deve
        mostrar exatamente como cada item foi perguntado.
        """
        now = time.time()
        if self._doc_start == 0.0:
            self._doc_start = now
        key = (item[0], item[1], can_id)
        rec = self._doc_req.get(key)
        if rec is None:
            self._doc_req[key] = {"count": 1, "data": bytes(data), "first": now}
        else:
            rec["count"] += 1

    def _record_response(self, item: tuple, src: int, value,
                         data: bytes, first_frame: bytes, frames: int):
        """
        Acumula uma resposta no registro da documentação.

        Uma entrada por (tipo, número, ECU): em broadcast o mesmo item volta de
        módulos diferentes, e cada módulo tem a SUA faixa de valores. Guardamos
        contagem, mínimo/máximo, último valor, os bytes de dado e o primeiro
        quadro cru — é exatamente o conjunto que a montadora pediu para
        validação (canal, payload TX, payload RX, resposta obtida).
        """
        now = time.time()
        key = (item[0], item[1], src)
        # Texto (chassi) não tem mínimo/máximo — as colunas ficam vazias no
        # relatório em vez de repetir a string três vezes.
        num = value if (isinstance(value, (int, float))
                        and not isinstance(value, bool)) else None
        rec = self._doc_obs.get(key)
        if rec is None:
            self._doc_obs[key] = {
                "count": 1, "first": now, "last": now,
                "min": num, "max": num, "last_value": value,
                "data": bytes(data), "raw_first": bytes(first_frame),
                "raw_last": bytes(first_frame), "frames": frames,
                "name": self._item_name(item), "unit": self._item_unit(item),
            }
        else:
            rec["count"] += 1
            rec["last"] = now
            rec["last_value"] = value
            rec["data"] = bytes(data)
            rec["raw_last"] = bytes(first_frame)
            rec["frames"] = frames
            # Mínimo/máximo só fazem sentido para número: um chassi não tem
            # faixa, e comparar strings aqui produziria lixo no relatório.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if isinstance(rec["min"], (int, float)):
                    rec["min"] = min(rec["min"], value)
                    rec["max"] = max(rec["max"], value)
                else:
                    rec["min"] = rec["max"] = value

    # ════════════════════════════════════════════════════════════════════════
    #  Exportação da documentação
    # ════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _hex_bytes(data: bytes) -> str:
        """Bytes em hexadecimal separados por espaço (ex.: '02 01 0C 55 ...')."""
        return " ".join(f"{b:02X}" for b in data) if data else "—"

    @staticmethod
    def _fmt_num(v) -> str:
        """
        Número legível: sem casas decimais quando é praticamente inteiro.

        Valores de TEXTO (chassi) passam direto — a mesma célula do relatório
        recebe tanto número quanto string.
        """
        if v is None:
            return "—"
        if isinstance(v, str):
            return v or "—"
        return f"{v:.0f}" if abs(v - round(v)) < 0.005 else f"{v:.2f}"

    @staticmethod
    def _fmt_time(ts: float) -> str:
        """Hora do relógio (HH:MM:SS) a partir de um time.time()."""
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"

    def _request_ids_for(self, kind: str, num: int) -> str:
        """IDs de request usadas para um item (pode haver mais de uma)."""
        ids = sorted({cid for (k, n, cid) in self._doc_req
                      if k == kind and n == num})
        return ", ".join(f"0x{c:03X}" for c in ids) if ids else "—"

    def _request_bytes_for(self, kind: str, num: int) -> bytes:
        """Um exemplo do quadro de request transmitido para o item."""
        for (k, n, _), rec in self._doc_req.items():
            if k == kind and n == num:
                return rec["data"]
        return b""

    def _doc_summary(self) -> list[tuple[str, str]]:
        """Pares (rótulo, valor) que descrevem a sessão de leitura."""
        total_req = sum(r["count"] for r in self._doc_req.values())
        total_resp = sum(r["count"] for r in self._doc_obs.values())
        itens_req = {(k, n) for (k, n, _) in self._doc_req}
        itens_resp = {(k, n) for (k, n, _) in self._doc_obs}
        ecus = sorted({s for (_, _, s) in self._doc_obs})
        pids_resp = sum(1 for (k, _) in itens_resp if k == KIND_PID)
        dids_resp = sum(1 for (k, _) in itens_resp if k == KIND_DID)

        # Origem dos dados (hardware real, simulação ou nada conectado).
        if self._bus is None or not self._bus.is_connected:
            fonte = "Sem conexão ativa no momento da exportação"
        elif self._bus.is_simulation:
            fonte = "Modo simulação interna (respostas sintéticas)"
        else:
            fonte = (f"Hardware IXXAT — canal {self._bus.channel} @ "
                     f"{self._bus.bitrate} bps")

        if self._bus is None:
            tx = "—"
        elif self._bus.is_simulation:
            tx = "N/A (simulação)"
        elif self._bus.is_listen_only:
            tx = "LISTEN-ONLY (transmissão bloqueada)"
        else:
            tx = "Modo normal (transmissão permitida)"

        dur = "—"
        if self._doc_start:
            secs = int(time.time() - self._doc_start)
            dur = f"{secs // 60} min {secs % 60} s"

        return [
            ("Documento", "Protocolo de diagnóstico observado no barramento"),
            ("Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
            ("Programa", "IxxatInterface v7 — aba OBD-II / UDS"),
            ("Normas",
             "SAE J1979 modo 01 (PIDs) e ISO 14229 serviço 0x22 (DIDs), "
             "sobre ISO 15765-4 (CAN 11 bits)"),
            ("Canal de diagnóstico",
             f"request 0x{OBD_REQUEST_FUNCTIONAL:03X} (funcional) / "
             f"0x{OBD_REQUEST_PHYSICAL_BASE:03X}-"
             f"0x{OBD_REQUEST_PHYSICAL_BASE + 7:03X} (físico)  →  "
             f"resposta 0x{OBD_RESP_MIN:03X}-0x{OBD_RESP_MAX:03X}"),
            ("Origem dos dados", fonte),
            ("Modo de transmissão", tx),
            ("ECU alvo selecionada", self._cmb_ecu.currentText()),
            ("Início da leitura", self._fmt_time(self._doc_start)),
            ("Duração da leitura", dur),
            ("Requests transmitidos",
             f"{total_req} (em {len(itens_req)} item(ns) distintos)"),
            ("Respostas recebidas", f"{total_resp}"),
            ("PIDs com resposta", f"{pids_resp}"),
            ("DIDs com resposta", f"{dids_resp}"),
            ("Itens sem resposta", f"{len(itens_req - itens_resp)}"),
            ("Respostas negativas (NRC)", f"{len(self._doc_nrc)}"),
            ("Flow Control transmitidos",
             f"{self._doc_fc_sent} (respostas multi-frame)"),
            ("Requests sem resposta no prazo", f"{self._doc_timeouts}"),
            ("ECUs que responderam",
             ", ".join(f"0x{s:03X} ({ecu_name(s)})" for s in ecus) or "—"),
            ("Serviços transmitidos",
             "0x01 (OBD-II modo 01), 0x09 (OBD-II informações do veículo) e "
             "0x22 (UDS Read Data By Identifier) — todos de LEITURA"),
            ("Escrita no barramento",
             "NENHUMA. O programa não transmite escrita de DID (0x2E), "
             "atuação (0x2F), rotina (0x31), reset (0x11), apagamento de "
             "falhas (0x14) nem troca de sessão (0x10/0x3E) — a lista branca "
             "de transmissão recusa esses serviços no barramento"),
        ]

    # Cabeçalhos das tabelas do documento (compartilhados por Excel e TXT).
    DOC_OBS_HEADERS = [
        "Tipo", "PID/DID", "Nº (dec)", "Sinal", "ECU", "Nome da ECU",
        "Request (ID)", "Request (bytes)", "Resposta (1º quadro)", "Quadros",
        "Dados (bruto hex)", "Bruto (dec)", "Conversão", "Unidade",
        "Último valor", "Mínimo", "Máximo", "Respostas",
        "1ª resposta", "Última resposta",
    ]
    DOC_NORESP_HEADERS = [
        "Tipo", "PID/DID", "Nº (dec)", "Sinal", "Request (ID)",
        "Request (bytes)", "Requests enviados", "Resposta negativa (NRC)",
        "Interpretação",
    ]
    DOC_DB_HEADERS = [
        "Modo", "PID (hex)", "PID (dec)", "Parâmetro", "Bytes de dados",
        "Unidade", "Fórmula (A, B = bytes de dados)", "Faixa típica",
        "Request (broadcast)", "Observado nesta sessão",
    ]
    DOC_DID_HEADERS = [
        "DID", "Sinal", "Unidade", "Request (bytes)",
        "Conversão (hipótese)", "Fonte da informação", "Prioritário",
        "Observado nesta sessão",
    ]
    DOC_LIB_HEADERS = ["#", "Linha (enviar ao equipamento)", "Comentário"]

    def _rows_observed(self) -> list[list]:
        """Uma linha por (item, ECU) efetivamente observado no barramento."""
        rows = []
        for key in sorted(self._doc_obs.keys()):
            kind, num, src = key
            rec = self._doc_obs[key]
            item = (kind, num)
            data = rec.get("data", b"")
            rows.append([
                self._item_service(item), self._item_label(item), num,
                rec["name"], f"0x{src:03X}", ecu_name(src),
                self._request_ids_for(kind, num),
                self._hex_bytes(self._request_bytes_for(kind, num)),
                self._hex_bytes(rec.get("raw_last", b"")),
                rec.get("frames", 1),
                self._hex_bytes(data),
                # "Bruto (dec)" não faz sentido para texto: o inteiro de um
                # chassi de 17 bytes seria um número de 41 dígitos.
                ("—" if isinstance(rec["last_value"], str)
                 else (raw_to_int(data) if data else "—")),
                self._item_conversion(item).replace("\n", " "),
                rec["unit"],
                self._fmt_num(rec["last_value"]),
                self._fmt_num(rec["min"]), self._fmt_num(rec["max"]),
                rec["count"],
                self._fmt_time(rec["first"]), self._fmt_time(rec["last"]),
            ])
        return rows

    def _rows_no_response(self) -> list[list]:
        """
        Itens perguntados que NÃO trouxeram valor.

        Distingue dois casos bem diferentes para o relatório: silêncio total
        (provavelmente não suportado) e recusa explícita da ECU, com o código
        negativo — que é informação de campo valiosa.
        """
        respondidos = {(k, n) for (k, n, _) in self._doc_obs}
        rows = []
        for (kind, num) in sorted({(k, n) for (k, n, _) in self._doc_req}
                                  - respondidos):
            item = (kind, num)
            n_req = sum(r["count"] for (k, nn, _), r in self._doc_req.items()
                        if k == kind and nn == num)
            nrc = self._doc_nrc.get(item)
            if nrc:
                nrc_txt = f"7F 22 {nrc['nrc']:02X} — {nrc['name']}"
                interp = ("A ECU recebeu e RECUSOU a consulta. Veja o código: "
                          "0x31 = não existe neste módulo; 0x33 = exige acesso "
                          "de segurança; 0x7F = exigiria sessão estendida.")
            else:
                nrc_txt = "—"
                interp = ("Silêncio total: nenhuma ECU respondeu. Provavelmente "
                          "não suportado, ou está em outra ECU/canal.")
            rows.append([
                self._item_service(item), self._item_label(item), num,
                self._item_name(item), self._request_ids_for(kind, num),
                self._hex_bytes(self._request_bytes_for(kind, num)),
                n_req, nrc_txt, interp,
            ])
        return rows

    def _rows_database(self) -> list[list]:
        """
        Referência do banco de PIDs implementado no programa.

        Inclui os dois serviços de leitura do OBD-II: modo 01 (dados atuais,
        numéricos) e modo 09 (informações do veículo, em texto).
        """
        resp_01 = {n for (k, n, _) in self._doc_obs if k == KIND_PID}
        resp_09 = {n for (k, n, _) in self._doc_obs if k == KIND_PID9}
        rows = []
        for pid in sorted(PID_DATABASE):
            info = PID_DATABASE[pid]
            _, req = build_request(pid)     # exemplo com ID funcional (0x7DF)
            rows.append([
                "01", f"0x{pid:02X}", pid, info.name, info.n_bytes, info.unit,
                formula_text(pid),
                f"{self._fmt_num(info.min_val)} a {self._fmt_num(info.max_val)} "
                f"{info.unit}".strip(),
                f"0x{OBD_REQUEST_FUNCTIONAL:03X}: {self._hex_bytes(req)}",
                "sim" if pid in resp_01 else "—",
            ])
        for pid in sorted(MODE9_PIDS):
            nome, tipo = MODE9_PIDS[pid]
            _, req = build_mode09_request(pid)
            rows.append([
                "09", f"0x{pid:02X}", pid, nome, "17 (texto)", "",
                "texto ASCII — sem conversão numérica", "—",
                f"0x{OBD_REQUEST_FUNCTIONAL:03X}: {self._hex_bytes(req)}",
                "sim" if pid in resp_09 else "—",
            ])
        return rows

    def _rows_did_database(self) -> list[list]:
        """Referência dos DIDs UDS cadastrados no programa."""
        respondidos = {n for (k, n, _) in self._doc_obs if k == KIND_DID}
        rows = []
        for did in sorted(DID_DATABASE):
            info = DID_DATABASE[did]
            _, req = build_read_did_request(did)
            rows.append([
                f"0x{did:04X}", info.name, info.unit,
                f"0x{OBD_REQUEST_FUNCTIONAL:03X}: {self._hex_bytes(req)}",
                info.hypothesis or "desconhecida",
                info.source or "—",
                "sim" if did in PRIORITY_DIDS else "—",
                "sim" if did in respondidos else "—",
            ])
        return rows

    def _library_entries(self) -> tuple[list[tuple[int, int, bool]], bool]:
        """
        Escolhe quais PIDs entram na biblioteca CAN.

        SÓ PIDs: as linhas VOBD/VS do equipamento descrevem consulta de PID
        OBD-II. DIDs UDS não têm representação nesse formato, então ficam de
        fora — gerar linha inválida seria pior que omitir.

        Preferimos os PIDs que REALMENTE responderam; se nada respondeu,
        caímos nos PIDs marcados na tabela, assumindo a ECU 1 (0x7E8), e
        sinalizamos que não estão confirmados.

        Devolve (entradas, confirmadas).
        """
        obs = [(n, src) for (k, n, src) in sorted(self._doc_obs) if k == KIND_PID]
        if obs:
            return [(n, src, True) for (n, src) in obs], True
        return [(pid, OBD_RESP_MIN, False) for pid in self._active_pids()], False

    def _rows_library(self) -> list[list]:
        """Linhas da biblioteca CAN numeradas, prontas para a planilha/TXT."""
        entries, _ = self._library_entries()
        # O baudrate da linha VS19_ENA vem da conexão real; 500 kbps é o padrão
        # OBD-II e serve de reserva quando não há conexão registrada.
        bitrate = self._bus.bitrate if (self._bus and self._bus.bitrate) else 500000
        linhas = build_can_library(entries, baudrate=bitrate,
                                   model=self._doc_model)
        return [[i, linha, coment]
                for i, (linha, coment) in enumerate(linhas, start=1)]

    @pyqtSlot()
    def _export_doc(self):
        """
        Gera o arquivo de documentação do protocolo desta sessão.

        O formato é escolhido pela extensão no diálogo de salvamento:
          .xlsx → planilha com 7 abas (resumo, observados, sem resposta,
                  biblioteca CAN, protocolo, banco de PIDs, banco de DIDs);
          .txt  → mesmo conteúdo em texto puro (não depende do openpyxl).
        """
        # Sem tráfego registrado o arquivo seria apenas a referência das normas;
        # confirmamos com o usuário para não gerar um documento vazio por engano.
        if not self._doc_req:
            resp = QMessageBox.question(
                self, "Nenhuma leitura registrada",
                "Nenhum request foi transmitido nesta sessão, então não há "
                "tráfego observado para documentar.\n\n"
                "Deseja exportar apenas a referência dos protocolos e os bancos "
                "de PIDs/DIDs do programa?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if resp != QMessageBox.Yes:
                return

        # Modelo do veículo — vai no registro VSRT da folha "Biblioteca CAN".
        modelo, ok = QInputDialog.getText(
            self, "Modelo do veículo",
            "Modelo do veículo (usado na folha 'Biblioteca CAN'):",
            text=self._doc_model)
        if ok and modelo.strip():
            self._doc_model = modelo.strip()

        # Sugere a pasta Documents/IxxatInterface (mesma dos logs de sessão).
        nome = f"protocolo_diagnostico_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        try:
            os.makedirs(DOC_DIR, exist_ok=True)
            sugerido = os.path.join(DOC_DIR, nome)
        except Exception:
            sugerido = nome

        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar Documentação de Diagnóstico", sugerido,
            "Excel (*.xlsx);;Texto (*.txt)"
        )
        if not path:
            return

        try:
            if path.lower().endswith(".txt"):
                self._write_doc_txt(path)
            else:
                if not path.lower().endswith(".xlsx"):
                    path += ".xlsx"
                self._write_doc_xlsx(path)
        except PermissionError:
            QMessageBox.critical(
                self, "Arquivo em uso",
                "Não foi possível salvar.\nFeche o arquivo e tente novamente.")
            return
        except ImportError:
            QMessageBox.critical(
                self, "openpyxl não instalado",
                "Para exportar em Excel é necessário o módulo 'openpyxl'.\n\n"
                "Execute no CMD:  pip install openpyxl\n\n"
                "Alternativa: salve com a extensão .txt.")
            return
        except Exception as e:
            QMessageBox.critical(self, "Erro ao exportar", str(e))
            return

        QMessageBox.information(
            self, "Documentação gerada",
            f"Arquivo salvo em:\n{path}\n\n"
            f"{len(self._doc_obs)} par(es) item/ECU documentado(s) e "
            f"{len(self._doc_nrc)} resposta(s) negativa(s) registrada(s).")

    def _write_doc_xlsx(self, path: str):
        """Escreve a documentação como planilha Excel."""
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        # Estilos reutilizados em todas as abas.
        hdr_fill = PatternFill("solid", fgColor="C0C0C0")
        ttl_fill = PatternFill("solid", fgColor="1F3864")
        bold = Font(bold=True)
        ttl_font = Font(bold=True, color="FFFFFF", size=12)
        thin = Side(border_style="thin", color="808080")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        left = Alignment(horizontal="left", vertical="center")
        wrap = Alignment(horizontal="left", vertical="top", wrap_text=True)
        center = Alignment(horizontal="center", vertical="center")

        def titulo(ws, texto: str, n_cols: int):
            """Faixa de título no topo da aba, mesclada nas colunas da tabela."""
            c = ws.cell(row=1, column=1, value=texto)
            c.fill, c.font, c.alignment = ttl_fill, ttl_font, left
            ws.merge_cells(start_row=1, start_column=1,
                           end_row=1, end_column=max(1, n_cols))

        def tabela(ws, headers: list, rows: list, start_row: int,
                   widths: list = None):
            """Cabeçalho + linhas com borda e zebra; devolve a próxima linha."""
            for c, h in enumerate(headers, start=1):
                cell = ws.cell(row=start_row, column=c, value=h)
                cell.fill, cell.font = hdr_fill, bold
                cell.border, cell.alignment = border, center
            zebra = PatternFill("solid", fgColor="F2F2F2")
            for i, row in enumerate(rows):
                r = start_row + 1 + i
                for c, val in enumerate(row, start=1):
                    cell = ws.cell(row=r, column=c, value=val)
                    cell.border = border
                    cell.alignment = left
                    if i % 2:
                        cell.fill = zebra
            if widths:
                for c, w in enumerate(widths, start=1):
                    ws.column_dimensions[get_column_letter(c)].width = w
            ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
            return start_row + 1 + len(rows)

        wb = Workbook()

        # ── Aba 1: Resumo da sessão ──────────────────────────────────────────
        ws = wb.active
        ws.title = "Resumo"
        titulo(ws, "DOCUMENTAÇÃO DO DIAGNÓSTICO — RESUMO DA SESSÃO", 2)
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 95
        r = 3
        for label, value in self._doc_summary():
            a = ws.cell(row=r, column=1, value=label)
            a.fill, a.font, a.border, a.alignment = hdr_fill, bold, border, left
            b = ws.cell(row=r, column=2, value=value)
            b.border, b.alignment = border, wrap
            r += 1

        # ── Aba 2: tráfego observado ─────────────────────────────────────────
        ws = wb.create_sheet("Sinais Observados")
        titulo(ws, "PIDs e DIDs RESPONDIDOS PELO VEÍCULO "
                   "(tráfego real capturado)", len(self.DOC_OBS_HEADERS))
        tabela(ws, self.DOC_OBS_HEADERS, self._rows_observed(), 3,
               widths=[8, 12, 9, 30, 8, 24, 14, 26, 26, 8, 22, 12, 34, 9,
                       13, 10, 10, 10, 12, 14])

        # ── Aba 3: consultados sem valor ─────────────────────────────────────
        ws = wb.create_sheet("Sem Resposta")
        titulo(ws, "CONSULTADOS SEM VALOR (silêncio ou recusa da ECU)",
               len(self.DOC_NORESP_HEADERS))
        tabela(ws, self.DOC_NORESP_HEADERS, self._rows_no_response(), 3,
               widths=[8, 12, 9, 32, 14, 26, 16, 34, 70])

        # ── Aba 4: biblioteca CAN pronta para o equipamento ──────────────────
        ws = wb.create_sheet("Biblioteca CAN")
        titulo(ws, "BIBLIOTECA CAN (VIRLOC) — CONSULTA DE PIDs OBD-II",
               len(self.DOC_LIB_HEADERS))
        entries, confirmadas = self._library_entries()
        nota = ("Linhas prontas para envio ao equipamento, na ordem abaixo. "
                "Os filtros foram gerados a partir dos PIDs que RESPONDERAM "
                "neste veículo."
                if confirmadas else
                "ATENÇÃO: nenhum PID respondeu nesta sessão — as linhas abaixo "
                "usam os PIDs MARCADOS na tabela e assumem a ECU 1 (0x7E8). "
                "Confirme fazendo uma leitura antes de aplicar no equipamento.")
        nota += (" Esta folha cobre apenas PIDs OBD-II: as linhas VOBD/VS do "
                 "equipamento não representam consulta UDS $22, então os DIDs "
                 "não entram aqui (veja a folha 'Banco de DIDs').")
        if len(entries) > LIB_MAX_FILTERS:
            nota += (f" Apenas os {LIB_MAX_FILTERS} primeiros sinais entraram: "
                     f"o equipamento só tem {LIB_MAX_FILTERS} filtros "
                     f"(VS1900..VS19{LIB_MAX_FILTERS - 1:02d}).")
        c = ws.cell(row=2, column=1, value=nota)
        c.alignment = wrap
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=3)
        ws.row_dimensions[2].height = 44
        prox = tabela(ws, self.DOC_LIB_HEADERS, self._rows_library(), 4,
                      widths=[5, 46, 92])
        # Legenda campo a campo do filtro VS, logo abaixo das linhas.
        prox += 1
        c = ws.cell(row=prox, column=1,
                    value="CAMPOS DO FILTRO:  "
                          ">VS19ff,iiiii,ppp,11,cc,4,n,mmmmmmmm,0,3<")
        c.font = bold
        ws.merge_cells(start_row=prox, start_column=1,
                       end_row=prox, end_column=3)
        tabela(ws, ["", "Campo", "Significado"],
               [["", campo, texto] for campo, texto in VS_FIELD_DOC], prox + 1)
        ws.freeze_panes = None

        # ── Aba 5: referência dos protocolos ─────────────────────────────────
        ws = wb.create_sheet("Protocolo")
        titulo(ws, "COMO O DIÁLOGO DE DIAGNÓSTICO FUNCIONA "
                   "(referência das normas)", 3)
        ws.column_dimensions["A"].width = 14
        ws.column_dimensions["B"].width = 24
        ws.column_dimensions["C"].width = 96
        r = 3
        for servico, notas in (("OBD-II 01", PROTOCOL_NOTES),
                               ("UDS $22", UDS_PROTOCOL_NOTES)):
            for topico, texto in notas:
                a = ws.cell(row=r, column=1, value=servico)
                a.fill, a.font, a.border, a.alignment = (hdr_fill, bold,
                                                         border, left)
                b = ws.cell(row=r, column=2, value=topico)
                b.fill, b.font, b.border, b.alignment = (hdr_fill, bold,
                                                         border, left)
                c = ws.cell(row=r, column=3, value=texto)
                c.border, c.alignment = border, wrap
                ws.row_dimensions[r].height = 32
                r += 1
        # Tabela de IDs de resposta por módulo.
        r += 1
        ecu_rows = [[f"0x{rid:03X}", nome,
                     f"0x{OBD_REQUEST_PHYSICAL_BASE + (rid - OBD_RESP_MIN):03X}"]
                    for rid, nome in sorted(ECU_NAMES.items())]
        tabela(ws, ["ID de resposta", "Módulo", "ID de request físico"],
               ecu_rows, r)
        ws.freeze_panes = None

        # ── Aba 6: banco de PIDs ─────────────────────────────────────────────
        ws = wb.create_sheet("Banco de PIDs")
        titulo(ws, "PIDs IMPLEMENTADOS NO PROGRAMA "
                   "(SAE J1979 — modo 01 e modo 09)",
               len(self.DOC_DB_HEADERS))
        tabela(ws, self.DOC_DB_HEADERS, self._rows_database(), 3,
               widths=[7, 10, 9, 32, 12, 9, 30, 22, 34, 12])

        # ── Aba 7: banco de DIDs ─────────────────────────────────────────────
        ws = wb.create_sheet("Banco de DIDs")
        titulo(ws, "DIDs IMPLEMENTADOS NO PROGRAMA (UDS $22)",
               len(self.DOC_DID_HEADERS))
        c = ws.cell(row=2, column=1,
                    value="As conversões abaixo são HIPÓTESES: a montadora "
                          "informou a unidade, não a escala. Confirme com "
                          "leitura real — o valor bruto está na folha "
                          "'Sinais Observados'.")
        c.alignment = wrap
        ws.merge_cells(start_row=2, start_column=1, end_row=2,
                       end_column=len(self.DOC_DID_HEADERS))
        ws.row_dimensions[2].height = 30
        tabela(ws, self.DOC_DID_HEADERS, self._rows_did_database(), 4,
               widths=[10, 34, 9, 30, 30, 18, 12, 12])

        wb.save(path)

    def _write_doc_txt(self, path: str):
        """Escreve a mesma documentação em texto puro (sem dependências)."""
        def tabela(headers: list, rows: list) -> list[str]:
            """Monta uma tabela de largura fixa a partir das linhas."""
            if not rows:
                return ["  (nenhum registro)"]
            cols = [[str(h)] + [str(r[i]) for r in rows]
                    for i, h in enumerate(headers)]
            larg = [min(max(len(v) for v in col), 44) for col in cols]

            def linha(vals):
                return "  " + " | ".join(
                    str(v)[:w].ljust(w) for v, w in zip(vals, larg))

            out = [linha(headers), "  " + "-+-".join("-" * w for w in larg)]
            out += [linha(r) for r in rows]
            return out

        def paragrafo(texto: str, recuo: str = "      ",
                      largura: int = 86) -> list[str]:
            """Quebra um texto longo em linhas, para o arquivo ficar legível."""
            out, atual = [], ""
            for p in texto.split():
                if len(atual) + len(p) + 1 > largura:
                    out.append(recuo + atual)
                    atual = p
                else:
                    atual = f"{atual} {p}".strip()
            if atual:
                out.append(recuo + atual)
            return out

        L: list[str] = []
        L.append("=" * 100)
        L.append("DOCUMENTAÇÃO DO DIAGNÓSTICO (OBD-II modo 01 + UDS $22) — "
                 "IxxatInterface v7")
        L.append("=" * 100)
        L.append("")
        L.append("1) RESUMO DA SESSÃO")
        L.append("-" * 100)
        for label, value in self._doc_summary():
            L.append(f"  {label + ':':<32} {value[:64]}")
            if len(value) > 64:
                L += paragrafo(value[64:], recuo=" " * 35)
        L.append("")
        L.append("2) PIDs e DIDs RESPONDIDOS PELO VEÍCULO (tráfego real)")
        L.append("-" * 100)
        L += tabela(self.DOC_OBS_HEADERS, self._rows_observed())
        L.append("")
        L.append("3) CONSULTADOS SEM VALOR (silêncio ou recusa da ECU)")
        L.append("-" * 100)
        L += tabela(self.DOC_NORESP_HEADERS, self._rows_no_response())
        L.append("")
        L.append("4) BIBLIOTECA CAN (VIRLOC) — CONSULTA DE PIDs OBD-II")
        L.append("-" * 100)
        entries, confirmadas = self._library_entries()
        if not confirmadas:
            L.append("  ATENÇÃO: nenhum PID respondeu nesta sessão. As linhas abaixo usam os")
            L.append("  PIDs MARCADOS na tabela e assumem a ECU 1 (0x7E8) — confirme com uma")
            L.append("  leitura antes de aplicar no equipamento.")
        L.append("  OBS: esta seção cobre apenas PIDs OBD-II; DIDs UDS não têm")
        L.append("  representação no formato VOBD/VS do equipamento.")
        if len(entries) > LIB_MAX_FILTERS:
            L.append(f"  OBS: só os {LIB_MAX_FILTERS} primeiros sinais entraram "
                     f"(limite de filtros do equipamento).")
        L.append("")
        # Formato igual ao do arquivo de biblioteca: >LINHA<  // comentário
        lib = self._rows_library()
        larg = max((len(r[1]) for r in lib), default=0)
        for _, linha, coment in lib:
            L.append(f"  {linha.ljust(larg)}  // {coment}")
        L.append("")
        L.append("  CAMPOS DO FILTRO:  >VS19ff,iiiii,ppp,11,cc,4,n,mmmmmmmm,0,3<")
        for campo, texto in VS_FIELD_DOC:
            L.append(f"    {campo:<10} {texto}")
        L.append("")
        L.append("5) COMO O DIÁLOGO DE DIAGNÓSTICO FUNCIONA (normas)")
        L.append("-" * 100)
        for servico, notas in (("OBD-II modo 01 (SAE J1979)", PROTOCOL_NOTES),
                               ("UDS $22 (ISO 14229)", UDS_PROTOCOL_NOTES)):
            L.append("")
            L.append(f"  ### {servico}")
            for topico, texto in notas:
                L.append(f"  • {topico}:")
                L += paragrafo(texto)
        L.append("")
        L.append("  IDs de resposta por módulo:")
        L += tabela(["ID de resposta", "Módulo", "ID de request físico"],
                    [[f"0x{rid:03X}", nome,
                      f"0x{OBD_REQUEST_PHYSICAL_BASE + (rid - OBD_RESP_MIN):03X}"]
                     for rid, nome in sorted(ECU_NAMES.items())])
        L.append("")
        L.append("6) PIDs IMPLEMENTADOS NO PROGRAMA (SAE J1979 — modos 01 e 09)")
        L.append("-" * 100)
        L += tabela(self.DOC_DB_HEADERS, self._rows_database())
        L.append("")
        L.append("7) DIDs IMPLEMENTADOS NO PROGRAMA (UDS $22)")
        L.append("-" * 100)
        L.append("  As conversões são HIPÓTESES: a montadora informou a unidade,")
        L.append("  não a escala. Confirme com leitura real (valor bruto na seção 2).")
        L.append("")
        L += tabela(self.DOC_DID_HEADERS, self._rows_did_database())
        L.append("")
        L.append("=" * 100)
        L.append(f"Canal de diagnóstico: request 0x{OBD_REQUEST_FUNCTIONAL:03X} / "
                 f"0x{OBD_REQUEST_PHYSICAL_BASE:03X}-0x{OBD_REQUEST_PHYSICAL_BASE + 7:03X}"
                 f"  |  resposta 0x{OBD_RESP_MIN:03X}-0x{OBD_RESP_MAX:03X}")
        L.append("Serviços transmitidos: 0x01 (OBD-II modo 01) e 0x22 (UDS "
                 "leitura de DID) — somente leitura.")
        L.append("=" * 100)

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
