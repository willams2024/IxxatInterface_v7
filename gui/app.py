"""Main application window."""
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  app.py — Janela principal (PyQt5) do mapeador de sinais CAN              ║
# ║                                                                          ║
# ║  Este arquivo monta toda a interface gráfica do programa:                ║
# ║    • ConnectionWidget  → painel superior de conexão (canal/baudrate/etc) ║
# ║    • MainWindow        → janela principal, abas e menus                   ║
# ║    • ReplayControlDialog → painel flutuante de controle de replay CSV     ║
# ║    • _CANDispatcher    → thread auxiliar (ver classe abaixo)             ║
# ║                                                                          ║
# ║  IMPORTANTE PARA MANUTENÇÃO: este código é compilado em .exe via          ║
# ║  PyInstaller e está em produção. Os caminhos de assets (ícone, seta)     ║
# ║  usam sys._MEIPASS para funcionar dentro do executável empacotado.       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

import os
import time
from datetime import datetime

# Imports do PyQt5:
#   - QtCore: tipos base, timers, threads e mecanismo de sinais/slots
#   - QtGui: cor, fonte e ícone (QIcon usado para o ícone da janela)
#   - QtWidgets: todos os componentes visuais (janela, botões, abas, etc.)
from PyQt5.QtCore import Qt, QTimer, pyqtSlot, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QComboBox, QSpinBox,
    QStatusBar, QGroupBox, QFrame, QCheckBox, QAction, QMenuBar,
    QFileDialog, QMessageBox, QProgressDialog, QInputDialog,
    QDialog, QProgressBar,
)

from core.can_bus import CANBus, CANMessage   # camada de acesso ao barramento CAN e ao tipo de mensagem
from gui.monitor import MonitorTab            # aba "Monitor CAN" (tráfego em tempo real)
from gui.discovery import DiscoveryTab        # aba "Descoberta de Sinais"
from gui.signals_tab import SignalsTab        # aba "Sinais Mapeados"
from gui.styles import COLORS, DARK_STYLE     # paleta de cores e folha de estilo escura


class _CANDispatcher(QThread):
    """Thread em segundo plano que lê o CANBus e reemite cada mensagem como sinal Qt.

    POR QUE ESTA CLASSE EXISTE
    --------------------------
    O recebimento de mensagens CAN acontece fora da thread da interface gráfica
    (a "GUI thread"). Tocar em widgets Qt a partir de outra thread é proibido e
    causa travamentos. A solução padrão do Qt é usar sinais (pyqtSignal): eles
    podem ser emitidos de qualquer thread e são entregues com segurança na GUI
    thread por meio da fila de eventos.

    Assim, esta QThread fica registrada como "listener" do barramento; quando
    chega uma mensagem, ela apenas emite `message_received`, e quem estiver
    conectado a esse sinal recebe a mensagem já dentro da GUI thread.

    Observação: na versão atual a MainWindow registra um listener direto no bus
    (`_on_message`), mas esta classe permanece disponível como mecanismo
    thread-safe de despacho.
    """
    # Sinal Qt emitido a cada mensagem recebida. O parâmetro é `object` porque
    # carrega uma instância de CANMessage (tipo Python arbitrário).
    message_received = pyqtSignal(object)

    def __init__(self, bus: CANBus):
        super().__init__()
        self._bus = bus          # referência ao barramento que será escutado
        self._running = False    # flag de controle do laço da thread

    def run(self):
        """Laço principal da thread (executado em segundo plano)."""
        self._running = True
        # Registra o callback que será chamado pelo bus a cada mensagem recebida.
        self._bus.add_listener(self._on_msg)
        # Mantém a thread viva enquanto _running for True. O msleep(50) cede a CPU
        # (não faz "busy wait") — as mensagens chegam via callback, não por polling.
        while self._running:
            self.msleep(50)
        # Ao encerrar, remove o listener para não vazar referências/callbacks.
        self._bus.remove_listener(self._on_msg)

    def _on_msg(self, msg: CANMessage):
        """Callback chamado pelo bus; apenas repassa a mensagem como sinal Qt."""
        self.message_received.emit(msg)

    def stop(self):
        """Solicita o encerramento ordenado da thread e aguarda até 2s."""
        self._running = False   # faz o laço de run() terminar
        self.quit()             # pede para a thread sair do loop de eventos
        self.wait(2000)         # bloqueia até a thread finalizar (timeout de 2000 ms)


