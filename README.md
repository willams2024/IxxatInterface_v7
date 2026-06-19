# IxxatInterface v7 — Mapeador de Sinais CAN / J1939

Ferramenta para monitoramento, descoberta e mapeamento de sinais em barramentos
CAN veiculares (caminhões, ônibus e veículos leves), via adaptador **IXXAT
USB-to-CAN V2**. Pensada para a equipe de engenharia identificar em qual PGN/byte
cada informação do veículo trafega e gerar a configuração pronta para o
equipamento de telemetria (formato VIRLOC).

---

## ✨ Funcionalidades

- **Monitor CAN em tempo real** com decodificação J1939 (banco FMS-Standard v05)
  - Gráficos por byte em tempo real (layout 4×2)
  - Navegação rápida entre PGNs (◀ ▶)
  - Filtro por hex, decimal, PGN ou sigla
- **Descoberta guiada de 22 sinais** (RPM, velocidade, embreagem, freio, câmbio,
  hodômetro, temperaturas, pressões, portas, ignição, etc.)
  - Algoritmo estatístico com correlação de padrão (Pearson)
  - Detecção automática de sinais de 1 ou 2 bytes (little/big-endian)
  - Suporte a J1939 (29 bits) **e** protocolos proprietários (11 bits)
- **Calibração de 2 pontos** para sinais proprietários (fator/offset exatos)
- **Exportação Excel** no formato VIRLOC (strings `VS` e `MAT` com operações inteiras)
- **Relatório PDF** da sessão com avaliação de produtividade
- **Replay de logs CSV** da IXXAT miniMon (com Play/Pause/Reiniciar/velocidade)
- **Modo Listen-Only** (100% passivo — seguro para veículos em operação)
- **Modo Simulação** para testes sem hardware

---

## 📦 Requisitos

- Windows 10/11 (64 bits)
- Python 3.12+ (apenas para rodar a partir do código)
- Adaptador IXXAT USB-to-CAN V2 compact + driver **IXXAT VCI V4**
  (apenas para uso com hardware real)

Instale as dependências:

```bash
pip install -r requirements.txt
```

---

## ▶️ Como rodar (a partir do código)

```bash
python main.py
```

## 🏗️ Como gerar o executável (.exe)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "IxxatInterface-v7" ^
  --hidden-import can.interfaces.ixxat --hidden-import PyQt5.sip ^
  --hidden-import PyQt5.QtPrintSupport --hidden-import openpyxl ^
  --collect-submodules openpyxl --icon assets/icon.ico ^
  --add-data "assets;assets" main.py
```

O executável é gerado em `dist/IxxatInterface-v7.exe`.

---

## 📁 Estrutura

```
IxxatInterface_v7/
├─ main.py              Ponto de entrada
├─ core/                Lógica (sem interface)
│  ├─ can_bus.py        Conexão CAN + simulação + replay CSV
│  ├─ discovery.py      Motor de descoberta de sinais
│  └─ j1939.py          Banco de dados J1939 (FMS v05) + decodificador
├─ gui/                 Interface PyQt5
│  ├─ app.py            Janela principal, abas e menus
│  ├─ monitor.py        Aba Monitor CAN + gráficos
│  ├─ discovery.py      Aba Descoberta de Sinais
│  ├─ signals_tab.py    Aba Sinais Mapeados + Excel/PDF/calibração
│  └─ styles.py         Tema escuro
└─ assets/              Ícone e recursos
```

---

## 🔒 Segurança

O programa opera por padrão em **modo Listen-Only**: o controlador IXXAT é
configurado como 100% passivo (não transmite nem envia ACK frames). É seguro
conectar em veículos em operação. O modo pode ser desativado para uso em bancada
com poucos nós (onde o ACK é necessário para a ECU não entrar em bus-off).

---

## 📄 Licença

Uso interno — Engenharia.
