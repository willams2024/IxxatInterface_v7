"""
Interface de barramento CAN: faz wrapper do backend IXXAT do python-can,
além de oferecer modo de simulação e replay de logs CSV.

Este módulo é o ponto central de conexão com o hardware CAN do programa de
mapeamento de sinais CAN/J1939. Ele abstrai três fontes de mensagens, todas
entregues do mesmo jeito aos "listeners" registrados:

  1) Hardware IXXAT real (via python-can), com suporte a modo listen-only;
  2) Simulação interna (gera dados J1939 realistas sem hardware);
  3) Replay de um log CSV exportado pela IXXAT miniMon.

Em todos os casos, as mensagens são despachadas como objetos CANMessage para
os callbacks registrados em add_listener(), de modo que o resto do programa
(a GUI, os decodificadores) não precisa saber de onde a mensagem veio.
"""

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

# Tentamos importar o python-can. Se a biblioteca não estiver instalada
# (CAN_AVAILABLE = False), o programa ainda funciona, porém cai forçadamente
# no modo simulação — ver lógica em connect(). Isso permite rodar a GUI numa
# máquina de desenvolvimento sem o driver/hardware IXXAT instalado.
try:
    import can
    CAN_AVAILABLE = True
except ImportError:
    CAN_AVAILABLE = False


@dataclass
class CANMessage:
    """
    Representa uma única mensagem CAN normalizada, independente da fonte
    (hardware real, simulação ou replay de CSV).

    É a "moeda comum" que circula por todo o programa: os listeners sempre
    recebem instâncias desta classe, não os objetos crus do python-can.
    """
    timestamp: float        # tempo (em segundos) relativo ao início da captura/replay
    can_id: int             # identificador CAN (11 bits padrão ou 29 bits estendido)
    data: bytes             # payload bruto da mensagem (0 a 8 bytes em CAN clássico)
    is_extended: bool       # True = ID estendido de 29 bits (típico de J1939)
    dlc: int                # Data Length Code: número de bytes de dados declarado
    channel: int = 0        # canal físico de origem (suporte a placas multicanal)


