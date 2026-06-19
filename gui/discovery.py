"""Signal discovery tab — guided tests to map CAN signals.

Aba "Descoberta de Sinais" (PyQt5) do programa de mapeamento CAN.

Visão geral do módulo:
    - SignalCard: card clicável (lista à esquerda) que representa um teste/sinal
      a ser mapeado. Mostra ícone de status, nome, resultado e o PGN encontrado.
    - DiscoveryTab: painel à direita com instruções (não editáveis), indicador
      das 4 fases (BASELINE/TESTING/ANALYZING/DONE), barra de progresso, botões
      de controle (Iniciar Teste / Parar / Simular Ação) e a tabela de
      candidatos detectados.

Como tudo se conecta:
    - DiscoveryEngine (core/discovery.py): é o "cérebro" da descoberta. Recebe
      as mensagens CAN, grava um baseline, observa o que muda durante a ação do
      operador e devolve uma lista de CandidateSignal ordenada por confiança.
    - CANBus (core/can_bus.py): fornece as mensagens. As mensagens chegam pela
      thread de leitura CAN e são entregues a esta aba pelo método on_message().
    - styles.COLORS: paleta de cores hexadecimais usada em todo o app.

Fluxo resumido de uso:
    1) O operador clica em um card de sinal -> select_test() prepara o painel.
    2) Clica em "Iniciar Teste" -> _start_test() arma o engine e o timer da UI.
    3) As mensagens CAN chegam em on_message() e alimentam o engine (feed()).
    4) O timer (_tick) lê a fase/progresso do engine e atualiza a tela.
    5) Ao terminar (Phase.DONE), _show_results() exibe os candidatos e guarda
       o resultado para a aba "Sinais Mapeados".
"""

import time

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QFrame, QProgressBar, QScrollArea,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QSplitter, QTextEdit,
)

from core.can_bus import CANMessage
from core.discovery import DiscoveryEngine, Phase, TESTS, TestDefinition, CandidateSignal
from gui.styles import COLORS


