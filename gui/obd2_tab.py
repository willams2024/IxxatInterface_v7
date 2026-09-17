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

import os
import time
from collections import deque
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QMessageBox,
    QFileDialog,
)

from core.can_bus import CANMessage
from core.obd2 import (
    PID_DATABASE, ECU_NAMES, PROTOCOL_NOTES, OBD_REQUEST_FUNCTIONAL,
    OBD_REQUEST_PHYSICAL_BASE, OBD_RESP_MIN, OBD_RESP_MAX,
    build_request, parse_response, ecu_name, formula_text,
)
from gui.styles import COLORS

# Pasta padrão sugerida ao salvar a documentação (a mesma usada pelos logs de
# sessão da descoberta de sinais, para o usuário achar tudo no mesmo lugar).
DOC_DIR = os.path.join(os.path.expanduser("~"), "Documents", "IxxatInterface")


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

        # ── Registro para a DOCUMENTAÇÃO exportável ──────────────────────────
        # Acumula, ao longo de toda a sessão, o que realmente circulou no
        # barramento: quais requests saíram e quais respostas voltaram (de qual
        # ECU, com quais bytes crus). É a matéria-prima do botão "Exportar
        # Documentação" — sem isso o arquivo seria só teoria, não o protocolo
        # observado neste veículo.
        #   _doc_req: (pid, can_id do request) -> {count, data, first_ts}
        #   _doc_obs: (pid, id da ECU)         -> {count, valores, bytes crus}
        self._doc_req: dict[tuple[int, int], dict] = {}
        self._doc_obs: dict[tuple[int, int], dict] = {}
        self._doc_start: float = 0.0   # instante do 1º request da sessão

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

        # Exporta um arquivo documentando o diálogo OBD-II observado.
        self._btn_doc = QPushButton("📄  Exportar Documentação")
        self._btn_doc.clicked.connect(self._export_doc)
        self._btn_doc.setToolTip(
            "Gera um arquivo (.xlsx ou .txt) documentando o protocolo OBD-II\n"
            "desta sessão: requests transmitidos, respostas recebidas por ECU,\n"
            "bytes crus, fórmulas de conversão, PIDs sem resposta e a\n"
            "referência da norma SAE J1979 / ISO 15765-4."
        )
        hdr.addWidget(self._btn_doc)
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
                self._record_request(pid, can_id, data)
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
        if ok:
            self._record_request(pid, can_id, data)
        else:
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
            # Guardamos também o quadro CRU e o DLC: a documentação exportada
            # precisa mostrar os bytes exatos que a ECU colocou no barramento,
            # não apenas o valor já convertido.
            result["raw"] = bytes(msg.data)
            result["dlc"] = msg.dlc
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

            # Alimenta o registro cumulativo usado pela documentação.
            self._record_response(r)

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

    # ── Registro do tráfego (matéria-prima da documentação) ──────────────────

    def _record_request(self, pid: int, can_id: int, data: bytes):
        """
        Anota um request OBD-II que FOI transmitido com sucesso.

        A chave inclui a ID usada (0x7DF ou 0x7E0+n) porque o operador pode
        trocar de endereçamento no meio da sessão — e a documentação deve
        mostrar exatamente como cada PID foi perguntado.
        """
        now = time.time()
        if self._doc_start == 0.0:
            self._doc_start = now      # marca o início da sessão OBD-II
        key = (pid, can_id)
        rec = self._doc_req.get(key)
        if rec is None:
            self._doc_req[key] = {"count": 1, "data": bytes(data), "first": now}
        else:
            rec["count"] += 1

    def _record_response(self, r: dict):
        """
        Acumula uma resposta decodificada no registro da documentação.

        Uma entrada por par (PID, ECU): em broadcast o mesmo PID volta de
        módulos diferentes, e cada módulo tem a SUA faixa de valores. Guardamos
        contagem, mínimo/máximo, último valor e os quadros crus (primeiro e
        último) para o arquivo exportado.
        """
        pid = r.get("pid")
        src = r.get("src")
        if pid is None or src is None:
            return
        now = time.time()
        value = r["value"]
        raw = r.get("raw", b"")
        key = (pid, src)
        rec = self._doc_obs.get(key)
        if rec is None:
            self._doc_obs[key] = {
                "count": 1, "first": now, "last": now,
                "min": value, "max": value, "last_value": value,
                "raw_first": raw, "raw_last": raw,
                "dlc": r.get("dlc", len(raw)),
                "name": r.get("name", ""), "unit": r.get("unit", ""),
            }
        else:
            rec["count"] += 1
            rec["last"] = now
            rec["min"] = min(rec["min"], value)
            rec["max"] = max(rec["max"], value)
            rec["last_value"] = value
            if raw:
                rec["raw_last"] = raw

    # ── Exportação da documentação ───────────────────────────────────────────

    @staticmethod
    def _hex_bytes(data: bytes) -> str:
        """Bytes em hexadecimal separados por espaço (ex.: '02 01 0C 55 ...')."""
        return " ".join(f"{b:02X}" for b in data) if data else "—"

    @staticmethod
    def _fmt_num(v: float) -> str:
        """Número legível: sem casas decimais quando é praticamente inteiro."""
        return f"{v:.0f}" if abs(v - round(v)) < 0.005 else f"{v:.2f}"

    @staticmethod
    def _fmt_time(ts: float) -> str:
        """Hora do relógio (HH:MM:SS) a partir de um time.time()."""
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"

    def _request_ids_for(self, pid: int) -> str:
        """IDs de request usadas para um PID (pode haver mais de uma)."""
        ids = sorted({cid for (p, cid) in self._doc_req if p == pid})
        return ", ".join(f"0x{c:03X}" for c in ids) if ids else "—"

    def _doc_summary(self) -> list[tuple[str, str]]:
        """Pares (rótulo, valor) que descrevem a sessão de leitura OBD-II."""
        total_req = sum(r["count"] for r in self._doc_req.values())
        total_resp = sum(r["count"] for r in self._doc_obs.values())
        pids_req = {p for (p, _) in self._doc_req}
        pids_resp = {p for (p, _) in self._doc_obs}
        ecus = sorted({s for (_, s) in self._doc_obs})

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
            ("Documento", "Protocolo OBD-II observado no barramento"),
            ("Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
            ("Programa", "IxxatInterface v7 — aba OBD-II"),
            ("Norma", "SAE J1979 (modo 01) sobre ISO 15765-4 (CAN 11 bits)"),
            ("Origem dos dados", fonte),
            ("Modo de transmissão", tx),
            ("ECU alvo selecionada", self._cmb_ecu.currentText()),
            ("Início da leitura", self._fmt_time(self._doc_start)),
            ("Duração da leitura", dur),
            ("Requests transmitidos", f"{total_req} (em {len(pids_req)} PID(s))"),
            ("Respostas recebidas", f"{total_resp}"),
            ("PIDs com resposta", f"{len(pids_resp)}"),
            ("PIDs sem resposta", f"{len(pids_req - pids_resp)}"),
            ("ECUs que responderam",
             ", ".join(f"0x{s:03X} ({ecu_name(s)})" for s in ecus) or "—"),
            ("Escrita no barramento",
             "NENHUMA — apenas requests de leitura do modo 01; nenhum dado é "
             "gravado nas ECUs"),
        ]

    # Cabeçalhos das tabelas do documento (compartilhados por Excel e TXT).
    DOC_OBS_HEADERS = [
        "PID (hex)", "PID (dec)", "Parâmetro", "ECU", "Nome da ECU",
        "Request (ID)", "Request (bytes)", "Resposta (bytes)", "DLC",
        "Bytes de dados", "Fórmula (A, B = bytes de dados)", "Unidade",
        "Último valor", "Mínimo", "Máximo", "Respostas",
        "1ª resposta", "Última resposta",
    ]
    DOC_NORESP_HEADERS = [
        "PID (hex)", "PID (dec)", "Parâmetro", "Request (ID)",
        "Request (bytes)", "Requests enviados", "Interpretação",
    ]
    DOC_DB_HEADERS = [
        "PID (hex)", "PID (dec)", "Parâmetro", "Bytes de dados", "Unidade",
        "Fórmula (A, B = bytes de dados)", "Faixa típica",
        "Request (broadcast)", "Observado nesta sessão",
    ]

    def _rows_observed(self) -> list[list]:
        """Uma linha por par (PID, ECU) efetivamente observado no barramento."""
        rows = []
        for (pid, src) in sorted(self._doc_obs.keys()):
            rec = self._doc_obs[(pid, src)]
            info = PID_DATABASE.get(pid)
            n_bytes = info.n_bytes if info else rec["dlc"]
            # Request cru correspondente (qualquer um deste PID serve como
            # exemplo do quadro transmitido).
            req_data = next((r["data"] for (p, _), r in self._doc_req.items()
                             if p == pid), b"")
            rows.append([
                f"0x{pid:02X}", pid,
                rec["name"] or (info.name if info else "—"),
                f"0x{src:03X}", ecu_name(src),
                self._request_ids_for(pid), self._hex_bytes(req_data),
                self._hex_bytes(rec["raw_last"]), rec["dlc"],
                n_bytes, formula_text(pid), rec["unit"],
                self._fmt_num(rec["last_value"]),
                self._fmt_num(rec["min"]), self._fmt_num(rec["max"]),
                rec["count"],
                self._fmt_time(rec["first"]), self._fmt_time(rec["last"]),
            ])
        return rows

    def _rows_no_response(self) -> list[list]:
        """PIDs que foram perguntados e NÃO voltaram — sinal de não suportado."""
        pids_resp = {p for (p, _) in self._doc_obs}
        rows = []
        for pid in sorted({p for (p, _) in self._doc_req} - pids_resp):
            info = PID_DATABASE.get(pid)
            req_data = next((r["data"] for (p, _), r in self._doc_req.items()
                             if p == pid), b"")
            n = sum(r["count"] for (p, _), r in self._doc_req.items() if p == pid)
            rows.append([
                f"0x{pid:02X}", pid, info.name if info else "—",
                self._request_ids_for(pid), self._hex_bytes(req_data), n,
                "Nenhuma ECU respondeu — PID provavelmente não suportado "
                "por este veículo",
            ])
        return rows

    def _rows_database(self) -> list[list]:
        """Referência completa do banco de PIDs implementado no programa."""
        pids_resp = {p for (p, _) in self._doc_obs}
        rows = []
        for pid in sorted(PID_DATABASE):
            info = PID_DATABASE[pid]
            _, req = build_request(pid)     # exemplo com ID funcional (0x7DF)
            rows.append([
                f"0x{pid:02X}", pid, info.name, info.n_bytes, info.unit,
                formula_text(pid),
                f"{self._fmt_num(info.min_val)} a {self._fmt_num(info.max_val)} "
                f"{info.unit}".strip(),
                f"0x{OBD_REQUEST_FUNCTIONAL:03X}: {self._hex_bytes(req)}",
                "sim" if pid in pids_resp else "—",
            ])
        return rows

    @pyqtSlot()
    def _export_doc(self):
        """
        Gera o arquivo de documentação do protocolo OBD-II desta sessão.

        O formato é escolhido pela extensão no diálogo de salvamento:
          .xlsx → planilha com 5 abas (resumo, observados, sem resposta,
                  referência do protocolo e banco de PIDs);
          .txt  → mesmo conteúdo em texto puro (não depende do openpyxl).
        """
        # Sem tráfego registrado o arquivo seria apenas a referência da norma;
        # confirmamos com o usuário para não gerar um documento vazio por engano.
        if not self._doc_req:
            resp = QMessageBox.question(
                self, "Nenhuma leitura registrada",
                "Nenhum request OBD-II foi transmitido nesta sessão, então não "
                "há tráfego observado para documentar.\n\n"
                "Deseja exportar apenas a referência do protocolo e o banco de "
                "PIDs do programa?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if resp != QMessageBox.Yes:
                return

        # Sugere a pasta Documents/IxxatInterface (mesma dos logs de sessão).
        nome = f"protocolo_obd2_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        try:
            os.makedirs(DOC_DIR, exist_ok=True)
            sugerido = os.path.join(DOC_DIR, nome)
        except Exception:
            sugerido = nome

        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar Documentação OBD-II", sugerido,
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

        obs = len(self._doc_obs)
        QMessageBox.information(
            self, "Documentação gerada",
            f"Arquivo salvo em:\n{path}\n\n"
            f"{obs} par(es) PID/ECU documentado(s) a partir do tráfego real.")

    def _write_doc_xlsx(self, path: str):
        """Escreve a documentação como planilha Excel de 5 abas."""
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
            """Escreve cabeçalho + linhas com borda e zebra; devolve a próxima linha."""
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
        titulo(ws, "DOCUMENTAÇÃO DO PROTOCOLO OBD-II — RESUMO DA SESSÃO", 2)
        ws.column_dimensions["A"].width = 26
        ws.column_dimensions["B"].width = 95
        r = 3
        for label, value in self._doc_summary():
            a = ws.cell(row=r, column=1, value=label)
            a.fill, a.font, a.border, a.alignment = hdr_fill, bold, border, left
            b = ws.cell(row=r, column=2, value=value)
            b.border, b.alignment = border, wrap
            r += 1

        # ── Aba 2: tráfego observado ─────────────────────────────────────────
        ws = wb.create_sheet("PIDs Observados")
        titulo(ws, "PIDs RESPONDIDOS PELO VEÍCULO (tráfego real capturado)",
               len(self.DOC_OBS_HEADERS))
        tabela(ws, self.DOC_OBS_HEADERS, self._rows_observed(), 3,
               widths=[10, 9, 30, 8, 24, 14, 26, 26, 6, 8, 30, 9,
                       13, 10, 10, 10, 12, 14])

        # ── Aba 3: PIDs sem resposta ─────────────────────────────────────────
        ws = wb.create_sheet("Sem Resposta")
        titulo(ws, "PIDs CONSULTADOS SEM RESPOSTA (não suportados)",
               len(self.DOC_NORESP_HEADERS))
        tabela(ws, self.DOC_NORESP_HEADERS, self._rows_no_response(), 3,
               widths=[10, 9, 32, 14, 26, 16, 60])

        # ── Aba 4: referência do protocolo ───────────────────────────────────
        ws = wb.create_sheet("Protocolo")
        titulo(ws, "COMO O DIÁLOGO OBD-II FUNCIONA (referência da norma)", 2)
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 100
        r = 3
        for topico, texto in PROTOCOL_NOTES:
            a = ws.cell(row=r, column=1, value=topico)
            a.fill, a.font, a.border, a.alignment = hdr_fill, bold, border, left
            b = ws.cell(row=r, column=2, value=texto)
            b.border, b.alignment = border, wrap
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

        # ── Aba 5: banco de PIDs do programa ─────────────────────────────────
        ws = wb.create_sheet("Banco de PIDs")
        titulo(ws, "PIDs IMPLEMENTADOS NO PROGRAMA (SAE J1979 — modo 01)",
               len(self.DOC_DB_HEADERS))
        tabela(ws, self.DOC_DB_HEADERS, self._rows_database(), 3,
               widths=[10, 9, 32, 8, 9, 30, 22, 34, 12])

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

        L: list[str] = []
        L.append("=" * 100)
        L.append("DOCUMENTAÇÃO DO PROTOCOLO OBD-II — IxxatInterface v7")
        L.append("=" * 100)
        L.append("")
        L.append("1) RESUMO DA SESSÃO")
        L.append("-" * 100)
        for label, value in self._doc_summary():
            L.append(f"  {label + ':':<24} {value}")
        L.append("")
        L.append("2) PIDs RESPONDIDOS PELO VEÍCULO (tráfego real capturado)")
        L.append("-" * 100)
        L += tabela(self.DOC_OBS_HEADERS, self._rows_observed())
        L.append("")
        L.append("3) PIDs CONSULTADOS SEM RESPOSTA (não suportados)")
        L.append("-" * 100)
        L += tabela(self.DOC_NORESP_HEADERS, self._rows_no_response())
        L.append("")
        L.append("4) COMO O DIÁLOGO OBD-II FUNCIONA (referência da norma)")
        L.append("-" * 100)
        for topico, texto in PROTOCOL_NOTES:
            L.append(f"  • {topico}:")
            # Quebra o texto em linhas de ~86 colunas para o arquivo ficar legível.
            palavras, atual = texto.split(), ""
            for p in palavras:
                if len(atual) + len(p) + 1 > 86:
                    L.append(f"      {atual}")
                    atual = p
                else:
                    atual = f"{atual} {p}".strip()
            if atual:
                L.append(f"      {atual}")
        L.append("")
        L.append("  IDs de resposta por módulo:")
        L += tabela(["ID de resposta", "Módulo", "ID de request físico"],
                    [[f"0x{rid:03X}", nome,
                      f"0x{OBD_REQUEST_PHYSICAL_BASE + (rid - OBD_RESP_MIN):03X}"]
                     for rid, nome in sorted(ECU_NAMES.items())])
        L.append("")
        L.append("5) PIDs IMPLEMENTADOS NO PROGRAMA (SAE J1979 — modo 01)")
        L.append("-" * 100)
        L += tabela(self.DOC_DB_HEADERS, self._rows_database())
        L.append("")
        L.append("=" * 100)
        L.append(f"Faixa de IDs OBD-II: request 0x{OBD_REQUEST_FUNCTIONAL:03X} / "
                 f"0x{OBD_REQUEST_PHYSICAL_BASE:03X}-0x{OBD_REQUEST_PHYSICAL_BASE + 7:03X}"
                 f"  |  resposta 0x{OBD_RESP_MIN:03X}-0x{OBD_RESP_MAX:03X}")
        L.append("=" * 100)

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
