"""Logika wyścigu: przeloty przez bramkę wykrywane po sygnale VTX, okrążenia i czasy. Bez interfejsu."""
import json
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

# scanner.py leży katalog wyżej, w ~/hackrf
sys.path.append(str(Path(__file__).resolve().parent.parent))
from scanner import CH_HALF_BW, CHANNELS, TABLE, Scanner, channel_power  # noqa: E402


def channel_labels():
    """Etykiety kanałów 5,8 GHz do listy wyboru: częstotliwość -> np. "F8/R7 5880".

    Kanały z różnych pasm na tej samej częstotliwości to w praktyce jeden kanał, więc mają wspólną etykietę.
    """
    names_by_freq = {}
    for name, freq in CHANNELS:
        names_by_freq.setdefault(freq, []).append(name)
    return {freq: "/".join(names) + f" {freq}" for freq, names in names_by_freq.items()}


CHANNELS_58 = channel_labels()
BAND_24 = (2400, 2483)  # do testów: TX16S z protokołem CW nadaje na 2400 + RF Chan. MHz
DEFAULT_FREQS_58 = TABLE["R"]  # RaceBand
DEFAULT_FREQS_24 = [2420, 2440, 2460, 2480]
LOGS = Path(__file__).resolve().parent / "logs"  # logs/<data i godzina startu>/<dron>.json


@dataclass
class Pilot:
    name: str
    freq: int     # MHz
    enter: float  # dB; sygnał powyżej oznacza, że dron jest przy bramce i zaczyna się przelot
    exit: float   # dB; poniżej przelot się kończy, a dopóki sygnał nie spadnie, nowy się nie zacznie (histereza)
    level: float | None = None      # ostatni odczyt w dB
    max_level: float | None = None  # maksimum od Zastosuj/Start, według niego ustawia się próg wejścia
    inside: bool = False            # dron jest między wejściem a wyjściem
    peak: float = 0.0               # najwyższy poziom w bieżącym przelocie
    peak_t: float = 0.0             # i jego czas, czyli czas przelotu: wtedy dron jest najbliżej bramki
    passes: list = field(default_factory=list)  # czasy zaliczonych przelotów, pierwszy to holeshot
    log: list = field(default_factory=list)     # (czas, dB) od startu do końca wyścigu, do wykresu po wyścigu

    def sample(self, level, t):
        """Przyjmuje nowy odczyt. Gdy przelot właśnie się skończył, zwraca czas jego szczytu, a w innym razie None."""
        self.level = level
        if self.max_level is None:
            self.max_level = level
        else:
            self.max_level = max(self.max_level, level)

        if not self.inside:
            if level >= self.enter:
                self.inside = True
                self.peak = level
                self.peak_t = t
        elif level < self.exit:
            self.inside = False
            return self.peak_t
        elif level > self.peak:
            self.peak = level
            self.peak_t = t
        return None


