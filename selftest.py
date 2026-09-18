from pathlib import Path
import sys

from PIL import Image

from modules.vision import extract_coordinates_from_photo, normalize_text


def main():
    assert normalize_text("Imagens de Campo") == "IMAGENSDECAMPO"
    samples = [
        "-6.04854, -44.25232",
        "GPS -6,04854 -44,25232",
    ]
    from modules.vision import _find_coordinate_pairs, _pick_best_coordinate
    for s in samples:
        best = _pick_best_coordinate(_find_coordinate_pairs(s))
        assert best is not None, s
        assert abs(best[0] + 6.04854) < 1e-6
        assert abs(best[1] + 44.25232) < 1e-6
    print("SELFTEST OK")


if __name__ == "__main__":
    main()
