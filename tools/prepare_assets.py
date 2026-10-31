"""构建期资源处理：裁掉图片白边/黑边，生成启动器图标、账户头像、窗口图标与背景。

运行：python tools/prepare_assets.py
产物：mcdecent/assets/{logo.png,account.png,app.png,app.ico,background.png}
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

DESKTOP = Path.home() / "Desktop"
OUT = Path(__file__).resolve().parent.parent / "mcdecent" / "assets"
BLACK = 28
WHITE = 232


def _uniform_line(pixels, coords) -> bool:
    """整行/整列是否都是接近纯黑或纯白（含透明）。"""
    total = len(coords)
    if total == 0:
        return False
    dark = light = 0
    for x, y in coords:
        r, g, b, a = pixels[x, y]
        if a < 8:
            light += 1  # 透明也视为背景
            continue
        if max(r, g, b) <= BLACK:
            dark += 1
        elif min(r, g, b) >= WHITE:
            light += 1
    return dark / total >= 0.98 or light / total >= 0.98


def strip_borders(image: Image.Image) -> Image.Image:
    """从四条边反复剥离纯黑/纯白边条（不做内容包围盒裁剪，避免误伤）。"""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    left, top, right, bottom = 0, 0, width - 1, height - 1
    changed = True
    while changed and right > left and bottom > top:
        changed = False
        if _uniform_line(pixels, [(x, top) for x in range(left, right + 1)]):
            top += 1
            changed = True
            continue
        if _uniform_line(pixels, [(x, bottom) for x in range(left, right + 1)]):
            bottom -= 1
            changed = True
            continue
        if _uniform_line(pixels, [(left, y) for y in range(top, bottom + 1)]):
            left += 1
            changed = True
            continue
        if _uniform_line(pixels, [(right, y) for y in range(top, bottom + 1)]):
            right -= 1
            changed = True
            continue
    if right <= left or bottom <= top:
        return rgba
    return rgba.crop((left, top, right + 1, bottom + 1))


def crop_content(image: Image.Image) -> Image.Image:
    """裁到非背景内容包围盒（把内部残留的黑/白背景也去掉）。"""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a < 8:
                continue
            if max(r, g, b) > BLACK and min(r, g, b) < WHITE:
                left = min(left, x); right = max(right, x)
                top = min(top, y); bottom = max(bottom, y)
    if right < left or bottom < top:
        return rgba
    return rgba.crop((left, top, right + 1, bottom + 1))


def transparent_corner_bg(image: Image.Image, tolerance: int = 46) -> Image.Image:
    """若四角颜色一致，则把与该颜色接近的像素设为透明（去掉白底/黑底方块）。"""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    corners = [pixels[0, 0], pixels[width - 1, 0], pixels[0, height - 1], pixels[width - 1, height - 1]]
    base = max(corners, key=lambda px: (px[3], -max(px[:3])))  # 取 alpha 最高的角
    if base[3] < 8:
        return rgba
    br, bg_, bb = base[0], base[1], base[2]
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if abs(r - br) <= tolerance and abs(g - bg_) <= tolerance and abs(b - bb) <= tolerance:
                pixels[x, y] = (r, g, b, 0)
    return rgba


def square(image: Image.Image, size: int) -> Image.Image:
    side = max(image.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2), image)
    return canvas.resize((size, size), Image.LANCZOS)


def polish_icon(image: Image.Image) -> Image.Image:
    """图标专用：更宽松地去掉白底，并留 2px 透明边（避免任何白边）。"""
    image = transparent_corner_bg(image, tolerance=72)
    bbox = image.getbbox()
    if bbox:
        image = image.crop(bbox)
    side = max(image.size) + 4
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2), image)
    return canvas


def load_clean(path: Path) -> Image.Image:
    image = Image.open(path)
    image = strip_borders(image)
    image = crop_content(image)
    return transparent_corner_bg(image)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    logo = polish_icon(load_clean(DESKTOP / "1.jpeg"))
    square(logo, 48).save(OUT / "logo.png")

    account = polish_icon(load_clean(DESKTOP / "3.jpeg"))
    square(account, 96).save(OUT / "account.png")

    app = square(polish_icon(load_clean(DESKTOP / "3.jpeg")), 256)
    app.save(OUT / "app.png")
    app.save(OUT / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                      (64, 64), (128, 128), (256, 256)])

    background = Image.open(DESKTOP / "2.webp").convert("RGBA")
    background = strip_borders(background).convert("RGB")
    target = (1366, 768)
    scale = max(target[0] / background.width, target[1] / background.height)
    resized = background.resize((int(background.width * scale) + 1, int(background.height * scale) + 1),
                                Image.LANCZOS)
    left = (resized.width - target[0]) // 2
    top = (resized.height - target[1]) // 2
    resized.crop((left, top, left + target[0], top + target[1])).save(OUT / "background.png")

    for name in ("logo.png", "account.png", "app.png", "app.ico", "background.png"):
        path = OUT / name
        print(f"{name}: {path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
