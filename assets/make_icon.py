"""Gera o ícone do programa em múltiplas resoluções (.ico)."""
import os
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ICO_PATH = os.path.join(OUT_DIR, "icon.ico")
PNG_PATH = os.path.join(OUT_DIR, "icon.png")


def make_icon(size: int) -> Image.Image:
    """Cria um único PNG quadrado do tamanho indicado."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ── Fundo arredondado (gradient roxo escuro) ──────────────────────────
    radius = int(size * 0.18)
    # cor base do fundo (igual ao bg_card do tema dark)
    bg = (24, 24, 44, 255)
    accent = (108, 99, 255, 255)  # #6c63ff

    # Retângulo arredondado
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=bg,
                        outline=accent, width=max(1, size // 32))

    # ── Forma de "CAN bus": duas linhas paralelas com pulsos (CAN_H/CAN_L) ─
    # Linhas horizontais com pequenas ondulações
    h_top    = int(size * 0.32)
    h_bot    = int(size * 0.48)
    margin_x = int(size * 0.18)

    # CAN_H (linha de cima)
    d.line([(margin_x, h_top), (size - margin_x, h_top)],
           fill=(76, 175, 80, 255), width=max(2, size // 32))   # verde
    # CAN_L (linha de baixo)
    d.line([(margin_x, h_bot), (size - margin_x, h_bot)],
           fill=(244, 67, 54, 255), width=max(2, size // 32))   # vermelho

    # Pulsos digitais (quadrados pequenos sobre as linhas — simboliza dados)
    pulse_w = max(2, size // 16)
    for x in range(margin_x + pulse_w, size - margin_x - pulse_w, pulse_w * 2):
        # Pulso CAN_H (sobe pra cima)
        d.rectangle((x, h_top - pulse_w * 2, x + pulse_w, h_top),
                    fill=(76, 175, 80, 255))
        # Pulso CAN_L (desce pra baixo)
        d.rectangle((x, h_bot, x + pulse_w, h_bot + pulse_w * 2),
                    fill=(244, 67, 54, 255))

    # ── Texto "CAN" abaixo ───────────────────────────────────────────────
    if size >= 32:
        try:
            font_size = int(size * 0.32)
            # Tenta fonte do sistema; fallback para default
            font = None
            for fname in ("arialbd.ttf", "Arial Bold.ttf", "arial.ttf",
                          "Arial.ttf", "DejaVuSans-Bold.ttf"):
                try:
                    font = ImageFont.truetype(fname, font_size)
                    break
                except (OSError, IOError):
                    continue
            if font is None:
                font = ImageFont.load_default()

            text = "CAN"
            # Centraliza no eixo X, posiciona abaixo das linhas
            bbox = d.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tx = (size - tw) // 2 - bbox[0]
            ty = int(size * 0.62) - bbox[1]
            # Sombra para legibilidade
            d.text((tx + 1, ty + 1), text, font=font, fill=(0, 0, 0, 200))
            d.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
        except Exception as e:
            print(f"[warn] sem fonte (size={size}): {e}")

    return img


# Gera o ícone a partir de uma imagem 256×256 — PIL escala automaticamente
master = make_icon(256)
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
master.save(ICO_PATH, format="ICO", sizes=sizes)
master.save(PNG_PATH, format="PNG")

print(f"OK! Ícone gerado:")
print(f"  {ICO_PATH}  ({os.path.getsize(ICO_PATH)/1024:.1f} KB)")
print(f"  {PNG_PATH}  ({os.path.getsize(PNG_PATH)/1024:.1f} KB)")
print(f"  Resoluções: {[s[0] for s in sizes]}")