class ConnectionWidget(QGroupBox):
    """Painel superior com os controles de conexão ao dispositivo IXXAT.

    Reúne: seleção de canal, baudrate (editável), caixas "Modo Simulação" e
    "Listen-Only", o botão Conectar/Desconectar e o indicador de status (a
    "bolinha" colorida + texto). É um QGroupBox titulado "Conexão".
    """

    def __init__(self, bus: CANBus, parent=None):
        super().__init__("Conexão", parent)   # título do grupo exibido na borda
        self._bus = bus                        # referência ao barramento CAN
        self._setup_ui()                       # monta todos os widgets do painel

    def _setup_ui(self):
        """Cria e posiciona todos os controles do painel de conexão."""
        # Layout horizontal: os controles ficam lado a lado em uma única linha.
        layout = QHBoxLayout(self)
        layout.setSpacing(10)

        # ── Seleção de canal CAN (0, 1 ou 2) ──────────────────────────────
        layout.addWidget(QLabel("Canal:"))
        self._cmb_channel = QComboBox()
        self._cmb_channel.addItems(["0", "1", "2"])
        self._cmb_channel.setMaximumWidth(60)
        layout.addWidget(self._cmb_channel)

        # ── Seleção/edição de baudrate (bits por segundo) ─────────────────
        layout.addWidget(QLabel("Baudrate:"))
        self._cmb_baud = QComboBox()
        self._cmb_baud.setEditable(True)   # editável: permite digitar valor fora da lista
        self._cmb_baud.addItems(["100000", "125000", "250000", "500000", "800000", "1000000"])
        self._cmb_baud.setCurrentIndex(2)  # índice 2 = 250000 bps (padrão J1939)
        self._cmb_baud.setMinimumWidth(140)
        self._cmb_baud.setMaximumWidth(150)
        self._cmb_baud.setToolTip("Selecione na seta ▼ ou digite um baudrate personalizado")
        self._cmb_baud.lineEdit().setPlaceholderText("ex: 333333")
        # Estilo customizado: garante seta visível mesmo no modo editável
        # Usa PNG da seta gerado em assets/arrow_down.png
        # sys._MEIPASS é definido apenas quando rodando dentro do .exe do
        # PyInstaller (pasta temporária onde os assets são extraídos). Quando
        # rodando como .py normal, esse atributo não existe e usamos o getattr
        # com fallback para a raiz do projeto (dois níveis acima deste arquivo).
        import sys
        _base = getattr(sys, "_MEIPASS",
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # Caminho do PNG da seta; barras invertidas viram "/" porque o Qt
        # stylesheet (CSS) espera caminhos com barra normal.
        _arrow_path = os.path.join(_base, "assets", "arrow_down.png").replace("\\", "/")
        self._cmb_baud.setStyleSheet(f"""
            QComboBox {{
                padding-right: 24px;
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border-left: 1px solid #6c63ff;
                background-color: #2a2a44;
            }}
            QComboBox::drop-down:hover {{
                background-color: #6c63ff;
            }}
            QComboBox::down-arrow {{
                image: url({_arrow_path});
                width: 16px;
                height: 10px;
            }}
        """)
        layout.addWidget(self._cmb_baud)

        # ── Caixa "Modo Simulação" ────────────────────────────────────────
        # Quando marcada, o programa gera tráfego J1939 falso, permitindo
        # testar a interface sem o hardware IXXAT conectado.
        self._chk_sim = QCheckBox("Modo Simulação")
        self._chk_sim.setToolTip("Gera tráfego J1939 sintético para testes sem hardware")
        layout.addWidget(self._chk_sim)

        # ── Caixa "Listen-Only" (modo passivo) ────────────────────────────
        # Marcada por padrão (padrão seguro). Em listen-only o controlador
        # IXXAT nunca transmite (nem ACK), evitando interferir no veículo.
        self._chk_listen = QCheckBox("🔒 Listen-Only")
        self._chk_listen.setChecked(True)
        self._chk_listen.setToolTip(
            "MARCADO (padrão seguro):\n"
            "  • Controlador IXXAT 100% passivo, não envia ACK\n"
            "  • Seguro para conectar em veículos\n\n"
            "DESMARCADO (modo bancada):\n"
            "  • Controlador envia ACK normalmente\n"
            "  • Necessário se a bancada tem poucos nós (ECU+IXXAT só)\n"
            "  • Sem ACK, a ECU bus-offs e para de transmitir"
        )
        layout.addWidget(self._chk_listen)

        # ── Botão Conectar / Desconectar ──────────────────────────────────
        # O mesmo botão alterna entre os dois estados; o clique chama
        # _toggle_connect, que decide o que fazer com base no estado atual.
        self._btn_connect = QPushButton("🔌  Conectar")
        self._btn_connect.setObjectName("btn_success")   # objectName usado pela folha de estilo (verde)
        self._btn_connect.setMinimumWidth(130)
        self._btn_connect.clicked.connect(self._toggle_connect)
        layout.addWidget(self._btn_connect)

        layout.addStretch()   # empurra o indicador de status para a direita

        # ── Indicador de status ("pílula") ────────────────────────────────
        # Bolinha colorida (vermelha = desconectado, verde = conectado) +
        # texto descritivo do estado/modo.
        self._lbl_dot = QLabel("⬤")
        self._lbl_dot.setStyleSheet(f"color: {COLORS['error']}; font-size: 18px;")
        layout.addWidget(self._lbl_dot)
        self._lbl_status = QLabel("Desconectado")
        self._lbl_status.setStyleSheet(f"color: {COLORS['text_muted']};")
        layout.addWidget(self._lbl_status)

    @pyqtSlot()
    def _toggle_connect(self):
        """Conecta ou desconecta do barramento, conforme o estado atual.

        Fluxo:
          • Se já conectado → desconecta e restaura a UI ao estado inicial.
          • Se desconectado → valida o baudrate, lê as opções (simulação /
            listen-only), tenta conectar pelo CANBus e, em caso de sucesso,
            inicia uma nova sessão de log e atualiza o indicador visual.
        """
        if self._bus.is_connected:
            # ---- Caminho de DESCONEXÃO ----
            self._bus.disconnect()
            self._btn_connect.setText("🔌  Conectar")
            self._btn_connect.setObjectName("btn_success")
            self._btn_connect.setStyleSheet("")   # limpa o estilo inline para voltar ao tema padrão
            self._lbl_dot.setStyleSheet(f"color: {COLORS['error']}; font-size: 18px;")
            self._lbl_status.setText("Desconectado")
        else:
            # ---- Caminho de CONEXÃO ----
            ch = int(self._cmb_channel.currentText())   # canal escolhido (0/1/2)
            # Validação do baudrate: remove separadores de milhar (ponto/vírgula)
            # e espaços, então converte para inteiro. Se falhar, avisa e aborta.
            try:
                baud = int(self._cmb_baud.currentText().replace(".", "").replace(",", "").strip())
            except ValueError:
                QMessageBox.warning(self.parent(), "Baudrate inválido",
                                    "Digite um valor numérico válido.\nExemplo: 250000")
                return
            # Faixa válida aceita pelo controlador: 10 kbps a 1 Mbps.
            if not (10000 <= baud <= 1000000):
                QMessageBox.warning(self.parent(), "Baudrate fora do intervalo",
                                    f"Baudrate deve estar entre 10.000 e 1.000.000 bps.\nValor informado: {baud}")
                return
            sim = self._chk_sim.isChecked()       # modo simulação ligado?
            listen = self._chk_listen.isChecked() # listen-only (passivo) ligado?
            # Tenta abrir a conexão; retorna (ok, mensagem). `msg` traz o erro
            # quando ok == False.
            ok, msg = self._bus.connect(channel=ch, bitrate=baud,
                                        simulation=sim, listen_only=listen)
            if ok:
                # Inicia uma nova sessão de log para esta conexão.
                # Envolto em try/except para que uma falha de logging nunca
                # impeça a conexão de funcionar.
                try:
                    from core.discovery import start_new_session
                    start_new_session()
                except Exception:
                    pass
                # Reconfigura o botão para o estado "Desconectar" (vermelho).
                self._btn_connect.setText("⏏  Desconectar")
                self._btn_connect.setObjectName("btn_danger")
                self._btn_connect.setStyleSheet(f"background-color: {COLORS['error']};")
                self._lbl_dot.setStyleSheet(f"color: {COLORS['success']}; font-size: 18px;")
                # Monta o sufixo de modo mostrado no status, por prioridade:
                #   simulação > listen-only > modo normal (com ACK, atenção!).
                if sim:
                    mode_txt = " [SIM]"
                elif listen:
                    mode_txt = " 🔒 LISTEN-ONLY"
                else:
                    mode_txt = " ⚠️ MODO NORMAL (ACK)"
                self._lbl_status.setText(f"Conectado{mode_txt} — {baud} bps")
                self._lbl_status.setStyleSheet(f"color: {COLORS['success']}; font-weight: bold;")
            else:
                # Conexão falhou: exibe a mensagem de erro retornada pelo bus.
                QMessageBox.critical(self.parent(), "Erro de Conexão", msg)

    def is_simulation(self) -> bool:
        """Retorna True se a caixa 'Modo Simulação' estiver marcada."""
        return self._chk_sim.isChecked()


class ReplayControlDialog(QDialog):
    """Painel flutuante de controle do replay CSV (play/pause/stop/velocidade).

    Quando o usuário carrega um log CSV da IXXAT miniMon (menu Ferramentas →
    Carregar Log CSV), este diálogo aparece flutuando sobre a janela principal.
    Ele NÃO é modal: o usuário continua podendo trabalhar nas abas enquanto o
    replay roda. Possui:
      • PLAY/PAUSAR  → alterna a reprodução das mensagens
      • REINICIAR    → volta ao começo do log sem recarregar o CSV
      • PARAR        → encerra o replay e fecha o painel
      • Velocidade   → multiplica a velocidade de reprodução (0.25× a 20×)
      • Barra de progresso + tempo/contagem, atualizados por um timer de polling
    """

    def __init__(self, bus: CANBus, filename: str, total_sec: float, parent=None):
        super().__init__(parent)
        self._bus = bus
        self.setWindowTitle("🎬  Controle de Replay")
        # Qt.Tool = janela utilitária leve; WindowStaysOnTopHint = sempre visível
        # acima da janela principal, para o usuário não perder os controles.
        self.setWindowFlags(self.windowFlags() | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(440)
        self.setModal(False)            # não-modal: não bloqueia o resto do app
        self._total_sec = total_sec     # duração total estimada do log (segundos)
        self._setup_ui(filename, total_sec)

        # Timer de polling — a cada 100 ms chama _refresh para atualizar a barra
        # de progresso. Usa-se polling (em vez de sinal) porque o progresso do
        # replay é uma simples contagem lida do bus, mais barata de consultar.
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._refresh)
        self._poll.start()

    def _setup_ui(self, filename: str, total_sec: float):
        """Monta os widgets do painel: nome do arquivo, botões, velocidade e progresso."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Nome do arquivo
        lbl_file = QLabel(f"📂  {filename}")
        lbl_file.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        layout.addWidget(lbl_file)

        # Linha 1: Botões grandes Play/Pause/Restart/Stop
        # Os pesos (3, 2, 2) passados ao addWidget definem a largura relativa
        # de cada botão na linha (PLAY é o mais largo).
        btn_row = QHBoxLayout()
        self._btn_play = QPushButton("▶  PLAY")
        self._btn_play.setObjectName("btn_success")
        self._btn_play.setMinimumHeight(40)
        self._btn_play.setStyleSheet(
            f"background-color: {COLORS['success']}; font-size: 14px; font-weight: bold;")
        self._btn_play.clicked.connect(self._toggle_play)
        btn_row.addWidget(self._btn_play, 3)

        self._btn_restart = QPushButton("🔄  REINICIAR")
        self._btn_restart.setMinimumHeight(40)
        self._btn_restart.setStyleSheet(
            f"background-color: {COLORS['accent']}; color: white; font-size: 12px; font-weight: bold;")
        self._btn_restart.setToolTip("Volta ao começo do log — útil para testar outro sinal sem recarregar o CSV")
        self._btn_restart.clicked.connect(self._restart)
        btn_row.addWidget(self._btn_restart, 2)

        self._btn_stop = QPushButton("⏹  PARAR")
        self._btn_stop.setObjectName("btn_danger")
        self._btn_stop.setMinimumHeight(40)
        self._btn_stop.setStyleSheet(
            f"background-color: {COLORS['error']}; font-size: 12px;")
        self._btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self._btn_stop, 2)
        layout.addLayout(btn_row)

        # Linha 2: Velocidade de reprodução
        # O fator multiplica a velocidade real do log (1× = tempo real).
        spd_row = QHBoxLayout()
        spd_row.addWidget(QLabel("Velocidade:"))
        self._cmb_speed = QComboBox()
        self._cmb_speed.addItems(["0.25×", "0.5×", "1×", "2×", "5×", "10×", "20×"])
        self._cmb_speed.setCurrentText("1×")
        self._cmb_speed.currentTextChanged.connect(self._on_speed_change)
        spd_row.addWidget(self._cmb_speed)
        spd_row.addStretch()
        layout.addLayout(spd_row)

        # Linha 3: Barra de progresso (0-100%) + rótulo de tempo/contagem
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setStyleSheet("""
            QProgressBar { border: 1px solid #2d2d44; border-radius: 4px;
                           text-align: center; height: 22px; }
            QProgressBar::chunk { background-color: #6c63ff; border-radius: 3px; }
        """)
        layout.addWidget(self._progress)

        self._lbl_time = QLabel("Tempo: 0.0s / " + f"{total_sec:.1f}s   |   "
                                "Mensagens: 0 / 0")
        self._lbl_time.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        self._lbl_time.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._lbl_time)

        # Dica
        tip = QLabel("💡 Dica: vá na aba <b>Descoberta de Sinais</b>, escolha o sinal,\n"
                     "depois clique <b>PLAY</b> aqui e em seguida <b>Iniciar Teste</b>.")
        tip.setStyleSheet(f"color: {COLORS['accent']}; font-size: 11px; padding: 6px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

    @pyqtSlot()
    def _toggle_play(self):
        """Alterna entre reproduzir (PLAY) e pausar (PAUSAR) o replay.

        O bus retorna o estado resultante: paused=True significa que acabou de
        pausar (mostramos PLAY, verde, para retomar); paused=False significa que
        está tocando (mostramos PAUSAR, amarelo).
        """
        paused = self._bus.toggle_replay_pause()
        if paused:
            self._btn_play.setText("▶  PLAY")
            self._btn_play.setStyleSheet(
                f"background-color: {COLORS['success']}; font-size: 14px; font-weight: bold;")
        else:
            self._btn_play.setText("⏸  PAUSAR")
            self._btn_play.setStyleSheet(
                f"background-color: {COLORS['warning']}; font-size: 14px; font-weight: bold;")

    @pyqtSlot()
    def _restart(self):
        """Reinicia o replay do começo (mantém o CSV em memória).

        Útil para repetir o log e testar outro sinal sem ter que recarregar o
        arquivo. Recomeça pausado (start_paused=True) e restaura a barra e o
        botão PLAY ao estado inicial; reativa o timer de polling se preciso.
        """
        ok = self._bus.restart_replay(start_paused=True)
        if not ok:
            return
        # Reabilita UI (caso o replay anterior tivesse terminado e desabilitado
        # o botão / parado o polling).
        self._btn_play.setEnabled(True)
        self._btn_play.setText("▶  PLAY")
        self._btn_play.setStyleSheet(
            f"background-color: {COLORS['success']}; font-size: 14px; font-weight: bold;")
        self._progress.setValue(0)
        if not self._poll.isActive():
            self._poll.start()

    @pyqtSlot()
    def _stop(self):
        """Para o replay: encerra o polling, desconecta o bus e fecha o diálogo."""
        self._poll.stop()
        self._bus.disconnect()
        self.accept()   # fecha o QDialog com resultado "aceito"

    @pyqtSlot(str)
    def _on_speed_change(self, text: str):
        """Aplica a nova velocidade ao replay quando o usuário muda o combo.

        Converte o texto (ex.: '2×') para float removendo o símbolo '×'. Falhas
        de parsing são silenciosamente ignoradas (mantém a velocidade atual).
        """
        try:
            factor = float(text.replace("×", "").strip())
            self._bus.set_replay_speed(factor)
        except ValueError:
            pass

    @pyqtSlot()
    def _refresh(self):
        """Atualiza barra de progresso e rótulo de tempo (chamado pelo timer).

        Lê do bus a contagem (done, total) de mensagens reproduzidas, calcula o
        percentual e estima o tempo decorrido proporcionalmente. Quando o log
        termina (done >= total) ou o bus desconecta, para o polling e marca o
        botão como "Concluído".
        """
        done, total = self._bus.replay_progress
        if total <= 0:
            return   # ainda não há contagem válida; evita divisão por zero
        pct = int(done * 100 / total)
        self._progress.setValue(pct)
        # Estimativa de tempo já reproduzido = duração total × fração concluída.
        sec_done = self._total_sec * done / total
        self._lbl_time.setText(
            f"Tempo: {sec_done:.1f}s / {self._total_sec:.1f}s   |   "
            f"Mensagens: {done:,} / {total:,}"
        )
        # Fim do replay (ou bus caiu): encerra o polling e bloqueia o botão.
        if done >= total or not self._bus.is_connected:
            self._poll.stop()
            self._btn_play.setText("✓ Concluído")
            self._btn_play.setEnabled(False)

    def closeEvent(self, event):
        """Garante limpeza ao fechar a janela pelo 'X': para o timer e desconecta."""
        self._poll.stop()
        if self._bus.is_connected:
            self._bus.disconnect()
        event.accept()


class MainWindow(QMainWindow):
    """Janela principal do aplicativo: cabeçalho, painel de conexão, abas e menus.

    Responsabilidades:
      • Criar o CANBus único compartilhado por todos os componentes.
      • Montar as abas (Monitor, Descoberta, Sinais, Banco J1939, Sobre).
      • Rotear cada mensagem CAN recebida para as abas interessadas.
      • Manter um timer de status (rodapé) e gerenciar o replay de CSV.
    """

    def __init__(self):
        super().__init__()
        self._bus = CANBus()   # barramento CAN central, injetado nos widgets/abas
        # Referências opcionais (None enquanto não criadas):
        self._dispatcher: _CANDispatcher | None = None          # thread de despacho (ver _CANDispatcher)
        self._replay_dialog: ReplayControlDialog | None = None  # painel flutuante de replay
        self.setWindowTitle("IxxatInterface  v7.0  —  CAN Signal Mapper")
        # ── Ícone (janela + barra de tarefas) ─────────────────────────────────
        self._apply_window_icon()
        self.setMinimumSize(1100, 700)   # tamanho mínimo da janela
        self.resize(1280, 780)           # tamanho inicial preferido
        self._build_ui()                 # monta cabeçalho, conexão, abas, status bar
        self._build_menu()               # monta a barra de menus (Arquivo / Ferramentas)
        # Timer de estatísticas: a cada 500 ms atualiza o rodapé (status/contagem).
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_status)
        self._stats_timer.start(500)

    def _apply_window_icon(self):
        """Carrega o ícone do programa. Funciona tanto no .py quanto no .exe (PyInstaller).

        Igual à lógica usada para a seta do combo: usa sys._MEIPASS (definido
        pelo PyInstaller dentro do .exe) e, fora dele, a raiz do projeto.
        Procura primeiro um .ico (ideal para Windows) e, se não houver, um .png.
        """
        import sys
        base = getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for candidate in (os.path.join(base, "assets", "icon.ico"),
                          os.path.join(base, "assets", "icon.png")):
            if os.path.exists(candidate):
                self.setWindowIcon(QIcon(candidate))
                return   # usa o primeiro arquivo encontrado e para a busca

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        """Constrói o conteúdo central: cabeçalho, painel de conexão, abas e rodapé."""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        # Header bar
        header = QHBoxLayout()
        title = QLabel("IXXAT Interface")
        title.setStyleSheet(f"""
            font-size: 20px; font-weight: bold;
            color: {COLORS['accent']};
            letter-spacing: 1px;
        """)
        header.addWidget(title)
        ver = QLabel("v7.0")
        ver.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 13px;")
        header.addWidget(ver)
        header.addStretch()
        root.addLayout(header)

        # Painel de conexão (compartilha o mesmo bus da janela).
        self._conn_widget = ConnectionWidget(self._bus)
        root.addWidget(self._conn_widget)

        # Conjunto de abas. Todas que precisam do barramento recebem self._bus.
        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        # Aba 1 — Monitor CAN: lista o tráfego em tempo real.
        self._monitor_tab = MonitorTab()
        self._tabs.addTab(self._monitor_tab, "📡  Monitor CAN")

        # Aba 2 — Descoberta de Sinais: identifica sinais (RPM, velocidade, etc.).
        self._discovery_tab = DiscoveryTab(self._bus)
        self._tabs.addTab(self._discovery_tab, "🔍  Descoberta de Sinais")

        # Aba 3 — Sinais Mapeados: consolida os sinais já descobertos.
        self._signals_tab = SignalsTab()
        self._signals_tab.set_bus(self._bus)
        self._tabs.addTab(self._signals_tab, "📋  Sinais Mapeados")

        # Aba 4 — Banco J1939 (tabela estática) e Aba 5 — Sobre (texto informativo).
        self._tabs.addTab(self._build_pgn_tab(), "📖  Banco J1939")
        self._tabs.addTab(self._build_about_tab(), "ℹ️  Sobre")

        # Barra de status (rodapé) com indicadores permanentes à direita.
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._lbl_stat_msgs = QLabel("Msgs: 0")
        self._lbl_stat_rate = QLabel("0 msg/s")
        self._lbl_stat_conn = QLabel("Desconectado")
        self._lbl_stat_conn.setStyleSheet(f"color: {COLORS['error']};")
        for w in (self._lbl_stat_msgs, self._lbl_stat_rate, self._lbl_stat_conn):
            self._status_bar.addPermanentWidget(w)

        # Registra _on_message como listener do bus: toda mensagem CAN recebida
        # será roteada por ele para as abas Monitor e Descoberta.
        self._bus.add_listener(self._on_message)

        # Ao trocar de aba, _on_tab_changed sincroniza os resultados da
        # Descoberta com a aba de Sinais Mapeados.
        self._tabs.currentChanged.connect(self._on_tab_changed)

    def _build_pgn_tab(self) -> QWidget:
        """Monta a aba 'Banco J1939': tabela com PGNs/SPNs e suas fórmulas de conversão."""
        from core.j1939 import PGN_DATABASE
        from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(QLabel("Banco de dados J1939 embutido").setObjectName("label_title") or QLabel("Banco de Dados J1939"))

        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["PGN", "Sigla", "Nome", "SPN", "Descrição / Fórmula"])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)

        # Preenche a tabela percorrendo o banco J1939 ordenado por número de PGN.
        # Cada célula é marcada como não-editável (somente leitura).
        for pgn_num, pgn in sorted(PGN_DATABASE.items()):
            if not pgn.spns:
                # PGN sem SPNs definidos: uma única linha com "—" nas colunas de SPN.
                row = table.rowCount()
                table.insertRow(row)
                for col, text in enumerate([str(pgn_num), pgn.acronym, pgn.name, "—", "—"]):
                    item = QTableWidgetItem(text)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    table.setItem(row, col, item)
            else:
                # PGN com SPNs: uma linha por SPN, montando a fórmula de conversão.
                for spn in pgn.spns:
                    row = table.rowCount()
                    table.insertRow(row)
                    # Fórmula de engenharia: valor_físico = raw × fator (+ offset) [unidade].
                    formula = f"valor = raw × {spn.factor}"
                    if spn.offset:
                        formula += f" + ({spn.offset})"
                    formula += f"  [{spn.unit}]" if spn.unit else ""
                    for col, text in enumerate([
                        str(pgn_num), pgn.acronym, pgn.name,
                        f"{spn.number} — {spn.name}", formula
                    ]):
                        item = QTableWidgetItem(text)
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        table.setItem(row, col, item)
        layout.addWidget(table)
        return w

    def _build_about_tab(self) -> QWidget:
        """Monta a aba 'Sobre' com a descrição do programa, modo listen-only e recursos."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setAlignment(Qt.AlignTop)

        title = QLabel("IxxatInterface v7.0")
        title.setStyleSheet(f"font-size: 26px; font-weight: bold; color: {COLORS['accent']};")
        layout.addWidget(title)

        info = QLabel(
            "Ferramenta de monitoramento e mapeamento de sinais CAN via IXXAT USB-to-CAN V2.\n\n"
            "🔒  MODO LISTEN-ONLY ATIVO\n"
            "Este programa opera em modo 100% passivo — o controlador IXXAT\n"
            "é configurado com CAN_OPMODE_LISTONLY, o que significa que:\n"
            "  • Nunca transmite mensagens no barramento\n"
            "  • Não envia ACK frames\n"
            "  • Não envia error frames\n"
            "  • Apenas escuta o tráfego existente\n"
            "  • Seguro para conectar em veículos em operação\n\n"
            "Funcionalidades:\n"
            "  • Monitor CAN em tempo real com decodificação J1939 FMS v05\n"
            "  • Descoberta guiada de 22 sinais (RPM, velocidade, freio, etc.)\n"
            "  • Detecção automática de sinais 1-byte e 2-byte (LE/BE)\n"
            "  • Suporte a CAN 11-bit proprietário e 29-bit J1939\n"
            "  • Exportação Excel formato VIRLOC + Relatório PDF\n"
            "  • Replay de logs CSV da IXXAT miniMon\n"
            "  • Banco de dados J1939 com ~220 SPNs\n"
            "  • Modo simulação para testes sem hardware\n\n"
            "Hardware suportado:\n"
            "  • IXXAT USB-to-CAN V2 compact (HMS Networks)\n"
            "  • Qualquer interface compatível com python-can\n\n"
            "Driver: https://www.hms-networks.com/p/1-01-0281-12001-ixxat-usb-to-can-v2-compact"
        )
        info.setStyleSheet(f"color: {COLORS['text']}; font-size: 13px; line-height: 1.8;")
        info.setWordWrap(True)
        layout.addWidget(info)
        return w

    def _build_menu(self):
        """Monta a barra de menus: 'Arquivo' (log/sair) e 'Ferramentas' (monitor/replay/logs)."""
        mb = self.menuBar()

        # Menu Arquivo
        file_menu = mb.addMenu("Arquivo")
        act_log = QAction("Salvar Log CAN...", self)
        act_log.triggered.connect(self._start_log)   # inicia gravação do log do monitor
        file_menu.addAction(act_log)
        file_menu.addSeparator()
        act_exit = QAction("Sair", self)
        act_exit.triggered.connect(self.close)       # fecha a janela (dispara closeEvent)
        file_menu.addAction(act_exit)

        # Menu Ferramentas
        tools_menu = mb.addMenu("Ferramentas")
        act_clear = QAction("Limpar Monitor", self)
        act_clear.triggered.connect(self._monitor_tab._clear)   # esvazia a lista do monitor
        tools_menu.addAction(act_clear)

        tools_menu.addSeparator()
        # Carregar log CSV para replay (atalho Ctrl+L).
        act_replay = QAction("📂  Carregar Log CSV (replay)...", self)
        act_replay.setShortcut("Ctrl+L")
        act_replay.triggered.connect(self._load_csv_replay)
        tools_menu.addAction(act_replay)

        tools_menu.addSeparator()
        # Abrir a pasta de logs de descoberta no explorador (atalho Ctrl+D).
        act_open_logs = QAction("📂  Abrir pasta de logs (Discovery)", self)
        act_open_logs.setShortcut("Ctrl+D")
        act_open_logs.setStatusTip(
            "Abre a pasta onde ficam os logs de candidatos descartados pela descoberta")
        act_open_logs.triggered.connect(self._open_logs_folder)
        tools_menu.addAction(act_open_logs)

    @pyqtSlot()
    def _open_logs_folder(self):
        """Abre a pasta de logs de descoberta no explorador de arquivos.

        Cria a pasta se ainda não existir e usa o comando nativo de cada SO
        (Windows / macOS / Linux) para abri-la no gerenciador de arquivos.
        """
        from core.discovery import DEBUG_LOG_DIR
        import sys, subprocess
        try:
            os.makedirs(DEBUG_LOG_DIR, exist_ok=True)   # garante que a pasta exista
            if sys.platform == "win32":
                os.startfile(DEBUG_LOG_DIR)                 # Windows
            elif sys.platform == "darwin":
                subprocess.Popen(["open", DEBUG_LOG_DIR])   # macOS
            else:
                subprocess.Popen(["xdg-open", DEBUG_LOG_DIR])  # Linux
        except Exception as e:
            QMessageBox.critical(
                self, "Erro ao abrir pasta",
                f"Não foi possível abrir a pasta de logs:\n{DEBUG_LOG_DIR}\n\n{e}"
            )

    # ── Roteamento de mensagens ─────────────────────────────────────────────

    def _on_message(self, msg: CANMessage):
        """Callback do bus: encaminha cada mensagem CAN recebida às abas.

        ATENÇÃO: este callback é invocado pela thread de recebimento do bus.
        As abas Monitor e Descoberta tratam internamente a entrega segura na
        GUI thread. Este método é o ponto único de roteamento de tráfego CAN.
        """
        self._monitor_tab.on_message(msg)
        self._discovery_tab.on_message(msg)

    @pyqtSlot(int)
    def _on_tab_changed(self, idx: int):
        """Ao abrir a aba 'Sinais Mapeados', importa os resultados da Descoberta.

        Assim, ao trocar para essa aba, ela já reflete os sinais descobertos
        mais recentes sem o usuário precisar exportar/recarregar manualmente.
        """
        if self._tabs.widget(idx) is self._signals_tab:
            results = self._discovery_tab.get_saved_results()
            if results:
                self._signals_tab.update_from_discovery(results)

    # ── Atualização de status (rodapé) ──────────────────────────────────────

    @pyqtSlot()
    def _update_status(self):
        """Atualiza o rodapé a cada 500 ms: estado da conexão e contagem de mensagens."""
        connected = self._bus.is_connected
        if connected:
            sim = " [SIM]" if self._bus.is_simulation else ""
            self._lbl_stat_conn.setText(f"Conectado{sim}")
            self._lbl_stat_conn.setStyleSheet(f"color: {COLORS['success']}; font-weight: bold;")
        else:
            self._lbl_stat_conn.setText("Desconectado")
            self._lbl_stat_conn.setStyleSheet(f"color: {COLORS['error']};")
        self._lbl_stat_msgs.setText(f"Msgs: {self._bus.msg_count():,}")

        # Mantém a referência ao bus na aba de descoberta sempre atualizada
        # (o bus pode ter sido reaberto/reconfigurado, ex.: por um replay).
        self._discovery_tab.set_bus(self._bus)

    @pyqtSlot()
    def _load_csv_replay(self):
        """Carrega um CSV (IXXAT miniMon) e abre o painel flutuante de replay.

        Passos:
          1. Se houver conexão ativa, pergunta se pode desconectar.
          2. Pede o arquivo CSV ao usuário.
          3. Inicia o replay PAUSADO (o usuário aperta PLAY quando quiser).
          4. Estima a duração total do log e atualiza o rodapé.
          5. Cria e posiciona o ReplayControlDialog no canto inferior-direito.
        """
        if self._bus.is_connected:
            resp = QMessageBox.question(
                self, "Desconectar primeiro?",
                "Você está conectado. Deseja desconectar e iniciar o replay do CSV?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if resp != QMessageBox.Yes:
                return
            self._bus.disconnect()

        # Seleção do arquivo CSV; se o usuário cancelar, path vem vazio.
        path, _ = QFileDialog.getOpenFileName(
            self, "Carregar Log CSV (IXXAT miniMon)", "",
            "CSV IXXAT (*.csv);;Todos (*.*)"
        )
        if not path:
            return

        # Inicia o replay PAUSADO (usuário aperta Play quando quiser)
        ok, msg = self._bus.replay_csv(path, speed_factor=1.0, start_paused=True)
        if not ok:
            QMessageBox.critical(self, "Falha no replay", msg)
            return

        # Calcula duração total a partir do progresso (parse já contou as msgs)
        _, total = self._bus.replay_progress
        # Estimativa rápida: ler primeira e última linha do CSV
        total_sec = self._estimate_log_duration(path)

        # Atualiza status bar para indicar o modo replay (em destaque/accent).
        self._lbl_stat_conn.setText(f"Replay CSV pausado — {os.path.basename(path)}")
        self._lbl_stat_conn.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")

        # Abre painel de controle FLUTUANTE — você pode trabalhar no programa
        self._replay_dialog = ReplayControlDialog(
            self._bus, os.path.basename(path), total_sec, parent=self
        )
        self._replay_dialog.show()
        # Posiciona no canto inferior-direito da janela principal, com pequenas
        # margens (30 px na horizontal, 60 px na vertical) para não colar à borda.
        geo = self.geometry()
        self._replay_dialog.move(
            geo.right() - self._replay_dialog.width() - 30,
            geo.bottom() - self._replay_dialog.height() - 60
        )

    def _estimate_log_duration(self, path: str) -> float:
        """Lê primeira/última linha do CSV para estimar duração total (em segundos).

        Formato esperado da IXXAT miniMon: campos separados por ';', sendo o
        terceiro campo (índice 2) um timestamp "HH:MM:SS.fff". A duração é a
        diferença entre o timestamp da última e da primeira linha de dados.
        Qualquer erro de parsing retorna 0.0 (estimativa indisponível).
        """
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            if len(lines) < 3:
                return 0.0   # arquivo curto demais (sem cabeçalho + 2 dados)
            def _parse_ts(line: str) -> float:
                # Quebra a linha em campos, remove aspas/espaços e converte o
                # timestamp HH:MM:SS.fff para total de segundos.
                parts = [p.strip().strip('"') for p in line.split(';')]
                if len(parts) < 3:
                    return 0.0
                h, m, rest = parts[2].split(':')
                return int(h)*3600 + int(m)*60 + float(rest)
            first = _parse_ts(lines[1])    # lines[0] é o cabeçalho; lines[1] = 1ª linha de dados
            last  = _parse_ts(lines[-1])   # última linha de dados
            return max(0.0, last - first)  # nunca negativo
        except Exception:
            return 0.0

    @pyqtSlot()
    def _start_log(self):
        """Pede um caminho de arquivo e inicia a gravação do log do monitor.

        O nome sugerido já inclui data/hora atuais. Se o usuário cancelar
        (path vazio), nada acontece.
        """
        path, _ = QFileDialog.getSaveFileName(
            self, "Salvar Log", f"log_{datetime.now().strftime('%d-%m-%Y_%H-%M-%S')}.txt",
            "Text (*.txt)"
        )
        if path:
            self._monitor_tab.start_logging(path)

    def closeEvent(self, event):
        """Encerramento da janela: desconecta o bus e para a gravação de log."""
        self._bus.disconnect()
        self._monitor_tab.stop_logging()
        event.accept()
