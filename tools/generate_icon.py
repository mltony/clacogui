"""Draw the selected friendly-robot icon (development dependency: Pillow)."""

from pathlib import Path

from generate_icon_options import square_robot


def main():
    assets = Path(__file__).resolve().parents[1] / "assets"
    square_robot().save("clacogui", directory=assets)
    print("Generated friendly-robot assets/clacogui.svg, clacogui.png and clacogui.ico")


if __name__ == "__main__":
    main()
