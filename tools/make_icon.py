"""图标生成脚本：生成 resources/icon.ico 与 icon.png。

无需任何外部素材，直接画出「蓝色圆角方块 + 白色时钟」图标。
运行：``python tools/make_icon.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "resources"


def build_images():
    """返回多种尺寸的 PIL 图像列表。"""
    from PIL import Image, ImageDraw

    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    images = []
    for size in sizes:
        scale = size / 256.0
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        def s(value: float) -> float:
            """把 256 基准坐标缩放到当前尺寸。"""
            return value * scale

        # 圆角背景（蓝色渐变感：上浅下深两层近似）
        draw.rounded_rectangle([s(8), s(8), s(248), s(248)], radius=s(56), fill=(79, 140, 255, 255))
        draw.rounded_rectangle([s(8), s(8), s(248), s(150)], radius=s(56), fill=(96, 158, 255, 255))

        # 表盘
        center = s(128)
        radius = s(88)
        draw.ellipse(
            [center - radius, center - radius, center + radius, center + radius],
            fill=(255, 255, 255, 255),
        )
        inner = s(76)
        draw.ellipse(
            [center - inner, center - inner, center + inner, center + inner],
            fill=(79, 140, 255, 255),
        )

        # 指针
        draw.line([center, center, center, center - s(52)], fill=(255, 255, 255, 255), width=max(1, int(s(16))))
        draw.line([center, center, center + s(38), center + s(22)], fill=(255, 255, 255, 255), width=max(1, int(s(14))))
        images.append(img)
    return images


def main() -> int:
    """生成图标文件。"""
    try:
        import PIL  # noqa: F401  仅用于检查依赖是否可用
    except ImportError:
        print("需要 Pillow：pip install Pillow")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    images = build_images()
    ico_path = OUT_DIR / "icon.ico"
    png_path = OUT_DIR / "icon.png"
    images[-1].save(ico_path, format="ICO", sizes=[(im.width, im.height) for im in images])
    images[-1].save(png_path, format="PNG")
    print(f"已生成: {ico_path}")
    print(f"已生成: {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
