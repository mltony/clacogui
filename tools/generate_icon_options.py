"""Draw alternative Clacogui icons as SVG, PNG and ICO (requires Pillow)."""

from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path(__file__).resolve().parents[1] / "assets" / "icon-options"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SCALE = 4


class Drawing:
    def __init__(self, title):
        self.title = title
        self.image = Image.new("RGBA", (256 * SCALE, 256 * SCALE))
        self.draw = ImageDraw.Draw(self.image)
        self.svg = []

    @staticmethod
    def scaled(values):
        return tuple(value * SCALE for value in values)

    def rounded(self, box, radius, fill):
        self.draw.rounded_rectangle(self.scaled(box), radius=radius * SCALE, fill=fill)
        x, y, right, bottom = box
        self.svg.append(f'<rect x="{x}" y="{y}" width="{right-x}" height="{bottom-y}" rx="{radius}" fill="{fill}"/>')

    def ellipse(self, box, fill):
        self.draw.ellipse(self.scaled(box), fill=fill)
        x, y, right, bottom = box
        self.svg.append(f'<ellipse cx="{(x+right)/2}" cy="{(y+bottom)/2}" rx="{(right-x)/2}" ry="{(bottom-y)/2}" fill="{fill}"/>')

    def polygon(self, points, fill):
        self.draw.polygon([self.scaled(point) for point in points], fill=fill)
        values = " ".join(f"{x},{y}" for x, y in points)
        self.svg.append(f'<polygon points="{values}" fill="{fill}"/>')

    def line(self, points, fill, width):
        self.draw.line([self.scaled(point) for point in points], fill=fill, width=width * SCALE, joint="curve")
        radius = width / 2
        for x, y in (points[0], points[-1]):
            self.draw.ellipse(self.scaled((x-radius, y-radius, x+radius, y+radius)), fill=fill)
        values = " ".join(f"{x},{y}" for x, y in points)
        self.svg.append(f'<polyline points="{values}" fill="none" stroke="{fill}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')

    def smile(self, box, fill, width):
        self.draw.arc(self.scaled(box), 25, 155, fill=fill, width=width * SCALE)
        # An SVG elliptical arc using the same angles as Pillow.
        import math
        x, y, right, bottom = box
        cx, cy, rx, ry = (x+right)/2, (y+bottom)/2, (right-x)/2, (bottom-y)/2
        points = [(cx + rx * math.cos(math.radians(angle)), cy + ry * math.sin(math.radians(angle))) for angle in (25, 155)]
        self.svg.append(f'<path d="M{points[0][0]} {points[0][1]} A{rx} {ry} 0 0 1 {points[1][0]} {points[1][1]}" fill="none" stroke="{fill}" stroke-width="{width}"/>')

    def save(self, name, directory=None):
        directory = OUTPUT if directory is None else Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        image = self.image.resize((256, 256), Image.Resampling.LANCZOS)
        image.save(directory / f"{name}.png")
        image.save(directory / f"{name}.ico", sizes=[(size, size) for size in SIZES])
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">\n'
        svg += f'<title>{escape(self.title)}</title>\n' + "\n".join(self.svg) + "\n</svg>\n"
        (directory / f"{name}.svg").write_text(svg, encoding="utf-8")
        return image


def square_robot():
    d = Drawing("Clacogui: friendly robot")
    d.rounded((8, 8, 248, 248), 52, "#4338CA")
    d.line(((128, 76), (128, 46)), "#F8FAFC", 12)
    d.ellipse((114, 24, 142, 52), "#FB7185")
    d.rounded((30, 112, 62, 164), 12, "#5EEAD4")
    d.rounded((194, 112, 226, 164), 12, "#5EEAD4")
    d.rounded((48, 70, 208, 204), 34, "#F8FAFC")
    d.rounded((64, 90, 192, 182), 24, "#172554")
    d.rounded((82, 111, 104, 140), 11, "#5EEAD4")
    d.rounded((152, 111, 174, 140), 11, "#5EEAD4")
    d.smile((107, 133, 149, 166), "#5EEAD4", 7)
    return d


def round_robot():
    d = Drawing("Clacogui: round smiling robot")
    d.rounded((8, 8, 248, 248), 52, "#FFF1D6")
    d.line(((128, 75), (128, 44)), "#334155", 12)
    d.ellipse((113, 22, 143, 52), "#F97316")
    d.rounded((25, 119, 62, 174), 15, "#60A5FA")
    d.rounded((194, 119, 231, 174), 15, "#60A5FA")
    d.ellipse((45, 62, 211, 222), "#1E3A5F")
    d.ellipse((62, 80, 194, 207), "#F8FAFC")
    d.rounded((83, 111, 104, 141), 10, "#1E3A5F")
    d.rounded((152, 111, 173, 141), 10, "#1E3A5F")
    d.ellipse((73, 143, 96, 155), "#FDBA9D")
    d.ellipse((160, 143, 183, 155), "#FDBA9D")
    d.smile((102, 141, 154, 182), "#1E3A5F", 8)
    return d


def owl():
    d = Drawing("Clacogui: curious golden owl")
    d.rounded((8, 8, 248, 248), 52, "#172554")
    d.polygon(((48, 48), (101, 76), (155, 76), (208, 48), (194, 145), (62, 145)), "#FBBF24")
    d.ellipse((52, 72, 204, 224), "#FBBF24")
    d.ellipse((59, 83, 130, 155), "#FFFBEB")
    d.ellipse((126, 83, 197, 155), "#FFFBEB")
    d.ellipse((83, 103, 109, 136), "#172554")
    d.ellipse((147, 103, 173, 136), "#172554")
    d.polygon(((128, 142), (143, 155), (128, 170), (113, 155)), "#EA580C")
    d.ellipse((53, 155, 87, 205), "#F59E0B")
    d.ellipse((169, 155, 203, 205), "#F59E0B")
    d.line(((111, 185), (118, 191), (125, 185)), "#FFFBEB", 5)
    d.line(((135, 185), (142, 191), (149, 185)), "#FFFBEB", 5)
    return d


def bubbles():
    d = Drawing("Clacogui: two conversation bubbles")
    d.rounded((8, 8, 248, 248), 52, "#F3E8FF")
    d.rounded((90, 39, 226, 149), 30, "#7C3AED")
    d.polygon(((173, 136), (220, 169), (214, 132)), "#7C3AED")
    for x in (117, 150, 183):
        d.ellipse((x, 79, x + 15, 94), "#F5F3FF")
    d.rounded((28, 102, 180, 212), 31, "#0D9488")
    d.polygon(((53, 197), (51, 235), (101, 201)), "#0D9488")
    for x in (57, 94, 131):
        d.ellipse((x, 147, x + 16, 163), "#F0FDFA")
    return d


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    options = (("robot-square", "1. Friendly robot", square_robot),
               ("robot-round", "2. Round robot", round_robot),
               ("owl", "3. Curious owl", owl),
               ("conversation-bubbles", "4. Conversation bubbles", bubbles))
    sheet = Image.new("RGB", (600, 680), "#E5E7EB")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/seguisb.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, title, factory) in enumerate(options):
        image = factory().save(name)
        x, y = 22 + (i % 2) * 300, 18 + (i // 2) * 340
        sheet.paste(image, (x, y), image)
        draw.text((x + 128, y + 276), title, fill="#172554", font=font, anchor="mt")
    sheet.save(OUTPUT / "icon-options.png")
    print("Created four SVG/PNG/ICO alternatives and icon-options.png")


if __name__ == "__main__":
    main()
