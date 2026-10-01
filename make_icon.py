from pathlib import Path
from PIL import Image, ImageDraw

out = Path(__file__).resolve().parent / "assets" / "power.ico"
size = 256
image = Image.new("RGBA", (size, size), (15, 23, 42, 255))
draw = ImageDraw.Draw(image)
blue = (59, 130, 246, 255)
# Power stem
draw.rounded_rectangle((116, 35, 140, 137), radius=12, fill=blue)
# Power ring built from an arc, leaving the top open.
draw.arc((48, 48, 208, 208), start=315, end=585, fill=blue, width=24)
image.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
