"""Odbiór widma z HackRF przez hackrf_sweep: tylko dane, bez interfejsu."""
import statistics
import subprocess
import threading

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
    """Uruchamia hackrf_sweep i w wątku w tle gromadzi wygładzone widmo."""

    def __init__(self, freq="800:1000", lna=24, vga=30, amp=False, bin_hz=1_000_000):
        cmd = ["hackrf_sweep", "-f", freq, "-w", str(bin_hz), "-l", str(lna), "-g", str(vga), "-a", str(int(amp))]
        # nie wyciszamy stderr: nie psuje obrazu w przeglądarce, a błędy hackrf_sweep są widoczne w konsoli
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        self.spectrum = {}   # MHz -> dB, ostatnia wartość każdego binu
        self.hold = {}       # MHz -> dB, maksimum surowych wartości (max-hold), bez wygładzania
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        for line in self.proc.stdout:
            self.read(line)

    def read(self, line):
        """Linia hackrf_sweep: data, czas, Hz od, Hz do, szerokość binu, liczba próbek, dB, dB, ..."""
        _, _, lo, _, step, _, *db = line.split(",")
        for i, value in enumerate(db):
            f, v = (float(lo) + (i + 0.5) * float(step)) / 1e6, float(value)
            old = self.spectrum.get(f, v)
            self.spectrum[f] = old + ALPHA * (v - old)
            self.hold[f] = max(self.hold.get(f, v), v)

    def clear_hold(self):
        self.hold = {}

    def snapshot(self):
        """(częstotliwości, dB, max-hold dB, szum, piki) z kopii: wątek w tle może właśnie dodawać biny."""
        spectrum, hold = dict(self.spectrum), dict(self.hold)
        xs = sorted(spectrum)
        ys = [spectrum[x] for x in xs]
        hs = [hold.get(x) for x in xs]   # None: bin jeszcze nie dotarł po clear_hold()
        if not ys:
            return xs, ys, hs, None, []
        floor = statistics.median(ys)
        return xs, ys, hs, floor, self._peaks(spectrum, floor)

    @staticmethod
    def _peaks(spectrum, floor):
        """[(częstotliwość, dB, [kanały])] powyżej szumu, od najsilniejszego do najsłabszego."""
        found = []
        for name, f in CHANNELS:
            p = max((v for x, v in spectrum.items() if abs(x - f) <= CH_HALF_BW), default=None)
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

    def close(self):
        self.proc.terminate()
        self.proc.wait()