class Race:
    """Stan wyścigu.

    feed() woła wątek odbioru, a resztę metod interfejs, dlatego wszystko działa pod jednym zamkiem.
    """

    def __init__(self, log_dir=None):
        self.lock = threading.Lock()
        self.log_dir = log_dir  # None = logów nie zapisujemy
        self.pilots = []
        self.laps = 3
        self.min_lap = 3.0
        # Czasy z time.time(). Dopóki start_t jest None, wyścig jeszcze nie ruszył.
        self.start_t = None
        self.stop_t = None

    @property
    def running(self):
        return self.start_t is not None and self.stop_t is None

    def configure(self, pilots, laps, min_lap):
        with self.lock:
            self.pilots = pilots
            self.laps = laps
            self.min_lap = min_lap
            self.start_t = None
            self.stop_t = None

    def start(self, t):
        with self.lock:
            for pilot in self.pilots:
                pilot.inside = False
                pilot.max_level = pilot.level
                pilot.passes = []
                pilot.log = []
            self.start_t = t
            self.stop_t = None

    def stop(self, t):
        with self.lock:
            if self.running:
                self._finish(t)

    def done(self, pilot):
        # holeshot i po jednym przelocie na każde okrążenie
        return len(pilot.passes) > self.laps

    def feed(self, lo, hi, t, level_at):
        """Nowa linia widma od lo do hi MHz z czasem t. Aktualizuje drony, których kanał w nią wpada."""
        with self.lock:
            for pilot in self.pilots:
                if not lo <= pilot.freq < hi:
                    continue
                level = level_at(pilot.freq)
                if self.running and t >= self.start_t:
                    # Każda próbka zostaje w pamięci, to do ~400 na sekundę na drona.
                    # Przy wyścigach trwających godzinami trzeba będzie to przerzedzić.
                    pilot.log.append((t, level))
                self._count_pass(pilot, pilot.sample(level, t))

    def _count_pass(self, pilot, t):
        if t is None or not self.running or t < self.start_t or self.done(pilot):
            return
        if pilot.passes and t - pilot.passes[-1] < self.min_lap:
            # Zanik sygnału przy bramce (odbicia) potrafi dać drugi przelot tuż po pierwszym.
            # Taki przelot po prostu odrzucamy, nawet jeśli jego szczyt był wyższy.
            return
        pilot.passes.append(t)
        if all(self.done(p) for p in self.pilots):
            # Wszyscy na mecie, więc zegar wyścigu zatrzymuje się na ostatnim przelocie
            self._finish(t)

    def _finish(self, t):
        """Zatrzymuje zegar i zapisuje sygnał każdego drona do log_dir/<data i godzina startu>/<dron>.json."""
        self.stop_t = t
        if self.log_dir is None:
            return
        try:
            folder = self.log_dir / datetime.fromtimestamp(self.start_t).strftime("%Y-%m-%d_%H-%M-%S")
            folder.mkdir(parents=True, exist_ok=True)
            for pilot in self.pilots:
                data = {
                    "enter": pilot.enter,
                    "exit": pilot.exit,
                    "passes": [round(when - self.start_t, 3) for when in pilot.passes],
                    "samples": [[round(when - self.start_t, 3), round(level, 1)] for when, level in pilot.log],
                }
                (folder / f"{pilot.name}.json").write_text(json.dumps(data))
        except OSError as e:
            # Tu trafia też wątek odbioru. Wyjątek by go zabił, a razem z nim pomiar sygnału.
            print(f"Nie zapisano logów wyścigu: {e}")

    def standings(self, now):
        """Wyniki każdego drona w kolejności dodania: okrążenie, czasy w sekundach i sygnał w dB."""
        with self.lock:
            rows = []
            for pilot in self.pilots:
                lap_times = [b - a for a, b in zip(pilot.passes, pilot.passes[1:])]
                finished = self.done(pilot)
                if finished:
                    end = pilot.passes[-1]
                else:
                    end = self.stop_t or now
                rows.append({
                    "name": pilot.name,
                    "freq": pilot.freq,
                    "level": pilot.level,
                    "max_level": pilot.max_level,
                    "lap": min(len(pilot.passes), self.laps),
                    "done": finished,
                    "passes": len(pilot.passes),
                    "last": lap_times[-1] if lap_times else None,
                    "best": min(lap_times, default=None),
                    "total": None if self.start_t is None else end - self.start_t,
                })
            return rows


def check(pilots, laps, test24):
    """Błędy w ustawieniach, przez które okrążenia liczyłyby się źle. Pusta lista znaczy, że wszystko gra."""
    errors = []
    if not pilots:
        errors.append("Dodaj co najmniej jednego drona")

    names = [pilot.name for pilot in pilots]
    if len(set(names)) < len(names) or any("/" in name for name in names):
        errors.append("Nazwy dronów muszą być różne i bez „/”: to nazwy plików z logami")
    if laps < 1:
        errors.append("Liczba okrążeń musi być co najmniej 1")

    for pilot in pilots:
        if pilot.exit >= pilot.enter:
            errors.append(f"{pilot.name}: próg wyjścia musi być niższy od progu wejścia")
        if test24 and not BAND_24[0] <= pilot.freq <= BAND_24[1]:
            errors.append(f"{pilot.name}: częstotliwość poza {BAND_24[0]}–{BAND_24[1]} MHz")

    for a, b in combinations(pilots, 2):
        if abs(a.freq - b.freq) <= 2 * CH_HALF_BW:
            errors.append(f"{a.name} i {b.name}: kanały za blisko (≤ {2 * CH_HALF_BW} MHz), "
                          "sygnał jednego zaliczy okrążenie drugiemu")
    return errors


def sweep_range(pilots):
    """Zakres dla hackrf_sweep obejmujący tylko kanały dronów.

    Im węższy zakres, tym częściej hackrf_sweep wraca do każdego kanału.
    """
    freqs = [pilot.freq for pilot in pilots]
    margin = 2 * CH_HALF_BW
    return f"{min(freqs) - margin}:{max(freqs) + margin}"


class RaceScanner(Scanner):
    """Scanner, który po każdej linii z hackrf_sweep przekazuje poziomy kanałów do wyścigu."""

    def __init__(self, race, freq, lna, vga):
        # Musi być ustawione przed super().__init__(), bo tam startuje wątek odbioru, który od razu woła read()
        self.race = race
        super().__init__(freq, lna, vga)

    def read(self, line):
        super().read(line)
        date, clock, hz_low, hz_high = line.split(",")[:4]
        # Bierzemy czas z hackrf_sweep (moment transferu z USB), a nie chwilę odczytu linii,
        # bo potok oddaje linie paczkami.
        t = datetime.strptime(f"{date} {clock.strip()}", "%Y-%m-%d %H:%M:%S.%f").timestamp()
        self.race.feed(float(hz_low) / 1e6, float(hz_high) / 1e6, t, self.level)

    def level(self, freq):
        return channel_power(self.spectrum, freq)