class SignalCard(QFrame):
    """Card widget representing one signal test.

    Representa, na lista à esquerda, UM teste de descoberta (um sinal a ser
    mapeado, ex.: velocidade, RPM, freio...). É clicável: ao receber o clique,
    avisa a aba para abrir o teste correspondente no painel da direita.

    Estado visual:
        - status ("idle"/"running"/"done"/"failed") controla o ícone e a cor do
          texto de resultado.
        - guarda a referência ao TestDefinition (self.test) para identificar qual
          teste este card representa.
    """

    # Ícone exibido à esquerda do card conforme o estado do teste.
    STATUS_ICONS = {
        "idle":    "⬜", "running": "🔵",
        "done":    "✅", "failed":  "❌",
    }

    def __init__(self, test: TestDefinition, on_click=None, parent=None):
        """Monta o card para um TestDefinition.

        Args:
            test: definição do teste/sinal que este card representa.
            on_click: função chamada quando o card é clicado (recebe o
                TestDefinition). Ver mousePressEvent para o motivo de usar um
                callback em vez de navegar pela hierarquia parent().
            parent: widget pai Qt.
        """
        super().__init__(parent)
        self.test = test
        self.status = "idle"
        # Guardamos a função de callback diretamente em vez de tentar achar a
        # DiscoveryTab subindo por self.parent().parent()...  Isso desacopla o
        # card da hierarquia de widgets: ele não precisa saber onde está montado
        # nem quem é o pai. A aba simplesmente passa self.select_test ao criar o
        # card, e o card só "dispara" essa função. Mais simples, robusto a
        # mudanças de layout e fácil de testar.
        self._on_click = on_click   # callback direto — sem depender de parent()
        self._setup_ui()
        self.setObjectName("signal_card")
        self.setStyleSheet(f"""
            QFrame#signal_card {{
                background-color: {COLORS['bg_card']};
                border: 1px solid {COLORS['border']};
                border-radius: 8px;
            }}
            QFrame#signal_card:hover {{
                border-color: {COLORS['accent']};
            }}
        """)
        self.setCursor(Qt.PointingHandCursor)

    def _setup_ui(self):
        """Monta os widgets internos do card (layout horizontal).

        Estrutura: [ícone de status] [nome + linha de resultado] [PGN à direita].
        """
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # Ícone de status (quadrado/azul/check/x) à esquerda.
        self._lbl_icon = QLabel(self.STATUS_ICONS["idle"])
        self._lbl_icon.setFixedWidth(24)
        self._lbl_icon.setFont(QFont("Segoe UI Emoji", 14))
        layout.addWidget(self._lbl_icon)

        # Bloco central: nome do sinal (negrito) + linha de resultado/status.
        info = QVBoxLayout()
        info.setSpacing(2)
        self._lbl_name = QLabel(self.test.name)
        self._lbl_name.setStyleSheet("font-weight: bold; font-size: 13px;")
        info.addWidget(self._lbl_name)
        # Texto secundário: muda conforme o teste avança (ver set_status).
        self._lbl_result = QLabel("Aguardando teste...")
        self._lbl_result.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        info.addWidget(self._lbl_result)
        layout.addLayout(info)
        layout.addStretch()

        # Rótulo à direita: mostra o PGN / CAN ID do melhor candidato quando o
        # teste termina com sucesso.
        self._lbl_pgn = QLabel("")
        self._lbl_pgn.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold; font-size: 11px;")
        self._lbl_pgn.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._lbl_pgn)

    def set_status(self, status: str, result_text: str = "", pgn_text: str = ""):
        """Atualiza a aparência do card conforme o andamento do teste.

        Args:
            status: "idle" | "running" | "done" | "failed". Define o ícone e a
                cor do texto de resultado.
            result_text: linha secundária (ex.: "Teste em andamento...",
                resumo do sinal encontrado). Vazio volta ao texto padrão.
            pgn_text: texto curto à direita (PGN ou CAN ID encontrado).
        """
        self.status = status
        self._lbl_icon.setText(self.STATUS_ICONS.get(status, "⬜"))
        self._lbl_result.setText(result_text or "Aguardando teste...")
        self._lbl_pgn.setText(pgn_text)
        color = {
            "idle":    COLORS["text_muted"],
            "running": COLORS["warning"],
            "done":    COLORS["success"],
            "failed":  COLORS["error"],
        }.get(status, COLORS["text_muted"])
        self._lbl_result.setStyleSheet(f"color: {color}; font-size: 11px;")

    def mousePressEvent(self, event):
        """Captura o clique do mouse sobre o card.

        Em vez de o card procurar a DiscoveryTab subindo pela hierarquia de
        widgets (parent()), ele apenas invoca o callback recebido no __init__
        passando o seu próprio TestDefinition. Quem montou o card (a aba) decide
        o que fazer — tipicamente abrir esse teste no painel da direita
        (select_test). Só responde ao botão esquerdo do mouse.
        """
        if event.button() == Qt.LeftButton and self._on_click:
            self._on_click(self.test)


