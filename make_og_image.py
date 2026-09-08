"""tarsus.world sosyal medya (Open Graph) önizleme görselini üretir -> og-image.png"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
FONT_DIR = "/usr/share/fonts/truetype/liberation/"
bold = lambda s: ImageFont.truetype(FONT_DIR + "LiberationSans-Bold.ttf", s)
reg = lambda s: ImageFont.truetype(FONT_DIR + "LiberationSans-Regular.ttf", s)

# --- Arka plan: dikey yeşil gradyan ---
img = Image.new("RGB", (W, H))
bg = ImageDraw.Draw(img)
top, bot = (11, 31, 23), (4, 39, 28)
for y in range(H):
    t = y / H
    bg.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
draw = ImageDraw.Draw(img, "RGBA")
draw.ellipse([-260, -320, 560, 380], fill=(16, 120, 80, 38))

# ================= Limon rozeti (4x süper-örnekleme) =================
S = 4
B = 280                      # rozet kenar (gerçek px)
bs = B * S
badge = Image.new("RGBA", (bs, bs), (0, 0, 0, 0))
bd = ImageDraw.Draw(badge)
bd.rounded_rectangle([0, 0, bs - 1, bs - 1], radius=72 * S, fill=(15, 118, 77, 255))
bd.rounded_rectangle([3 * S, 3 * S, bs - 3 * S, bs - 3 * S], radius=70 * S,
                     outline=(52, 211, 153, 255), width=4 * S)

def rot_paste(layer, angle, cx, cy):
    r = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    badge.alpha_composite(r, (cx - r.width // 2, cy - r.height // 2))

# yaprak (limonun arkasında, sol üst)
leaf = Image.new("RGBA", (150 * S, 90 * S), (0, 0, 0, 0))
ImageDraw.Draw(leaf).ellipse([0, 0, 150 * S, 90 * S], fill=(74, 222, 128, 255),
                             outline=(21, 128, 61, 255), width=4 * S)
ImageDraw.Draw(leaf).line([30 * S, 63 * S, 120 * S, 30 * S],
                          fill=(20, 83, 45, 220), width=4 * S)
rot_paste(leaf, 32, int(bs * 0.60), int(bs * 0.30))

# limon gövdesi
body = Image.new("RGBA", (210 * S, 150 * S), (0, 0, 0, 0))
lb = ImageDraw.Draw(body)
lb.ellipse([6 * S, 6 * S, 204 * S, 144 * S], fill=(253, 224, 71, 255),
           outline=(202, 138, 4, 255), width=5 * S)
# uç tomurcukları
lb.ellipse([0, 63 * S, 20 * S, 87 * S], fill=(250, 204, 21, 255))
lb.ellipse([190 * S, 63 * S, 210 * S, 87 * S], fill=(250, 204, 21, 255))
# parlama
lb.ellipse([40 * S, 28 * S, 86 * S, 52 * S], fill=(255, 255, 255, 70))
rot_paste(body, 26, int(bs * 0.46), int(bs * 0.60))

badge = badge.resize((B, B), Image.LANCZOS)
img.paste(badge, (95, 175), badge)

# ================= Metinler =================
x = 445
draw.text((x, 180), "TarsusAI", font=bold(122), fill=(255, 255, 255))
draw.text((x + 3, 322), "Yapay Zekâ Tarım Danışmanı", font=bold(52), fill=(110, 231, 183))
draw.line([(x + 4, 400), (x + 680, 400)], fill=(52, 211, 153, 130), width=3)
draw.text((x + 4, 420), "Hava durumu · İlaçlama · Gübreleme · Hastalık teşhisi",
          font=reg(30), fill=(160, 167, 178))
draw.text((x + 4, 474), "tarsus.world", font=bold(33), fill=(253, 224, 71))

img.save("/root/tarsusai/og-image.png", "PNG")
print("yazıldı: /root/tarsusai/og-image.png", img.size)
