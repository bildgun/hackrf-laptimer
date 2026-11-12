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

# Analog FPV System: pasmo -> kanały CH1..CH8, MHz
TABLE = {
    "A": [5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725],
    "B": [5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866],
    "E": [5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945],
    "F": [5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880],
    "R": [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917],
    "D": [5362, 5399, 5436, 5473, 5510, 5547, 5584, 5621],
    "U": [5325, 5348, 5366, 5384, 5402, 5420, 5438, 5456],
    "O": [5474, 5492, 5510, 5528, 5546, 5564, 5582, 5600],
    "L": [5333, 5373, 5413, 5453, 5493, 5533, 5573, 5613],
    "H": [5653, 5693, 5733, 5773, 5813, 5853, 5893, 5933],
}
CHANNELS = [(f"{band}{i}", f) for band, freqs in TABLE.items() for i, f in enumerate(freqs, 1)]
CH_HALF_BW = 5     # MHz: moc kanału = maksimum w ±5 MHz od jego częstotliwości
MERGE = 10         # MHz: kanały bliżej niż to są podpisywane razem ("B1/R3/H3")
THRESHOLD = 10     # dB ponad szumem, aby kanał uznać za zajęty (kalibracja)
ALPHA = 0.3        # wygładzanie w czasie: 1.0 = bez niego, mniej = spokojniej, ale wolniej


class Scanner:
    def __init__(self, freq="5300:5960", lna=24, vga=30, amp=False, bin_hz=1_000_000):
        cmd = ["hackrf_sweep", "-f", freq, "-w", str(bin_hz), "-l", str(lna), "-g", str(vga), "-a", str(int(amp))]
        # wyciszamy stderr: hackrf_sweep co sekundę wypisuje tam statystyki, które psułyby obraz
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.title = f"Skaner widma {freq.replace(':', '–')} MHz"
        self.spectrum = {}   # MHz -> dB, ostatnia wartość każdego binu

    def read(self, line):
        """Linia hackrf_sweep: data, czas, Hz od, Hz do, szerokość binu, liczba próbek, dB, dB, ..."""
        _, _, lo, _, step, _, *db = line.split(",")
        for i, value in enumerate(db):
            f, v = (float(lo) + (i + 0.5) * float(step)) / 1e6, float(value)
            old = self.spectrum.get(f, v)
            self.spectrum[f] = old + ALPHA * (v - old)

    def peaks(self, floor):
        """[(częstotliwość, dB, [kanały])] powyżej szumu, od najsilniejszego do najsłabszego."""
        found = []
        for name, f in CHANNELS:
            p = max((v for x, v in self.spectrum.items() if abs(x - f) <= CH_HALF_BW), default=None)
            if p is not None and p > floor + THRESHOLD:
                found.append((p, f, name))
        groups = []
        for p, f, name in sorted(found, reverse=True):
            group = next((g for g in groups if abs(g[0] - f) < MERGE), None)
            if group:
                group[2].append(name)
            else:
                groups.append((f, p, [name]))
        return groups

    def draw(self, ymin, ymax):
        xs = sorted(self.spectrum)
        ys = [self.spectrum[x] for x in xs]
        floor = statistics.median(ys)
        groups = self.peaks(floor)

        fig = plotext.figure
        fig.clear.data()
        fig.clear.size()                      # uwzględnić nowy rozmiar terminala
        curve = fig.signal(xs, ys, marker="braille")
        curve.lines()
        curve.fill(fig.signal(xs, [ymin] * len(xs)))   # wypełnienie w dół do krawędzi wykresu
        fig.draw(curve)
        for f, p, names in groups:
            fig.draw(fig.text(f, min(p + 3, ymax), "/".join(names), alignment="center"))
        fig.title(self.title)
        fig.label("Częstotliwość [MHz]", axis="x")
        fig.label("Moc [dB]", axis="y")
        fig.ruler("x").lim(round(xs[0]), round(xs[-1]))   # całe MHz na osi
        fig.ruler("y").lim(ymin, ymax).ticks(list(range(int(ymax), int(ymin) - 1, -10)))   # krok 10 dB

        top = " | ".join(f"{'/'.join(n)} {f} MHz {p:.0f} dB" for f, p, n in groups[:5]) or "brak"
        status = f"Szum: {floor:.0f} dB   Najsilniejsze: {top}"[: shutil.get_terminal_size().columns]
        print("\033[H\033[J" + fig.build().string() + "\n" + status, end="", flush=True)

    def run(self, ymin=-75, ymax=0, fps=5):
        next_frame = 0
        for line in self.proc.stdout:
            self.read(line)
            if time.monotonic() >= next_frame:
                self.draw(ymin, ymax)
                next_frame = time.monotonic() + 1 / fps
        sys.exit(f"\nhackrf_sweep zakończył działanie (kod {self.proc.wait()}). "
                 "Czy HackRF jest podłączony? Sprawdź: hackrf_info")

    def close(self):
        self.proc.terminate()
        self.proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-f", "--freq", default="5300:5960", help="zakres częstotliwości w MHz, min:max")
    p.add_argument("--lna", type=int, default=24, help="wzmocnienie LNA w dB (0–40, co 8)")
    p.add_argument("--vga", type=int, default=30, help="wzmocnienie VGA w dB (0–62, co 2)")
    p.add_argument("--amp", action="store_true", help="przedwzmacniacz +14 dB (nie włączaj w pobliżu nadajnika)")
    p.add_argument("--ymin", type=float, default=-75, help="dolna granica osi Y w dB")
    p.add_argument("--ymax", type=float, default=0, help="górna granica osi Y w dB")
    a = p.parse_args()

    with Scanner(a.freq, a.lna, a.vga, a.amp) as scanner:
        try:
            scanner.run(a.ymin, a.ymax)
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
