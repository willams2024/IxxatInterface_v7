"""
Aba "OBD-II" (PyQt5) — leitura de PIDs padrão SAE J1979 sobre CAN.

DIFERENÇA FUNDAMENTAL para as outras abas:
    O Monitor CAN e a Descoberta de Sinais são PASSIVOS (só escutam o que as
    ECUs J1939 transmitem sozinhas). O OBD-II é pergunta/resposta: esta aba
    precisa TRANSMITIR um request para cada PID e aguardar a resposta da ECU.

    Por isso, ela exige que a conexão esteja com "Listen-Only" DESMARCADO.
    Em modo Simulação, o CANBus fabrica respostas sintéticas, permitindo
    testar a aba sem hardware.

Fluxo:
    1) O usuário marca os PIDs de interesse na tabela.
    2) "Ler uma vez" envia um request por PID marcado.
    3) "Stream" liga um timer que fica consultando os PIDs em rodízio.
    4) As respostas chegam por on_message() (thread do CAN) e são acumuladas;
       um timer da GUI atualiza a tabela (nunca mexemos em widgets fora da
       thread da interface).
"""

from collections import deque

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QMessageBox,
)

from core.can_bus import CANMessage
from core.obd2 import PID_DATABASE, build_request, parse_response, ecu_name
from gui.styles import COLORS


