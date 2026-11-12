"""Odbiór widma z HackRF przez hackrf_sweep. Same dane, bez interfejsu."""
import statistics
import subprocess
import threading

# Kanały analogowego FPV: pasmo -> częstotliwości CH1..CH8 w MHz
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
CHANNELS = [(f"{band}{i}", freq) for band, freqs in TABLE.items() for i, freq in enumerate(freqs, 1)]

CH_HALF_BW = 5   # MHz, moc kanału to maksimum w promieniu 5 MHz od jego częstotliwości
MERGE = 10       # MHz, bliższe kanały podpisujemy razem, np. "B1/R3/H3"
THRESHOLD = 10   # dB ponad szumem, od których uznajemy kanał za zajęty
ALPHA = 0.3      # wygładzanie w czasie; 1.0 to brak wygładzania, mniej daje spokojniejszy, ale wolniejszy wykres


def parse_line(line):
    """Zamienia linię z hackrf_sweep na pary (środek binu w MHz, poziom w dB).

    Format linii: data, czas, Hz od, Hz do, szerokość binu, liczba próbek, dB, dB, ...
    """
    _, _, hz_low, _, bin_width, _, *levels = line.split(",")
    for i, level in enumerate(levels):
        yield (float(hz_low) + (i + 0.5) * float(bin_width)) / 1e6, float(level)


def channel_power(spectrum, freq):
    """Najwyższy poziom w promieniu CH_HALF_BW od kanału albo None, jeśli nie ma tam żadnego binu."""
    levels = [level for f, level in spectrum.items() if abs(f - freq) <= CH_HALF_BW]
    return max(levels) if levels else None


def find_peaks(spectrum, floor):
    """Zajęte kanały, od najsilniejszego: lista (częstotliwość, dB, [nazwy kanałów]).

    Kanały z różnych pasm leżące bliżej niż MERGE trafiają do jednej grupy,
    bo na wykresie i tak widać je jako jeden pik.
    """
    busy = []
    for name, freq in CHANNELS:
        power = channel_power(spectrum, freq)
        if power is not None and power > floor + THRESHOLD:
            busy.append((power, freq, name))

    groups = []
    for power, freq, name in sorted(busy, reverse=True):
        for group_freq, _, names in groups:
            if abs(group_freq - freq) < MERGE:
                names.append(name)
                break
        else:
            groups.append((freq, power, [name]))
    return groups


def status_text(floor, peaks):
    """Linia pod wykresem: poziom szumu i pięć najsilniejszych sygnałów."""
    strongest = " | ".join(f"{'/'.join(names)} {freq} MHz {power:.0f} dB" for freq, power, names in peaks[:5])
    return f"Szum: {floor:.0f} dB   Najsilniejsze: {strongest or 'brak'}"


class Scanner:
    """Uruchamia hackrf_sweep, a wątek w tle na bieżąco zbiera z niego wygładzone widmo."""

    def __init__(self, freq="800:1000", lna=24, vga=30, amp=False, bin_hz=1_000_000):
        cmd = ["hackrf_sweep", "-f", freq, "-w", str(bin_hz), "-l", str(lna), "-g", str(vga), "-a", str(int(amp))]
        # stderr zostaje w konsoli: interfejsowi w przeglądarce nie przeszkadza, a błędy hackrf_sweep widać od razu
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        self.spectrum = {}  # MHz -> wygładzony poziom w dB
        self.hold = {}      # MHz -> najwyższy surowy odczyt (max-hold)
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        for line in self.proc.stdout:
            self.read(line)

    def read(self, line):
        for freq, level in parse_line(line):
            previous = self.spectrum.get(freq, level)
            self.spectrum[freq] = previous + ALPHA * (level - previous)
            self.hold[freq] = max(self.hold.get(freq, level), level)

    def clear_hold(self):
        self.hold = {}

    def snapshot(self):
        """Widmo do narysowania: (częstotliwości, poziomy, max-hold, szum, piki).

        Pracuje na kopii, bo wątek odbioru może akurat dopisywać nowe biny.
        """
        spectrum, hold = dict(self.spectrum), dict(self.hold)
        freqs = sorted(spectrum)
        levels = [spectrum[f] for f in freqs]
        held = [hold.get(f) for f in freqs]  # None, dopóki bin nie przyjdzie ponownie po clear_hold()
        if not levels:
            return freqs, levels, held, None, []
        floor = statistics.median(levels)
        return freqs, levels, held, floor, find_peaks(spectrum, floor)

    def close(self):
        self.proc.terminate()
        self.proc.wait()
