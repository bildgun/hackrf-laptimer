#!/home/bildgun/hackrf/.venv/bin/python
"""Skaner pasma FPV 5,8 GHz na HackRF: wykres mocy w terminalu, jak skaner Wi-Fi.

Uruchomienie: hackrf-rx
              hackrf-rx --lna 32 --vga 40     # bardzo słabe, odległe sygnały
              hackrf-rx --lna 8 --vga 20      # nadajnik blisko (mniej przesterowania)
              hackrf-rx -f 5600:5960          # węższy zakres
Wyjście:      Ctrl+C
"""
import argparse
import shutil
import statistics
import subprocess
import sys
import time

import plotext

from scanner import ALPHA, find_peaks, parse_line, status_text

CLEAR_SCREEN = "\033[H\033[J"  # kursor w lewy górny róg i czyszczenie ekranu


class TerminalScanner:
    """Czyta hackrf_sweep w głównym wątku i co klatkę rysuje widmo w terminalu."""

    def __init__(self, freq="5300:5960", lna=24, vga=30, amp=False, bin_hz=1_000_000):
        cmd = ["hackrf_sweep", "-f", freq, "-w", str(bin_hz), "-l", str(lna), "-g", str(vga), "-a", str(int(amp))]
        # hackrf_sweep co sekundę wypisuje na stderr statystyki, które rozjeżdżałyby wykres
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.title = f"Skaner widma {freq.replace(':', '–')} MHz"
        self.spectrum = {}  # MHz -> wygładzony poziom w dB

    def read(self, line):
        for freq, level in parse_line(line):
            previous = self.spectrum.get(freq, level)
            self.spectrum[freq] = previous + ALPHA * (level - previous)

    def draw(self, ymin, ymax):
        freqs = sorted(self.spectrum)
        levels = [self.spectrum[f] for f in freqs]
        floor = statistics.median(levels)
        peaks = find_peaks(self.spectrum, floor)

        fig = plotext.figure
        fig.clear.data()
        fig.clear.size()  # bez tego wykres nie dopasuje się do nowego rozmiaru terminala
        curve = fig.signal(freqs, levels, marker="braille")
        curve.lines()
        # zamalowanie pod krzywą aż do dolnej krawędzi wykresu
        curve.fill(fig.signal(freqs, [ymin] * len(freqs)))
        fig.draw(curve)
        for freq, power, names in peaks:
            label_y = min(power + 3, ymax)
            fig.draw(fig.text(freq, label_y, "/".join(names), alignment="center"))

        fig.title(self.title)
        fig.label("Częstotliwość [MHz]", axis="x")
        fig.label("Moc [dB]", axis="y")
        fig.ruler("x").lim(round(freqs[0]), round(freqs[-1]))
        fig.ruler("y").lim(ymin, ymax).ticks(list(range(int(ymax), int(ymin) - 1, -10)))

        status = status_text(floor, peaks)[:shutil.get_terminal_size().columns]
        print(CLEAR_SCREEN + fig.build().string() + "\n" + status, end="", flush=True)

    def run(self, ymin=-75, ymax=0, fps=5):
        # Linie przychodzą dużo częściej, niż warto przerysowywać ekran
        next_frame = 0
        for line in self.proc.stdout:
            self.read(line)
            if time.monotonic() >= next_frame:
                self.draw(ymin, ymax)
                next_frame = time.monotonic() + 1 / fps

        code = self.proc.wait()
        sys.exit(f"\nhackrf_sweep zakończył działanie (kod {code}). Czy HackRF jest podłączony? Sprawdź: hackrf_info")

    def close(self):
        self.proc.terminate()
        self.proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-f", "--freq", default="5300:5960", help="zakres częstotliwości w MHz, min:max")
    parser.add_argument("--lna", type=int, default=24, help="wzmocnienie LNA w dB (0–40, co 8)")
    parser.add_argument("--vga", type=int, default=30, help="wzmocnienie VGA w dB (0–62, co 2)")
    parser.add_argument("--amp", action="store_true", help="przedwzmacniacz +14 dB (nie włączaj w pobliżu nadajnika)")
    parser.add_argument("--ymin", type=float, default=-75, help="dolna granica osi Y w dB")
    parser.add_argument("--ymax", type=float, default=0, help="górna granica osi Y w dB")
    args = parser.parse_args()

    with TerminalScanner(args.freq, args.lna, args.vga, args.amp) as scanner:
        try:
            scanner.run(args.ymin, args.ymax)
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