class DiscoveryTab(QWidget):
    """Aba "Descoberta de Sinais": orquestra a UI e o DiscoveryEngine.

    Responsabilidades:
        - Montar a interface (lista de cards à esquerda + painel de teste à
          direita).
        - Controlar o ciclo de um teste: selecionar -> iniciar -> alimentar o
          engine com mensagens CAN -> mostrar fases/progresso -> exibir
          candidatos.
        - Guardar os resultados por teste em self._saved_results para que outra
          aba ("Sinais Mapeados") possa consumi-los via get_saved_results().

    Atributos principais:
        _bus: referência ao CANBus (conexão, modo simulação, estado simulado).
        _engine: instância do DiscoveryEngine que faz a análise dos dados.
        _active_test: teste atualmente selecionado no painel da direita.
        _cards: mapa chave-do-teste -> SignalCard, para atualizar status/visual.
        _saved_results: mapa chave-do-teste -> lista de candidatos detectados.
        _tick_timer: QTimer que dispara _tick() periodicamente para atualizar a
            UI a partir do estado do engine (a UI NÃO faz a análise; só lê).
    """

    def __init__(self, bus_ref, parent=None):
        """Inicializa a aba.

        Args:
            bus_ref: referência ao CANBus (pode ser trocada depois via set_bus).
            parent: widget pai Qt.
        """
        super().__init__(parent)
        self._bus = bus_ref                          # fonte das mensagens CAN
        self._engine = DiscoveryEngine()             # cérebro da descoberta
        self._active_test: TestDefinition | None = None   # teste selecionado
        self._cards: dict[str, SignalCard] = {}      # chave -> card visual
        # Resultados acumulados por teste; consumidos pela aba "Sinais Mapeados".
        self._saved_results: dict[str, list[CandidateSignal]] = {}
        # Timer que "puxa" o estado do engine (fase/progresso) para a tela.
        # NÃO recebe mensagens CAN — apenas atualiza widgets a ~10 Hz (100 ms).
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._setup_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        """Monta toda a interface da aba.

        Layout geral: um QSplitter horizontal divide a tela em duas áreas:
            - Esquerda: lista rolável de cards (um por teste em TESTS).
            - Direita: painel do teste (instruções, fases, progresso, botões e
              tabela de resultados).
        """
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Divisor redimensionável entre a lista (esquerda) e o painel (direita).
        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter)

        # Left: signal cards list
        left_w = QWidget()
        left_layout = QVBoxLayout(left_w)
        left_layout.setContentsMargins(12, 12, 6, 12)
        left_layout.setSpacing(6)

        lbl = QLabel("Sinais para Mapear")
        lbl.setObjectName("label_title")
        left_layout.addWidget(lbl)

        sub = QLabel("Clique em um sinal para iniciar o teste guiado")
        sub.setObjectName("label_subtitle")
        left_layout.addWidget(sub)

        # Área rolável que comporta todos os cards verticalmente.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        cards_w = QWidget()
        cards_layout = QVBoxLayout(cards_w)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(6)

        # Cria um SignalCard para cada teste definido em core/discovery.TESTS.
        # on_click=self.select_test: ao clicar, o card chama este método (ver
        # SignalCard.mousePressEvent) — sem precisar conhecer a hierarquia.
        # Guardamos cada card em self._cards pela mesma chave do teste, para
        # depois atualizar status/destaque.
        for key, test in TESTS.items():
            card = SignalCard(test, on_click=self.select_test, parent=cards_w)
            self._cards[key] = card
            cards_layout.addWidget(card)

        cards_layout.addStretch()
        scroll.setWidget(cards_w)
        left_layout.addWidget(scroll)

        splitter.addWidget(left_w)

        # Right: test panel
        # Painel da direita: tudo relacionado ao teste atualmente selecionado.
        right_w = QWidget()
        right_layout = QVBoxLayout(right_w)
        right_layout.setContentsMargins(6, 12, 12, 12)
        right_layout.setSpacing(10)

        # Test header
        # Título do painel: mostra qual teste está ativo (ou um convite).
        self._lbl_test_name = QLabel("Selecione um sinal à esquerda")
        self._lbl_test_name.setObjectName("label_title")
        right_layout.addWidget(self._lbl_test_name)

        # Instruction box
        # Caixa de instruções do teste. Usa um QPlainTextEdit somente-leitura
        # (em vez de QLabel) para suportar textos longos com rolagem, mantendo
        # o conteúdo NÃO editável pelo operador.
        instr_box = QGroupBox("Instruções")
        instr_layout = QVBoxLayout(instr_box)
        from PyQt5.QtWidgets import QPlainTextEdit
        self._lbl_instruction = QPlainTextEdit()
        self._lbl_instruction.setPlaceholderText("Selecione um sinal à esquerda...")
        self._lbl_instruction.setReadOnly(True)              # fixo, não editável
        self._lbl_instruction.setMinimumHeight(220)
        self._lbl_instruction.setStyleSheet(f"""
            QPlainTextEdit {{
                color: {COLORS['text']};
                background-color: {COLORS['bg_header']};
                border: 1px solid {COLORS['accent']};
                border-radius: 4px;
                font-size: 16px;
                line-height: 1.5;
                padding: 10px 14px;
            }}
        """)
        instr_layout.addWidget(self._lbl_instruction)
        right_layout.addWidget(instr_box)

        # Phase indicator
        # Indicador das 4 fases do teste, exibidas lado a lado. Cada label
        # corresponde, na ordem, a: BASELINE, TESTING, ANALYZING, DONE.
        # A fase ativa é destacada por _set_phase_active() durante o _tick.
        phase_row = QHBoxLayout()
        phase_row.setSpacing(6)
        self._phase_labels = []
        for text in ["1  Baseline", "2  Teste", "3  Análise", "4  Resultado"]:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFixedHeight(30)
            lbl.setStyleSheet(f"""
                background-color: {COLORS['bg_header']};
                color: {COLORS['text_muted']};
                border-radius: 4px;
                font-size: 11px;
                padding: 0 8px;
            """)
            phase_row.addWidget(lbl)
            self._phase_labels.append(lbl)
        right_layout.addLayout(phase_row)

        # Progress
        # Barra de progresso (0–100%). Reflete engine.progress * 100 no _tick.
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(18)
        right_layout.addWidget(self._progress)

        # Linha de status textual (ex.: "Gravando baseline...", "REALIZE A AÇÃO
        # AGORA!", resultado final). Cor/tamanho mudam conforme a fase.
        self._lbl_status = QLabel("")
        self._lbl_status.setAlignment(Qt.AlignCenter)
        self._lbl_status.setStyleSheet(f"color: {COLORS['warning']}; font-weight: bold;")
        right_layout.addWidget(self._lbl_status)

        # Control buttons
        # Linha de botões de controle do teste.
        btn_row = QHBoxLayout()
        # "Iniciar Teste": começa a gravação do baseline (requer barramento
        # conectado). Habilitado só após selecionar um teste / conectar.
        self._btn_start = QPushButton("Iniciar Teste")
        self._btn_start.setObjectName("btn_success")
        self._btn_start.setEnabled(False)
        self._btn_start.clicked.connect(lambda: self._start_test())
        btn_row.addWidget(self._btn_start)

        # "Parar": interrompe o teste em andamento e reseta as fases.
        self._btn_stop = QPushButton("Parar")
        self._btn_stop.setObjectName("btn_danger")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(lambda: self._stop_test())
        btn_row.addWidget(self._btn_stop)

        # "Simular Acao": só no Modo Simulação. Liga/desliga a injeção de um
        # sinal sintético para validar a descoberta sem o veículo real.
        self._btn_sim = QPushButton("Simular Acao")
        self._btn_sim.setObjectName("btn_warning")
        self._btn_sim.setEnabled(False)
        self._btn_sim.setToolTip("Injeta o sinal simulado durante o teste (requer Modo Simulacao ativo)")
        self._btn_sim.clicked.connect(lambda: self._toggle_simulation())
        btn_row.addWidget(self._btn_sim)
        btn_row.addStretch()
        right_layout.addLayout(btn_row)

        # Results table
        # Tabela de candidatos detectados pelo engine, uma linha por sinal,
        # ordenados por confiança (melhor primeiro). Preenchida em
        # _show_results().
        results_box = QGroupBox("Sinais Encontrados")
        results_layout = QVBoxLayout(results_box)

        # 6 colunas: confiança, CAN ID, PGN/Nome, byte(s), fórmula e exemplo.
        self._results_table = QTableWidget(0, 6)
        self._results_table.setHorizontalHeaderLabels([
            "Confiança", "CAN ID", "PGN / Nome", "Byte(s)", "Fórmula", "Exemplo de Valor"
        ])
        hdr = self._results_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._results_table.verticalHeader().setVisible(False)
        self._results_table.setSelectionBehavior(QTableWidget.SelectRows)
        results_layout.addWidget(self._results_table)

        # Detalhe textual (fonte mono) do melhor candidato: fórmula, confiança
        # e variação bruta observada. Preenchido em _show_results().
        self._lbl_formula_detail = QLabel("")
        self._lbl_formula_detail.setWordWrap(True)
        self._lbl_formula_detail.setStyleSheet(
            f"color: {COLORS['accent']}; font-family: Consolas, monospace; font-size: 12px;"
        )
        results_layout.addWidget(self._lbl_formula_detail)

        right_layout.addWidget(results_box)
        splitter.addWidget(right_w)
        # Tamanhos iniciais: ~300 px para a lista, ~700 px para o painel.
        splitter.setSizes([300, 700])

    # ── Slot: select test ─────────────────────────────────────────────────────

    def select_test(self, test: TestDefinition):
        """Abre um teste no painel da direita (chamado pelo clique no card).

        Prepara a UI para o teste escolhido: mostra título e instruções, limpa
        resultados anteriores, reseta as fases e destaca o card selecionado.
        Habilita "Iniciar Teste" apenas se o barramento estiver conectado, e
        "Simular Ação" apenas no Modo Simulação.

        Args:
            test: definição do teste selecionado (vinda do SignalCard clicado).
        """
        self._active_test = test
        self._lbl_test_name.setText(f"Teste: {test.name}")
        self._lbl_instruction.setPlainText(test.instruction)
        self._btn_start.setEnabled(True)
        self._btn_start.setEnabled(self._bus.is_connected)
        self._btn_sim.setEnabled(self._bus.is_simulation)
        self._reset_phase_ui()
        self._results_table.setRowCount(0)
        self._lbl_formula_detail.setText("")
        self._lbl_status.setText("")

        # Highlight active card
        # Destaca visualmente o card do teste ativo (borda accent) e devolve os
        # demais ao estilo normal.
        for key, card in self._cards.items():
            if card.test.key == test.key:
                card.setStyleSheet(f"""
                    QFrame#signal_card {{
                        background-color: {COLORS['bg_header']};
                        border: 2px solid {COLORS['accent']};
                        border-radius: 8px;
                    }}
                """)
            else:
                card.setStyleSheet(f"""
                    QFrame#signal_card {{
                        background-color: {COLORS['bg_card']};
                        border: 1px solid {COLORS['border']};
                        border-radius: 8px;
                    }}
                """)

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _start_test(self):
        """Inicia o teste selecionado.

        Passos:
            1) Valida que há teste ativo e barramento conectado.
            2) Arma o DiscoveryEngine (start_test) — ele entra na fase BASELINE
               e passa a aceitar mensagens via feed() (ver on_message).
            3) Liga o timer de UI (100 ms) que vai refletir fase/progresso.
            4) Ajusta os botões e coloca o card em estado "running".
        Não bloqueia: a coleta acontece de forma assíncrona (mensagens chegam
        pela thread CAN; o engine controla o tempo de cada fase).
        """
        if not self._active_test or not self._bus.is_connected:
            return
        self._engine.start_test(self._active_test)
        self._tick_timer.start(100)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_sim.setEnabled(self._bus.is_simulation)
        self._lbl_status.setText("Gravando baseline — não mexa no veículo...")
        self._set_phase_active(0)
        if self._active_test.key in self._cards:
            self._cards[self._active_test.key].set_status("running", "Teste em andamento...")

    def _stop_test(self):
        """Interrompe o teste em andamento.

        Para o timer de UI, força o engine de volta a Phase.IDLE (deixando de
        aceitar mensagens em on_message), restaura os botões, limpa o indicador
        de fases e desliga qualquer simulação ativa.
        """
        self._tick_timer.stop()
        self._engine.phase = Phase.IDLE
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._lbl_status.setText("Teste interrompido.")
        self._reset_phase_ui()
        self._toggle_simulation(force_off=True)

    def _toggle_simulation(self, force_off=False):
        """Liga/desliga a injeção de um sinal sintético (Modo Simulação).

        Serve para validar o fluxo de descoberta SEM o veículo real: em vez de
        o operador acionar algo no carro, o gerador de simulação do CANBus passa
        a emitir mensagens que contêm o sinal esperado. Assim o engine consegue
        detectar a variação durante a fase TESTING.

        Mecanismo: cada teste declara em sim_inject_attr o NOME do atributo
        booleano dentro do estado de simulação (_bus._sim_state) que liga aquele
        sinal. Aqui apenas alternamos esse booleano via setattr e atualizamos o
        texto do botão.

        Args:
            force_off: quando True, garante o desligamento (usado ao parar ou
                concluir o teste), sem alternar.

        Observações:
            - Não faz nada se não houver teste ativo, se o barramento não estiver
              conectado, se não estiver em Modo Simulação, se o teste não definir
              sim_inject_attr ou se não houver estado de simulação.
            - Envolto em try/except: qualquer falha vira um aviso (QMessageBox)
              em vez de derrubar a UI.
        """
        try:
            if not self._active_test:
                return
            # Só funciona conectado E em Modo Simulação; caso contrário desabilita.
            if not self._bus or not self._bus.is_connected or not self._bus.is_simulation:
                self._btn_sim.setEnabled(False)
                return
            # Nome do atributo booleano que ativa o sinal sintético deste teste.
            attr = self._active_test.sim_inject_attr
            if not attr:
                return
            # Estado interno do gerador de simulação do barramento.
            sim = self._bus._sim_state
            if sim is None:
                return
            # Desligamento forçado (parar/concluir): zera o atributo e restaura o botão.
            if force_off:
                setattr(sim, attr, False)
                self._btn_sim.setText("Simular Acao")
                return
            # Alternância normal: inverte o booleano e reflete no texto do botão.
            current = getattr(sim, attr, False)
            new_val = not current
            setattr(sim, attr, new_val)
            self._btn_sim.setText("Parar Simulacao" if new_val else "Simular Acao")
        except Exception as e:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Erro na Simulacao", str(e))

    # ── Feed CAN data ─────────────────────────────────────────────────────────

    def on_message(self, msg: CANMessage):
        """Recebe UMA mensagem CAN e a repassa ao engine.

        Este é o ponto de entrada dos dados: cada quadro CAN capturado (vindo da
        thread de leitura do CANBus) chega aqui. Só alimentamos o engine durante
        as fases que coletam dados (BASELINE e TESTING); nas demais (IDLE,
        ANALYZING, DONE) a mensagem é ignorada para não contaminar a análise.

        Importante: este método pode ser chamado a partir da thread CAN, então
        ele faz o mínimo — apenas entrega os dados crus ao engine (feed). A
        atualização visual fica por conta do _tick (na thread da UI), evitando
        mexer em widgets fora da thread principal.

        Args:
            msg: mensagem CAN recebida (id, dados, flag de id estendido).
        """
        if self._engine.phase in (Phase.BASELINE, Phase.TESTING):
            self._engine.feed(msg.can_id, msg.data, msg.is_extended)

    # ── Periodic tick ─────────────────────────────────────────────────────────

    @pyqtSlot()
    def _tick(self):
        """Atualiza a UI a partir do estado do engine (chamado a cada 100 ms).

        A UI é "passiva": não decide nada da análise, apenas LÊ a fase atual, o
        progresso e o tempo restante do engine e reflete isso na tela
        (indicador de fases, barra e linha de status). É aqui que o operador vê
        a instrução "REALIZE A AÇÃO AGORA!" no momento certo.

        Ao detectar Phase.DONE, encerra o ciclo: para o timer, restaura botões,
        desliga simulação e exibe os candidatos (_show_results).
        """
        phase = self._engine.phase
        # Progresso 0–100% derivado de engine.progress (0.0–1.0).
        pct = int(self._engine.progress * 100)
        self._progress.setValue(pct)
        # Segundos restantes da fase atual (mostrado na contagem regressiva).
        secs = self._engine.remaining_seconds()

        # Fase 1 — BASELINE: grava o "estado parado" do barramento (referência).
        if phase == Phase.BASELINE:
            self._set_phase_active(0)
            self._lbl_status.setText(f"Gravando baseline... {secs:.0f}s restantes")

        # Fase 2 — TESTING: o operador deve executar a ação AGORA para que o
        # sinal varie e o engine consiga isolá-lo. Destaque visual reforçado.
        elif phase == Phase.TESTING:
            self._set_phase_active(1)
            self._lbl_status.setText(f"⚡ REALIZE A AÇÃO AGORA! {secs:.0f}s restantes")
            self._lbl_status.setStyleSheet(f"color: {COLORS['warning']}; font-weight: bold; font-size: 14px;")

        # Fase 3 — ANALYZING: o engine compara baseline x teste e pontua candidatos.
        elif phase == Phase.ANALYZING:
            self._set_phase_active(2)
            self._lbl_status.setText("Analisando dados...")

        # Fase 4 — DONE: terminou. Encerra o ciclo e mostra os resultados.
        elif phase == Phase.DONE:
            self._tick_timer.stop()
            self._set_phase_active(3)
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._toggle_simulation(force_off=True)
            self._show_results(self._engine.results)

    # ── Results ───────────────────────────────────────────────────────────────

    def _show_results(self, results: list[CandidateSignal]):
        """Exibe os candidatos detectados e persiste o resultado do teste.

        Faz três coisas:
            1) Salva a lista de candidatos em self._saved_results[key]. É ESTE
               dicionário que a aba "Sinais Mapeados" consome via
               get_saved_results() — ou seja, o resultado da descoberta é
               passado adiante por aqui, sem acoplamento direto entre as abas.
            2) Preenche a tabela "Sinais Encontrados", uma linha por candidato,
               com cor por nível de confiança e a primeira linha (melhor
               candidato) realçada.
            3) Atualiza o card do teste (status "done"/"failed") e o detalhe da
               fórmula do melhor candidato.

        Args:
            results: candidatos ordenados por confiança (melhor primeiro).
        """
        # Chave do teste ativo: usada para arquivar o resultado e atualizar card.
        key = self._active_test.key if self._active_test else ""
        # Guarda para a aba "Sinais Mapeados" ler depois (get_saved_results).
        self._saved_results[key] = results

        self._results_table.setRowCount(0)
        # Nenhum candidato: marca o card como falho e orienta a repetir.
        if not results:
            self._lbl_status.setText("Nenhum sinal detectado. Tente novamente.")
            self._lbl_status.setStyleSheet(f"color: {COLORS['error']}; font-weight: bold;")
            if key in self._cards:
                self._cards[key].set_status("failed", "Nenhum sinal encontrado")
            return

        # Melhor candidato (maior confiança) — usado no resumo do card e detalhe.
        best = results[0]
        for i, sig in enumerate(results):
            row = self._results_table.rowCount()
            self._results_table.insertRow(row)
            conf_pct = f"{sig.confidence * 100:.0f}%"

            # Cor da confiança: verde (>80%), amarelo (>50%), cinza abaixo disso.
            color = COLORS['success'] if sig.confidence > 0.8 else (
                COLORS['warning'] if sig.confidence > 0.5 else COLORS['text_muted']
            )

            # CAN ID formatado: 8 dígitos hex (estendido 29 bits) ou 4 (padrão 11 bits).
            id_str = f"0x{sig.can_id:08X}" if sig.is_extended else f"0x{sig.can_id:04X}"
            # PGN + nome (J1939) quando houver; senão "—". SPN em segunda linha.
            pgn_str = f"{sig.pgn} — {sig.pgn_name}" if sig.pgn else "—"
            if sig.spn_name:
                pgn_str += f"\n{sig.spn_name}"

            # Posição do sinal: byte único ou faixa de bytes + ordem (endianness).
            byte_str = f"Byte {sig.byte_index}"
            if sig.length_bytes > 1:
                byte_str += f"–{sig.byte_index + sig.length_bytes - 1} ({sig.byte_order})"

            # Conteúdo das 6 colunas (texto + alinhamento), na ordem do cabeçalho.
            items = [
                (conf_pct, Qt.AlignCenter),
                (id_str,   Qt.AlignCenter),
                (pgn_str,  Qt.AlignLeft),
                (byte_str, Qt.AlignCenter),
                (sig.formula_str, Qt.AlignLeft),
                ("", Qt.AlignCenter),
            ]
            # Cria cada célula: alinha, torna NÃO editável, colore a confiança
            # (col 0) e dá fundo verde-escuro à linha do melhor candidato (i==0).
            for col, (text, align) in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(align | Qt.AlignVCenter)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0:
                    item.setForeground(QColor(color))
                if i == 0:
                    item.setBackground(QColor("#1a3020"))
                self._results_table.setItem(row, col, item)

        # Update card status
        # Atualiza o card do teste com um resumo do melhor candidato.
        if key in self._cards:
            if best.pgn:
                summary = f"PGN {best.pgn} ({best.pgn_name}) • Byte {best.byte_index}"
            else:
                summary = f"ID 0x{best.can_id:08X} • Byte {best.byte_index}"
            self._cards[key].set_status(
                "done", summary,
                f"PGN {best.pgn}" if best.pgn else f"0x{best.can_id:08X}"
            )

        # Formula detail
        # Bloco textual com a descrição, a fórmula de conversão, a confiança e a
        # variação bruta observada do melhor candidato.
        self._lbl_formula_detail.setText(
            f"✅ Melhor candidato: {best.describe()}\n"
            f"   {best.formula_str}\n"
            f"   Confiança: {best.confidence*100:.0f}%   Variação bruta: {best.test_range:.0f}"
        )
        self._lbl_status.setText(f"✅ {len(results)} sinal(is) encontrado(s)!")
        self._lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-weight: bold;")

    # ── Phase UI helpers ──────────────────────────────────────────────────────

    def _set_phase_active(self, idx: int):
        """Realça o indicador de fases conforme a fase atual.

        Pinta as fases já concluídas (índice < idx) em verde, a fase atual
        (índice == idx) em accent/negrito, e as futuras (índice > idx) apagadas.

        Args:
            idx: índice da fase ativa (0=Baseline, 1=Teste, 2=Análise, 3=Resultado).
        """
        for i, lbl in enumerate(self._phase_labels):
            if i < idx:
                lbl.setStyleSheet(f"""
                    background-color: {COLORS['success']}33;
                    color: {COLORS['success']};
                    border-radius: 4px; font-size: 11px; padding: 0 8px;
                """)
            elif i == idx:
                lbl.setStyleSheet(f"""
                    background-color: {COLORS['accent']};
                    color: white; font-weight: bold;
                    border-radius: 4px; font-size: 11px; padding: 0 8px;
                """)
            else:
                lbl.setStyleSheet(f"""
                    background-color: {COLORS['bg_header']};
                    color: {COLORS['text_muted']};
                    border-radius: 4px; font-size: 11px; padding: 0 8px;
                """)

    def _reset_phase_ui(self):
        """Zera a barra de progresso e apaga todos os indicadores de fase.

        Usado ao selecionar um novo teste ou ao parar/interromper um teste.
        """
        self._progress.setValue(0)
        for lbl in self._phase_labels:
            lbl.setStyleSheet(f"""
                background-color: {COLORS['bg_header']};
                color: {COLORS['text_muted']};
                border-radius: 4px; font-size: 11px; padding: 0 8px;
            """)

    def get_saved_results(self) -> dict:
        """Retorna os resultados acumulados (chave-do-teste -> candidatos).

        É o canal pelo qual a aba "Sinais Mapeados" obtém os sinais descobertos
        nesta aba. Retorna o próprio dicionário interno (não uma cópia).
        """
        return self._saved_results

    def set_bus(self, bus):
        """Troca a referência ao CANBus em tempo de execução.

        Útil quando o barramento é (re)criado após a UI já existir (ex.:
        conectar/trocar de modo). Reavalia a habilitação dos botões: "Iniciar
        Teste" só se houver teste ativo e o barramento estiver conectado;
        "Simular Ação" só em Modo Simulação.
        """
        self._bus = bus
        self._btn_start.setEnabled(bus.is_connected if self._active_test else False)
        self._btn_sim.setEnabled(bus.is_simulation)