class OBD2Tab(QWidget):
    """Aba de leitura de PIDs OBD-II (modo 01 — dados atuais)."""

    # Colunas da tabela
    (COL_ATIVO, COL_PID, COL_NOME, COL_VALOR,
     COL_UNID, COL_FONTE, COL_STATUS) = range(7)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bus = None
        # Fila de respostas recebidas na thread do CAN; consumida pelo timer
        # da GUI. deque com limite evita crescimento sem controle.
        self._pending: deque = deque(maxlen=2000)
        # Mapeia PID -> índice da linha na tabela (para atualizar valores).
        self._pid_rows: dict[int, int] = {}
        # Mapeia PID -> conjunto de IDs de ECU que já responderam aquele PID.
        # Com request funcional (0x7DF) VÁRIAS ECUs podem responder o mesmo
        # PID com valores diferentes; guardamos as origens para avisar o
        # operador em vez de sobrescrever silenciosamente a mesma célula.
        self._pid_sources: dict[int, set] = {}
        # Lista de PIDs ativos usada no rodízio do stream, e o índice atual.
        self._poll_order: list[int] = []
        self._poll_idx = 0
        self._streaming = False
        self._setup_ui()

        # Timer que atualiza a tabela com as respostas recebidas (thread GUI).
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._drain_responses)
        self._ui_timer.start(120)

        # Timer do rodízio de requests durante o stream.
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_next)

    def set_bus(self, bus):
        """Recebe a referência do CANBus (usada para transmitir os requests)."""
        self._bus = bus

    # ── Construção da interface ──────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Cabeçalho
        hdr = QHBoxLayout()
        title = QLabel("Leitura OBD-II (SAE J1979 — Modo 01)")
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
        layout.addLayout(hdr)

        # ── Seletor de ECU alvo ───────────────────────────────────────────────
        # Com "Todas" (0x7DF) o request é broadcast e vários módulos respondem
        # o mesmo PID, cada um com seu valor. Endereçar uma ECU específica
        # (0x7E0+n) elimina essa ambiguidade.
        ecu_row = QHBoxLayout()
        ecu_row.addWidget(QLabel("ECU alvo:"))
        self._cmb_ecu = QComboBox()
        self._cmb_ecu.addItem("Todas as ECUs (broadcast 0x7DF)", None)
        for n in range(8):
            self._cmb_ecu.addItem(
                f"Somente ECU {n + 1} (0x{0x7E0 + n:03X})", n)
        self._cmb_ecu.setToolTip(
            "Broadcast: todas as ECUs respondem — o MESMO PID pode voltar com\n"
            "valores diferentes de módulos diferentes (a coluna Fonte mostra a\n"
            "origem e avisa quando há conflito).\n"
            "ECU específica: só aquele módulo responde, sem ambiguidade."
        )
        self._cmb_ecu.setMinimumWidth(260)
        ecu_row.addWidget(self._cmb_ecu)
        ecu_row.addStretch()
        layout.addLayout(ecu_row)

        # Aviso sobre transmissão / listen-only
        self._lbl_warn = QLabel(
            "⚠️  O OBD-II precisa TRANSMITIR requests. Conecte com "
            "'Listen-Only' DESMARCADO (ou use Modo Simulação para testar)."
        )
        self._lbl_warn.setStyleSheet(
            f"color: {COLORS['warning']}; font-size: 11px; padding: 4px;")
        self._lbl_warn.setWordWrap(True)
        layout.addWidget(self._lbl_warn)

        # Barra de status
        self._lbl_status = QLabel("Pronto.")
        self._lbl_status.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 12px;")
        layout.addWidget(self._lbl_status)

        # Tabela de PIDs
        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["Ler", "PID", "Parâmetro", "Valor", "Unidade",
             "Fonte (ECU)", "Status"])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(self.COL_ATIVO,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_PID,    QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_NOME,   QHeaderView.Stretch)
        h.setSectionResizeMode(self.COL_VALOR,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_UNID,   QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_FONTE,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeToContents)
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
        """Cria uma linha para cada PID do banco, ordenada pelo número do PID."""
        self._table.setRowCount(0)
        self._pid_rows.clear()
        for pid in sorted(PID_DATABASE.keys()):
            info = PID_DATABASE[pid]
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._pid_rows[pid] = row

            # Coluna 0: checkbox de seleção (widget próprio, centralizado)
            chk = QCheckBox()
            chk.setChecked(pid in (0x0C, 0x0D, 0x05, 0x11))   # marca os mais usados
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.addWidget(chk)
            hl.setAlignment(Qt.AlignCenter)
            hl.setContentsMargins(0, 0, 0, 0)
            self._table.setCellWidget(row, self.COL_ATIVO, holder)

            for col, text in (
                (self.COL_PID,    f"0x{pid:02X}"),
                (self.COL_NOME,   info.name),
                (self.COL_VALOR,  "—"),
                (self.COL_UNID,   info.unit),
                (self.COL_FONTE,  "—"),
                (self.COL_STATUS, "aguardando"),
            ):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                align = Qt.AlignLeft if col == self.COL_NOME else Qt.AlignCenter
                item.setTextAlignment(align | Qt.AlignVCenter)
                self._table.setItem(row, col, item)

    # ── Helpers de seleção ───────────────────────────────────────────────────

    def _checkbox_at(self, row: int) -> QCheckBox:
        """Devolve o QCheckBox da coluna 'Ler' de uma linha."""
        holder = self._table.cellWidget(row, self.COL_ATIVO)
        return holder.findChild(QCheckBox) if holder else None

    def _set_all_checked(self, checked: bool):
        for row in range(self._table.rowCount()):
            chk = self._checkbox_at(row)
            if chk:
                chk.setChecked(checked)

    def _active_pids(self) -> list[int]:
        """Lista dos PIDs marcados pelo usuário."""
        out = []
        for pid, row in self._pid_rows.items():
            chk = self._checkbox_at(row)
            if chk and chk.isChecked():
                out.append(pid)
        return sorted(out)

    def _target_ecu(self):
        """
        ECU alvo escolhida no combo: None = broadcast (0x7DF), 0..7 = física.
        """
        return self._cmb_ecu.currentData()

    # ── Verificação de pré-condições ─────────────────────────────────────────

    def _check_ready(self) -> bool:
        """Valida que dá para transmitir; explica ao usuário se não der."""
        if self._bus is None or not self._bus.is_connected:
            QMessageBox.warning(self, "Sem conexão",
                                "Conecte ao barramento antes de ler PIDs OBD-II.")
            return False
        # Em hardware real, listen-only impede transmitir.
        if (not self._bus.is_simulation) and self._bus.is_listen_only:
            QMessageBox.warning(
                self, "Listen-Only ativo",
                "O OBD-II precisa TRANSMITIR requests para a ECU responder.\n\n"
                "Desconecte, DESMARQUE a caixa 'Listen-Only' e conecte de novo."
            )
            return False
        if not self._active_pids():
            QMessageBox.information(self, "Nenhum PID marcado",
                                    "Marque ao menos um PID na coluna 'Ler'.")
            return False
        return True

    # ── Envio de requests ────────────────────────────────────────────────────

    @pyqtSlot()
    def _read_once(self):
        """Envia um request para cada PID marcado (uma única rodada)."""
        if not self._check_ready():
            return
        pids = self._active_pids()
        target = self._target_ecu()
        # Zera as origens conhecidas: uma nova leitura recomeça a detecção de
        # conflito entre ECUs do zero.
        self._pid_sources.clear()
        enviados, erro = 0, None
        for pid in pids:
            can_id, data = build_request(pid, target_ecu=target)
            ok, msg = self._bus.send(can_id, data, is_extended=False)
            if ok:
                enviados += 1
                self._set_status(pid, "consultando...", COLORS['warning'])
            else:
                erro = msg
                break
        if erro:
            self._lbl_status.setText(f"Falha ao transmitir: {erro}")
            self._lbl_status.setStyleSheet(f"color: {COLORS['error']}; font-size: 12px;")
        else:
            self._lbl_status.setText(f"{enviados} request(s) enviado(s). Aguardando respostas...")
            self._lbl_status.setStyleSheet(f"color: {COLORS['accent']}; font-size: 12px;")

    @pyqtSlot(bool)
    def _toggle_stream(self, checked: bool):
        """Liga/desliga a consulta contínua em rodízio."""
        if checked:
            if not self._check_ready():
                self._btn_stream.setChecked(False)
                return
            self._poll_order = self._active_pids()
            self._poll_idx = 0
            self._streaming = True
            self._pid_sources.clear()   # recomeça a detecção de conflito
            self._btn_stream.setText("⏸  Parar Stream")
            # ~8 requests/s: rápido o bastante e sem saturar o barramento.
            self._poll_timer.start(125)
            self._lbl_status.setText(
                f"Stream ativo — {len(self._poll_order)} PID(s) em rodízio.")
            self._lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")
        else:
            self._streaming = False
            self._poll_timer.stop()
            self._btn_stream.setText("▶  Stream Contínuo")
            self._lbl_status.setText("Stream parado.")
            self._lbl_status.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px;")

    @pyqtSlot()
    def _poll_next(self):
        """Envia o request do próximo PID da fila de rodízio."""
        if not self._poll_order or self._bus is None:
            return
        pid = self._poll_order[self._poll_idx % len(self._poll_order)]
        self._poll_idx += 1
        can_id, data = build_request(pid, target_ecu=self._target_ecu())
        ok, msg = self._bus.send(can_id, data, is_extended=False)
        if not ok:
            # Erro de transmissão interrompe o stream e avisa o usuário.
            self._btn_stream.setChecked(False)
            self._lbl_status.setText(f"Stream interrompido: {msg}")
            self._lbl_status.setStyleSheet(f"color: {COLORS['error']}; font-size: 12px;")

    # ── Recepção de respostas ────────────────────────────────────────────────

    def on_message(self, msg: CANMessage):
        """
        Chamado da THREAD DO CAN para cada mensagem recebida.

        Faz o mínimo possível: tenta interpretar como resposta OBD-II e, se for,
        enfileira o resultado. NÃO toca em widgets Qt aqui (isso é feito no
        _drain_responses, que roda na thread da interface).
        """
        try:
            result = parse_response(msg.can_id, msg.data)
        except Exception:
            return
        if result is not None:
            self._pending.append(result)

    @pyqtSlot()
    def _drain_responses(self):
        """
        Consome a fila de respostas e atualiza a tabela (thread da GUI).

        TRATAMENTO DE MÚLTIPLAS ECUs: com request broadcast (0x7DF), vários
        módulos respondem o MESMO PID com valores próprios. Em vez de deixar a
        última resposta sobrescrever a célula silenciosamente (o que faria o
        operador anotar a leitura do módulo errado), registramos todas as
        origens e sinalizamos o conflito na coluna Fonte e no Status.
        """
        while self._pending:
            try:
                r = self._pending.popleft()
            except IndexError:
                break
            pid = r["pid"]
            row = self._pid_rows.get(pid)
            if row is None:
                continue
            src = r.get("src")
            value = r["value"]

            # Registra a ECU de origem deste PID.
            srcs = self._pid_sources.setdefault(pid, set())
            if src is not None:
                srcs.add(src)

            # Formata: inteiro sem casas, fracionário com 1 casa.
            txt = f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"
            item = self._table.item(row, self.COL_VALOR)
            if item:
                item.setText(txt)
                item.setForeground(QColor(COLORS['success']))

            # Coluna Fonte: qual ECU respondeu (e aviso se houver mais de uma).
            fonte_item = self._table.item(row, self.COL_FONTE)
            if fonte_item is not None and src is not None:
                if len(srcs) > 1:
                    # Mais de um módulo respondendo o mesmo PID: valores
                    # distintos estão disputando a mesma célula.
                    lista = ", ".join(f"0x{s:03X}" for s in sorted(srcs))
                    fonte_item.setText(f"⚠ {len(srcs)} ECUs: {lista}")
                    fonte_item.setForeground(QColor(COLORS['warning']))
                    fonte_item.setToolTip(
                        "Vários módulos respondem este PID com valores próprios.\n"
                        "O valor exibido é o da última resposta recebida.\n"
                        "Escolha uma ECU específica no seletor 'ECU alvo' para\n"
                        "obter uma leitura sem ambiguidade."
                    )
                else:
                    fonte_item.setText(f"0x{src:03X}")
                    fonte_item.setForeground(QColor(COLORS['text_muted']))
                    fonte_item.setToolTip(ecu_name(src))

            # Status reflete o conflito, se houver.
            if len(srcs) > 1:
                self._set_status(pid, "⚠ conflito", COLORS['warning'])
            else:
                self._set_status(pid, "OK", COLORS['success'])

    def _set_status(self, pid: int, text: str, color: str):
        """Atualiza a coluna Status de um PID."""
        row = self._pid_rows.get(pid)
        if row is None:
            return
        item = self._table.item(row, self.COL_STATUS)
        if item:
            item.setText(text)
            item.setForeground(QColor(color))
