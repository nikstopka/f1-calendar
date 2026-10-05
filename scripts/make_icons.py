"""Генерация растровых иконок сайта из той же геометрии, что и favicon.svg.

Запуск:  python scripts/make_icons.py
Создаёт: favicon.ico (16/32/48), apple-touch-icon.png (180),
         icon-192.png, icon-512.png
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

SS = 2048          # supersampling для гладких краёв
K = SS / 512.0     # 512 — система координат, как в SVG
RADIUS = 104       # радиус скруглённых углов в тех же координатах

# Три «скоростных» полосы ( skewed parallelograms) + нижняя акцентная полоса
STREAKS = [
    (125, 174, 421, 174, 387, 214, 91, 214),
    (151, 236, 401, 236, 367, 276, 117, 276),
    (177, 298, 381, 298, 347, 338, 143, 338),
]
ACCENT = (0, 448, 512, 512)


def _lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def build_master():
    """Рисует иконку в разрешении SS×SS с прозрачными скруглёнными углами."""
    # Фон: вертикальный градиент #1a1a26 -> #0a0a0f
    bg = Image.new("RGB", (SS, SS))
    d = ImageDraw.Draw(bg)
    top, bottom = (26, 26, 38), (10, 10, 15)
    for y in range(SS):
        d.line([(0, y), (SS, y)], fill=_lerp(top, bottom, y / SS))

    # Красный градиент #ff3b30 -> #b00500
    red = Image.new("RGB", (SS, SS))
    rd = ImageDraw.Draw(red)
    rtop, rbottom = (255, 59, 48), (176, 5, 0)
    for y in range(SS):
        rd.line([(0, y), (SS, y)], fill=_lerp(rtop, rbottom, y / SS))

    # Маска красных элементов + лёгкое свечение
    mask = Image.new("L", (SS, SS), 0)
    md = ImageDraw.Draw(mask)
    for poly in STREAKS:
        md.polygon([(x * K, y * K) for x, y in zip(poly[::2], poly[1::2])], fill=255)
    md.rectangle([ACCENT[0] * K, ACCENT[1] * K, ACCENT[2] * K, ACCENT[3] * K], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(SS * 0.018))

    img = bg.convert("RGBA")
    img.paste(red.convert("RGBA"), (0, 0), mask)

    # Прозрачные скруглённые углы
    round_mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(round_mask).rounded_rectangle(
        [0, 0, SS - 1, SS - 1], radius=RADIUS * K, fill=255
    )
    alpha = img.getchannel("A")
    img.putalpha(Image.composite(alpha, Image.new("L", (SS, SS), 0), round_mask))
    return img


def main():
    master = build_master()
    out = PROJECT_DIR

    for size, name in [(512, "icon-512.png"), (192, "icon-192.png"), (180, "apple-touch-icon.png")]:
        master.resize((size, size), Image.LANCZOS).save(out / name, "PNG")
        print(f"  {name} ({size}x{size})")

    ico_base = master.resize((256, 256), Image.LANCZOS)
    ico_base.save(out / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print("  favicon.ico (16/32/48)")

    print("\nГотово.")


if __name__ == "__main__":
    main()