class CANBus:
    """
    Faz o wrapper do python-can e fornece modo de simulação e replay de CSV.

    Esta classe é o coração do módulo. Ela gerencia:
      - a conexão com o hardware IXXAT (abrir/fechar o bus);
      - uma thread de fundo que lê mensagens continuamente (de hardware,
        simulação ou CSV) e as despacha para os listeners;
      - o estado de replay (pausa, velocidade, progresso).

    Padrão de uso típico:
        bus = CANBus()
        bus.add_listener(meu_callback)
        bus.connect(channel=0, bitrate=250000, listen_only=True)
        ...
        bus.disconnect()

    Observação sobre threads: a leitura roda sempre numa thread daemon
    separada (self._thread). Os métodos públicos de controle (pause/resume,
    set_speed, disconnect) são chamados a partir da thread da GUI. A
    comunicação entre as duas threads é feita por flags booleanas/inteiras
    simples, cuja leitura/escrita é atômica sob o GIL do CPython — por isso
    não há locks explícitos (ver comentários nos atributos abaixo).
    """

    def __init__(self):
        # Objeto can.Bus do python-can quando conectado a hardware real;
        # permanece None em modo simulação/replay.
        self._bus = None
        # Thread de fundo que executa o loop de leitura/simulação/replay.
        self._thread: Optional[threading.Thread] = None
        # Flag-mestra do loop. Colocar em False sinaliza para a thread parar.
        # Lida/escrita por duas threads, mas é um bool simples (atômico sob o GIL).
        self._running = False
        # True quando NÃO estamos lendo hardware real (simulação ou replay).
        self._simulation = False
        # Lista de callbacks que recebem cada CANMessage despachada.
        self._listeners: list[Callable[[CANMessage], None]] = []
        # Contador total de mensagens já despachadas (estatística para a GUI).
        self._msg_count = 0
        # Instante (time.time()) em que a captura começou; usado para
        # converter timestamps absolutos em tempo relativo ao início.
        self._start_time = 0.0
        # Gerador de dados simulados J1939 (ver classe _SimState no fim do arquivo).
        self._sim_state = _SimState()
        # Estado do replay — seguro entre threads via leituras simples de
        # atributos (o GIL do Python garante leituras atômicas de int/bool).
        self._replay_done    = 0      # quantas mensagens do CSV já foram reproduzidas
        self._replay_total   = 0      # total de mensagens carregadas do CSV
        self._replay_paused  = True   # inicia pausado para o usuário ter tempo de configurar
        self._replay_speed   = 1.0    # pode ser alterado durante o replay
        self._replay_messages: list   = []   # mantido em memória para reinício rápido

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, channel: int = 0, bitrate: int = 250000,
                simulation: bool = False,
                listen_only: bool = True) -> tuple[bool, str]:
        """
        Abre o barramento CAN (ou inicia a simulação) e começa a leitura
        numa thread de fundo.

        Parâmetros:
            channel     → índice do canal físico da placa IXXAT (0 = primeiro).
            bitrate     → taxa do barramento em bits/s (250000 é o padrão J1939).
            simulation  → se True, ignora o hardware e gera dados sintéticos.
            listen_only → ver explicação abaixo.

        Retorna uma tupla (sucesso, mensagem) para a GUI exibir ao usuário.

        Sobre o modo listen-only (passivo) — detalhe técnico importante:

        listen_only=True  → controlador 100% passivo (não envia ACK).
                            Seguro para veículos reais.
                            ATENÇÃO: em bancada com poucos nós, pode causar
                            bus-off da ECU e parar a transmissão.
                            (Sem o ACK do receptor, o transmissor pode entrar
                            em estado de erro se não houver outro nó ackando.)
        listen_only=False → modo normal (envia ACK).
                            Necessário para bancada com 1-2 nós.
        """
        # Se já houver uma conexão/replay rodando, encerramos antes de abrir
        # uma nova — evita threads órfãs e bus aberto duas vezes.
        if self._running:
            self.disconnect()

        # Caímos em simulação se o usuário pediu explicitamente OU se o
        # python-can não está disponível na máquina (sem driver/hardware).
        self._simulation = simulation or not CAN_AVAILABLE

        # Caminho 1: modo simulação — nenhum hardware é tocado. Dispara a
        # thread _sim_loop que gera mensagens J1939 sintéticas.
        if self._simulation:
            self._running = True
            self._start_time = time.time()
            self._thread = threading.Thread(target=self._sim_loop, daemon=True)
            self._thread.start()
            return True, "Modo simulação ativo"

        # Caminho 2: hardware IXXAT real.
        try:
            # Aplica ou remove o patch de listen-only ANTES de abrir o bus.
            # A ordem importa: o opmode é lido no momento da criação do
            # can.Bus, então patchear depois não teria efeito.
            self._set_listen_only_mode(listen_only)

            self._bus = can.Bus(interface='ixxat', channel=channel, bitrate=bitrate)
            self._running = True
            self._start_time = time.time()
            # Thread daemon: morre junto com o processo, não impede o shutdown.
            self._thread = threading.Thread(target=self._read_loop, daemon=True)
            self._thread.start()
            mode_str = "LISTEN-ONLY" if listen_only else "MODO NORMAL (envia ACK)"
            return True, f"Conectado ao canal {channel} @ {bitrate} bps [{mode_str}]"
        except Exception as e:
            # Qualquer falha de abertura (canal ocupado, driver ausente,
            # bitrate inválido) volta como mensagem de erro para a GUI.
            return False, str(e)

    @staticmethod
    def _set_listen_only_mode(enabled: bool):
        """
        Configura o backend IXXAT para listen-only (passivo) ou normal (com ACK).

        Quando enabled=True, força CAN_OPMODE_LISTONLY no opmode interno —
        o controlador não transmite NADA, nem ACKs. Seguro para veículos.

        Quando enabled=False, restaura o opmode original (com ACK) — necessário
        para setups em bancada onde há poucos nós no barramento.

        TRUQUE TÉCNICO: o python-can não expõe uma opção direta de listen-only
        para o backend IXXAT. Então fazemos "monkey-patch" da constante
        CAN_OPMODE_STANDARD do módulo de constantes do driver, que é o valor
        usado internamente ao abrir o controlador. Alterar essa constante ANTES
        de instanciar o can.Bus faz o controlador abrir já em modo passivo.
        """
        try:
            from can.interfaces.ixxat import constants as ixxat_const

            # Guarda o valor original numa primeira chamada. Sem este backup,
            # patches repetidos acumulariam o bit LISTONLY e perderíamos a
            # referência do valor "normal" para restaurar depois.
            if not hasattr(ixxat_const, "_ORIGINAL_STANDARD"):
                ixxat_const._ORIGINAL_STANDARD = ixxat_const.CAN_OPMODE_STANDARD

            if enabled:
                # Adiciona o bit LISTONLY ao opmode padrão (OR bit-a-bit),
                # sempre partindo do valor ORIGINAL para ser idempotente.
                ixxat_const.CAN_OPMODE_STANDARD = (
                    ixxat_const._ORIGINAL_STANDARD | ixxat_const.CAN_OPMODE_LISTONLY
                )
            else:
                # Restaura ao valor original (modo normal, com ACK)
                ixxat_const.CAN_OPMODE_STANDARD = ixxat_const._ORIGINAL_STANDARD
        except ImportError:
            # python-can/backend IXXAT ausente (ex.: máquina de dev). Ignora
            # silenciosamente — só faz sentido em modo simulação mesmo.
            pass

    def disconnect(self):
        """
        Encerra a conexão/simulação/replay de forma ordenada.

        Sequência (a ordem importa):
          1) baixa a flag _running → sinaliza para a thread de leitura sair
             do seu loop na próxima iteração;
          2) join() com timeout → espera a thread terminar de fato, mas sem
             travar a GUI indefinidamente se algo der errado;
          3) fecha o bus de hardware (shutdown) e zera a referência.
        """
        # 1) Sinaliza parada. A thread checa esta flag em cada iteração.
        self._running = False
        # 2) Aguarda a thread realmente encerrar. O timeout evita deadlock
        #    caso a thread esteja presa; como é daemon, não impede o processo.
        if self._thread:
            self._thread.join(timeout=2.0)
        # 3) Libera o recurso de hardware. O try/except cobre o caso de o
        #    bus já estar fechado ou o shutdown falhar — não há o que fazer.
        if self._bus:
            try:
                self._bus.shutdown()
            except Exception:
                pass
            self._bus = None

    # ── CSV Replay ────────────────────────────────────────────────────────────

    # ── Replay controls (chamáveis do thread GUI) ────────────────────────────
    #
    # Todos estes métodos rodam na thread da GUI e apenas escrevem flags
    # simples que a thread de replay (_replay_loop) lê a cada iteração. Como
    # são escritas atômicas de bool/float sob o GIL, não precisamos de lock.

    def pause_replay(self):
        """Pausa o replay. A thread de replay para no próximo passo."""
        self._replay_paused = True

    def resume_replay(self):
        """Retoma o replay a partir de onde estava pausado."""
        self._replay_paused = False

    def toggle_replay_pause(self) -> bool:
        """Alterna pausa/play. Retorna o novo estado (True = pausado)."""
        self._replay_paused = not self._replay_paused
        return self._replay_paused

    def set_replay_speed(self, factor: float):
        """
        Ajusta a velocidade de reprodução em tempo real.

        O fator é limitado (clamp) ao intervalo [0.05x, 100x]:
          - mínimo evita divisão por valor ínfimo/quase-zero no cálculo de timing;
          - máximo evita avançar o log rápido demais a ponto de virar rajada.
        """
        self._replay_speed = max(0.05, min(factor, 100.0))

    def restart_replay(self, start_paused: bool = True) -> bool:
        """
        Reinicia o replay do começo, sem precisar recarregar o CSV.
        Retorna False se nenhum CSV foi carregado ainda.

        As mensagens ficam guardadas em self._replay_messages, então o
        reinício é instantâneo: não relê nem reparseia o arquivo de disco.
        """
        # Sem CSV carregado previamente não há o que reiniciar.
        if not self._replay_messages:
            return False
        # Para a thread anterior (se ainda estiver rodando). Guardamos o
        # estado anterior por simetria, embora aqui só forcemos a parada.
        was_running = self._running
        self._running = False
        if self._thread and self._thread.is_alive():
            # Não dá para join() em si mesma; usuário chama daqui da thread GUI.
            # O try/except cobre o caso raro de a thread atual ser a própria
            # thread de replay, o que dispararia RuntimeError no join().
            try:
                self._thread.join(timeout=2.0)
            except RuntimeError:
                pass
        # Reseta todo o estado de progresso/tempo para começar do zero.
        self._running       = True
        self._simulation    = True
        self._start_time    = time.time()
        self._msg_count     = 0
        self._replay_done   = 0
        self._replay_paused = start_paused
        # Nova thread reutilizando a MESMA lista de mensagens já em memória.
        self._thread = threading.Thread(
            target=self._replay_loop,
            args=(self._replay_messages,),
            daemon=True,
        )
        self._thread.start()
        return True

    @property
    def replay_paused(self) -> bool:
        """Estado atual de pausa (True = pausado). Leitura segura entre threads."""
        return self._replay_paused

    @property
    def replay_speed(self) -> float:
        """Fator de velocidade de replay atual (1.0 = tempo real)."""
        return self._replay_speed

    # ─────────────────────────────────────────────────────────────────────────

    def replay_csv(self, path: str, speed_factor: float = 1.0,
                   start_paused: bool = True
                   ) -> tuple[bool, str]:
        """
        Carrega um log CSV da IXXAT miniMon e reproduz as mensagens para os
        listeners, como se fossem provenientes do hardware.

        O progresso pode ser consultado via `self.replay_progress` (thread-safe).

        Formato esperado:
        "Bus";"No";"Time (abs)";"State";"ID (hex)";"DLC";"Data (hex)";"ASCII"
        """
        # Não permite iniciar replay por cima de uma conexão já ativa —
        # evita threads concorrentes despachando para os mesmos listeners.
        if self._running:
            return False, "Já existe uma conexão/replay ativo. Desconecte primeiro."

        # 1) Parse do CSV — feito de forma totalmente tolerante a falhas:
        #    linhas malformadas são simplesmente puladas (continue), nunca
        #    abortam o carregamento. Um log real pode ter linhas de status,
        #    campos vazios ou caracteres inválidos no meio.
        messages: list[tuple] = []
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.readline()  # descarta a linha de cabeçalho
                for line in f:
                    # Campos separados por ';' e ainda envoltos em aspas no
                    # formato da miniMon — removemos espaços e aspas de cada um.
                    parts = [p.strip().strip('"') for p in line.split(';')]
                    if len(parts) < 7:
                        continue  # linha incompleta, ignora
                    try:
                        # Índices conforme o cabeçalho documentado abaixo:
                        # [2]=Time(abs), [4]=ID(hex), [5]=DLC, [6]=Data(hex).
                        time_str = parts[2]
                        id_str   = parts[4].strip()
                        dlc_str  = parts[5].strip()
                        data_str = parts[6].strip()
                        if not id_str or not dlc_str:
                            continue  # ID ou DLC ausente → linha inútil
                        can_id  = int(id_str, 16)
                        dlc     = int(dlc_str)
                        # Heurística: IDs acima de 0x7FF (11 bits) só cabem em
                        # frame estendido de 29 bits — caso típico do J1939.
                        is_ext  = can_id > 0x7FF
                        # Bytes de dados vêm separados por espaço em hexadecimal.
                        data    = bytes(int(b, 16) for b in data_str.split() if b)
                        # Timestamp no formato "HH:MM:SS.sss" → segundos absolutos.
                        h, m, rest = time_str.split(':')
                        s   = float(rest)
                        abs_ts = int(h) * 3600 + int(m) * 60 + s
                        messages.append((abs_ts, can_id, data, is_ext, dlc))
                    except (ValueError, IndexError):
                        # Qualquer conversão hex/float que falhe → pula a linha.
                        continue
        except Exception as e:
            return False, f"Erro ao ler CSV: {e}"

        if not messages:
            return False, "CSV vazio ou inválido (nenhuma mensagem reconhecida)"

        # Duração total do log = timestamp da última menos o da primeira mensagem.
        total_sec = messages[-1][0] - messages[0][0]

        # 2) Inicializa o estado e dispara a thread de replay.
        self._replay_messages = messages   # guardado para permitir reinicio rapido
        self._running         = True
        self._simulation      = True   # replay conta como "não-hardware"
        self._start_time      = time.time()
        self._msg_count       = 0
        self._replay_done     = 0
        self._replay_total    = len(messages)
        self._replay_paused   = start_paused
        self._replay_speed    = speed_factor
        self._thread = threading.Thread(
            target=self._replay_loop,
            args=(messages,),
            daemon=True,
        )
        self._thread.start()
        state = " (PAUSADO — clique Play para iniciar)" if start_paused else ""
        return True, (f"{len(messages):,} mensagens carregadas "
                      f"({total_sec:.1f}s){state}")

    @property
    def replay_progress(self) -> tuple[int, int]:
        """Retorna (mensagens_processadas, total) — thread-safe via GIL."""
        return (self._replay_done, self._replay_total)

    def _replay_loop(self, messages: list):
        """
        Loop principal de replay. Suporta pause/resume e mudança de velocidade
        em tempo de execução. O timing é compensado de modo que pausas longas
        não causem rajadas após o resume.

        Estratégia de timing (a parte sutil):
          - Cada mensagem tem um timestamp 'ts'. Calculamos quando ela DEVERIA
            tocar relativo ao início (ts - t0), ajustado pela velocidade.
          - Comparamos com o tempo de parede já decorrido, descontando o tempo
            que ficamos pausados (pause_offset). Sem esse desconto, ao retomar
            de uma pausa o 'elapsed' estaria muito à frente do 'target' e o
            loop despejaria todas as mensagens atrasadas de uma vez (rajada).
          - O sleep é limitado a 0.5s por vez para que o loop volte a checar
            as flags _running/_replay_paused com frequência e responda rápido
            a comandos da GUI mesmo em gaps longos entre mensagens.
        """
        t0           = messages[0][0]      # timestamp absoluto da 1ª mensagem (origem do tempo)
        play_start   = time.time()         # instante de parede em que o replay começou
        pause_offset = 0.0                 # tempo total acumulado em pausa (a descontar)
        pause_start  = None                # marca o início da pausa atual (None = não pausado)
        N            = len(messages)

        for i, (ts, can_id, data, is_ext, dlc) in enumerate(messages):
            # Saída imediata se a thread foi sinalizada para parar.
            if not self._running:
                break

            # 1) Enquanto pausado, dorme em fatias curtas e fica observando as
            #    flags. Registramos quando a pausa começou para depois descontar
            #    sua duração do tempo decorrido.
            while self._replay_paused and self._running:
                if pause_start is None:
                    pause_start = time.time()
                time.sleep(0.05)
            # Saiu da pausa: acumula quanto tempo ficou parado e zera o marcador.
            if pause_start is not None:
                pause_offset += time.time() - pause_start
                pause_start = None

            # 2) Calcula o instante-alvo desta mensagem considerando a
            #    velocidade ATUAL (lida a cada iteração → muda em tempo real).
            spd     = max(self._replay_speed, 0.05)
            target  = (ts - t0) / spd
            elapsed = (time.time() - play_start) - pause_offset
            # Só dorme se ainda estamos adiantados em relação ao alvo. O cap de
            # 0.5s mantém a responsividade a pausa/parada (ver docstring).
            if target > elapsed + 0.001:
                time.sleep(min(target - elapsed, 0.5))  # cap em 500ms p/ responder a pausa

            # 3) Monta o CANMessage com timestamp relativo ao início e despacha
            #    para os listeners, exatamente como o hardware faria.
            msg = CANMessage(
                timestamp=ts - t0,
                can_id=can_id,
                data=data,
                is_extended=is_ext,
                dlc=dlc,
            )
            self._dispatch(msg)
            self._replay_done = i + 1   # atualiza progresso (lido pela GUI)

        # Fim do log (ou parada): marca como desconectado.
        self._running = False

    @property
    def is_connected(self) -> bool:
        """True enquanto houver leitura ativa (hardware, simulação ou replay)."""
        return self._running

    @property
    def is_simulation(self) -> bool:
        """True quando a fonte NÃO é hardware real (simulação ou replay de CSV)."""
        return self._simulation

    def add_listener(self, fn: Callable[[CANMessage], None]):
        """Registra um callback que receberá cada CANMessage despachada."""
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[CANMessage], None]):
        """Remove um callback previamente registrado (deve existir na lista)."""
        self._listeners.remove(fn)

    def msg_count(self) -> int:
        """Total de mensagens já despachadas desde a conexão (para estatística)."""
        return self._msg_count

    # ── Real hardware loop ────────────────────────────────────────────────────

    def _read_loop(self):
        """
        Loop principal de leitura do barramento CAN.

        IMPORTANTE: erros transientes (frame error, FIFO overflow, RX queue full,
        bus warning, etc.) NÃO devem matar o thread. Eles são normais em um
        barramento ativo e devem ser ignorados — só registramos contagem.
        """
        # Contador de erros consecutivos. Resetado a cada recepção bem-sucedida.
        consecutive_errors = 0
        MAX_CONSECUTIVE = 50          # se 50 erros seguidos → bus realmente quebrado

        while self._running and self._bus:
            try:
                # recv com timeout curto: retorna None se nada chegou em 100ms,
                # o que permite ao loop reavaliar a flag _running regularmente
                # (importante para o disconnect responder rápido).
                raw = self._bus.recv(timeout=0.1)
                if raw:
                    # Converte o objeto cru do python-can no nosso CANMessage,
                    # já com timestamp relativo ao início da captura.
                    msg = CANMessage(
                        timestamp=raw.timestamp - self._start_time,
                        can_id=raw.arbitration_id,
                        data=bytes(raw.data),
                        is_extended=raw.is_extended_id,
                        dlc=raw.dlc,
                    )
                    self._dispatch(msg)
                    consecutive_errors = 0   # reset contador em recv válido
            except Exception:
                # NÃO faz break — só conta e aguarda um pouco para não
                # spinar o CPU em caso de erro persistente.
                # Erros transientes (frame error, FIFO overflow, RX queue full,
                # bus warning) são esperados num barramento ativo; matar a
                # thread por causa deles deixaria a captura morta sem aviso.
                consecutive_errors += 1
                if consecutive_errors > MAX_CONSECUTIVE:
                    # Bus aparentemente morto — tenta um "chega pra lá" no
                    # buffer de TX para destravar o controlador e segue tentando.
                    try:
                        self._bus.flush_tx_buffer()
                    except Exception:
                        pass
                    consecutive_errors = 0   # zera para continuar tentando
                time.sleep(0.05)
                continue

    # ── Simulation loop ───────────────────────────────────────────────────────

    def _sim_loop(self):
        """
        Loop da simulação. A cada tick de 10ms pergunta ao _SimState quais
        mensagens J1939 devem ser geradas naquele instante (respeitando as
        taxas de cada PGN) e despacha cada uma.
        """
        while self._running:
            # Tempo decorrido desde o início da simulação (base de tempo do _SimState).
            now = time.time() - self._start_time
            msgs = self._sim_state.tick(now)
            for msg in msgs:
                self._dispatch(msg)
            time.sleep(0.010)  # 10ms tick → resolução suficiente p/ PGNs de 10ms

    def _dispatch(self, msg: CANMessage):
        """
        Entrega uma mensagem a todos os listeners registrados.

        Ponto único de saída para qualquer fonte (hardware/sim/replay). Cada
        callback roda dentro de um try/except isolado: se um listener lançar
        exceção, ele é ignorado para não derrubar a thread de leitura nem
        impedir os demais listeners de receberem a mensagem.
        """
        self._msg_count += 1
        for fn in self._listeners:
            try:
                fn(msg)
            except Exception:
                pass


# ── Simulation state ──────────────────────────────────────────────────────────

class _SimState:
    """
    Gera dados de simulação J1939 realistas.

    Mantém o estado físico de um "veículo virtual" (RPM, velocidade, marcha,
    temperatura, etc.) e, a cada tick, produz as mensagens J1939 adequadas
    nas taxas corretas de cada PGN. Também suporta "injeções" de teste —
    perfis pré-programados (rampa de velocidade, pico de RPM, troca de marcha)
    que servem para validar os decodificadores sem precisar de um veículo.
    """

    def __init__(self):
        # Mapa PGN -> próximo instante (em s) em que aquela mensagem deve sair.
        # É assim que cada PGN respeita sua própria taxa de envio.
        self._next = {}   # pgn -> next_send_time
        # Estado físico do veículo virtual:
        self._rpm = 800.0            # rotação do motor (marcha lenta ~800 rpm)
        self._speed = 0.0            # velocidade em km/h
        self._throttle = 0.0         # acelerador em % (0-100)
        self._parking_brake = 1      # freio de estacionamento (1 = acionado)
        self._clutch = 0             # embreagem (0 = solta, 1 = pressionada)
        self._gear = 0        # marcha atual; codificação J1939 usa offset -125 (neutro)
        self._odometer = 12345.0     # hodômetro em km (valor inicial arbitrário)
        self._coolant_temp = 85.0    # temperatura do líquido de arrefecimento em °C

        # Flags de injeção de teste — quando ligadas, sobrescrevem o estado
        # físico com um padrão determinístico (ver _update_state). Úteis para
        # exercitar sinais específicos a partir da GUI.
        self.inject_rpm_peak = False
        self.inject_speed_ramp = False
        self.inject_clutch_toggle = False
        self.inject_brake_toggle = False
        self.inject_gear_change = False
        self.inject_throttle_ramp = False

        # Variáveis auxiliares de estado das injeções (algumas reservadas/legado).
        self._peak_phase = 0.0
        self._speed_target = 0.0
        self._toggle_state = False
        self._toggle_count = 0

    # ------------------------------------------------------------------ #

    def tick(self, now: float) -> list[CANMessage]:
        """
        Avança a simulação para o instante 'now' (segundos desde o início) e
        retorna a lista de mensagens que devem ser enviadas neste tick.

        Cada PGN tem sua própria taxa (ex.: EEC1/RPM a cada 10ms, ET1/temperatura
        a cada 1s). Usamos self._next[pgn] como "agenda": só geramos a mensagem
        quando 'now' alcança o horário agendado, e então reagendamos para
        now + período. Isso reproduz fielmente as periodicidades reais do J1939.
        """
        msgs = []

        # Primeiro atualiza a física do veículo (RPM, velocidade, etc.).
        self._update_state(now)

        # EEC1 @ 10ms - RPM (sinal mais rápido, prioridade alta)
        if now >= self._next.get(61444, 0):
            self._next[61444] = now + 0.010
            msgs.append(self._make_eec1())

        # EEC2 @ 50ms - Throttle (posição do acelerador)
        if now >= self._next.get(61443, 0):
            self._next[61443] = now + 0.050
            msgs.append(self._make_eec2())

        # CCVS @ 100ms - Speed + brakes (velocidade do veículo + freios)
        if now >= self._next.get(65265, 0):
            self._next[65265] = now + 0.100
            msgs.append(self._make_ccvs())

        # VD @ 1000ms - Odometer (hodômetro)
        if now >= self._next.get(65248, 0):
            self._next[65248] = now + 1.0
            # Integra a distância: v[km/h] / 3600 = km percorridos em 1s.
            self._odometer += self._speed / 3600.0
            msgs.append(self._make_vd())

        # ET1 @ 1000ms - Temperatures (temperatura do motor)
        if now >= self._next.get(65262, 0):
            self._next[65262] = now + 1.0
            msgs.append(self._make_et1())

        # ETC2 @ 100ms - Gear (marcha da transmissão)
        if now >= self._next.get(61445, 0):
            self._next[61445] = now + 0.100
            msgs.append(self._make_etc2())

        # EFL_P1 @ 500ms - Oil pressure (pressão de óleo)
        if now >= self._next.get(65263, 0):
            self._next[65263] = now + 0.500
            msgs.append(self._make_efl())

        return msgs

    def _update_state(self, now: float):
        """
        Atualiza as grandezas físicas do veículo virtual para o instante 'now'.

        Por padrão simula uma marcha lenta com leve flutuação. As flags
        inject_* (acionadas pela GUI) sobrescrevem grandezas específicas com
        padrões periódicos determinísticos, úteis para testar cada sinal.
        """
        # Marcha lenta: ~800 rpm com oscilação suave (seno) + ruído gaussiano,
        # para parecer um motor real e não um valor estático.
        self._rpm = 800.0 + 20.0 * math.sin(now * 0.3) + random.gauss(0, 5)

        # Injeção: pico de RPM em ciclos de 4s (sobe, desce, repousa).
        if self.inject_rpm_peak:
            phase = (now % 4.0) / 4.0
            if phase < 0.3:
                # Rampa de subida 800 → 1500 rpm
                self._rpm = 800 + (1500 - 800) * (phase / 0.3)
            elif phase < 0.6:
                # Rampa de descida 1500 → 800 rpm
                self._rpm = 1500 - (1500 - 800) * ((phase - 0.3) / 0.3)
            else:
                # Repouso na marcha lenta
                self._rpm = 800 + random.gauss(0, 5)

        # Injeção: rampa triangular de velocidade em ciclos de 10s (0→60→0 km/h).
        if self.inject_speed_ramp:
            t = now % 10.0
            if t < 5:
                self._speed = t * 12.0  # 0 -> 60 km/h
            else:
                self._speed = (10.0 - t) * 12.0  # 60 -> 0

        # Injeção: rampa de acelerador em ciclos de 8s (0 → 100%).
        if self.inject_throttle_ramp:
            t = now % 8.0
            self._throttle = (t / 8.0) * 100.0

        # Injeção: alterna a embreagem (onda quadrada ~0.5 Hz).
        if self.inject_clutch_toggle:
            phase = int(now * 0.5) % 2
            self._clutch = phase

        # Injeção: alterna o freio de estacionamento (onda quadrada ~0.3 Hz).
        if self.inject_brake_toggle:
            phase = int(now * 0.3) % 2
            self._parking_brake = phase

        # Injeção: percorre uma sequência cíclica de marchas (1s por marcha).
        if self.inject_gear_change:
            gears = [0, 1, 2, 3, 4, 5, 4, 3, 2, 1]
            self._gear = gears[int(now % 10)]

    def _j1939_id(self, pgn: int, sa: int = 0x00) -> int:
        """
        Monta um identificador CAN estendido (29 bits) no formato J1939.

        Layout do ID de 29 bits:
            [prioridade:3][PF:8][PS:8][SA:8]
        onde:
            priority → prioridade da mensagem (6 é o valor comum/médio);
            PF (PDU Format) = byte alto do PGN;
            PS (PDU Specific) = byte baixo do PGN;
            SA (Source Address) = endereço do nó que envia.
        """
        priority = 6
        pf = (pgn >> 8) & 0xFF       # byte alto do PGN
        ps = pgn & 0xFF              # byte baixo do PGN
        return (priority << 26) | (pf << 16) | (ps << 8) | sa

    # NOTA sobre os métodos _make_*: cada um codifica grandezas físicas em
    # bytes seguindo a escala/offset definidos pelo padrão SAE J1939 para o
    # respectivo PGN. Bytes não usados ficam 0xFF (valor "não disponível" em
    # J1939). O timestamp passado é time.time() absoluto; ele é convertido em
    # tempo relativo mais adiante, no _read_loop/_replay_loop.

    def _make_eec1(self) -> CANMessage:
        """EEC1 (PGN 61444): rotação do motor, acelerador, carga e embreagem."""
        rpm_raw = max(0, int(self._rpm / 0.125))                 # 0.125 rpm/bit
        throttle_raw = int(max(0, min(250, self._throttle / 0.4)))  # 0.4 %/bit
        load_raw = int(self._rpm / 8031.875 * 100)               # carga aproximada em %
        # byte0: bits de status da embreagem (bits 3-4)
        byte0 = (self._clutch & 0x3) << 3
        data = bytes([byte0, throttle_raw, load_raw,
                      rpm_raw & 0xFF, (rpm_raw >> 8) & 0xFF,      # RPM little-endian (16 bits)
                      0xFF, 0xFF, 0xFF])
        return CANMessage(time.time(), self._j1939_id(61444), data, True, 8)

    def _make_eec2(self) -> CANMessage:
        """EEC2 (PGN 61443): posição do pedal do acelerador + flag de marcha lenta."""
        throttle_raw = int(max(0, min(250, self._throttle / 0.4)))
        byte0 = throttle_raw
        # bit 4: indica acelerador "em repouso" (idle) quando throttle < 1%.
        byte0 |= (1 if self._throttle < 1 else 0) << 4
        data = bytes([byte0, throttle_raw, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        return CANMessage(time.time(), self._j1939_id(61443), data, True, 8)

    def _make_ccvs(self) -> CANMessage:
        """CCVS (PGN 65265): velocidade do veículo + estado dos freios."""
        spd_raw = int(max(0, self._speed * 256))                 # 1/256 km/h por bit
        # byte0: freio de estacionamento no bit 2, freio de serviço no bit 4
        byte0 = ((self._parking_brake & 0x3) << 2)
        data = bytes([byte0, spd_raw & 0xFF, (spd_raw >> 8) & 0xFF,  # velocidade little-endian
                      0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        return CANMessage(time.time(), self._j1939_id(65265, 0xFC), data, True, 8)

    def _make_vd(self) -> CANMessage:
        """VD (PGN 65248): hodômetro total e distância de viagem (trip)."""
        odo_raw = int(self._odometer / 0.125)                    # 0.125 km/bit
        trip_raw = 0                                             # trip zerado nesta simulação
        # Ambos os campos são inteiros de 4 bytes little-endian.
        data = (odo_raw.to_bytes(4, 'little') + trip_raw.to_bytes(4, 'little'))
        return CANMessage(time.time(), self._j1939_id(65248, 0xFC), data, True, 8)

    def _make_et1(self) -> CANMessage:
        """ET1 (PGN 65262): temperatura do líquido de arrefecimento do motor."""
        ct_raw = int(self._coolant_temp + 40)                    # offset J1939 de -40 °C
        data = bytes([ct_raw, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        return CANMessage(time.time(), self._j1939_id(65262, 0xFC), data, True, 8)

    def _make_etc2(self) -> CANMessage:
        """ETC2 (PGN 61445): marcha selecionada e marcha atual da transmissão."""
        # Codificação J1939 da marcha usa offset +125 (neutro = 0 → 125).
        gear_sel = (self._gear + 125) & 0xFF
        gear_cur = (self._gear + 125) & 0xFF
        data = bytes([gear_sel, 0xFF, gear_cur, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
        return CANMessage(time.time(), self._j1939_id(61445, 0x03), data, True, 8)

    def _make_efl(self) -> CANMessage:
        """EFL/P1 (PGN 65263): pressão de óleo do motor (correlacionada ao RPM)."""
        oil_kpa = 350 + self._rpm / 8031.875 * 350               # pressão simulada em kPa
        oil_raw = int(oil_kpa / 4.0)                             # 4 kPa/bit
        data = bytes([0xFF, 0xFF, 0xFF, oil_raw & 0xFF, 0xFF, 0x64, 0xFF, 0xFF])
        return CANMessage(time.time(), self._j1939_id(65263, 0xFC), data, True, 8)
