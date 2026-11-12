"""Logika wyścigu: przeloty przez bramkę po sygnale VTX, okrążenia, czasy. Bez interfejsu.

Samotest: python race.py
"""
import json
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))   # scanner.py leży poziom wyżej, w ~/hackrf
from scanner import CH_HALF_BW, CHANNELS, TABLE, Scanner  # noqa: E402

# 5,8 GHz: częstotliwość -> etykieta; kanały o tej samej częstotliwości to jeden kanał (F8 i R7 = 5880 -> "F8/R7 5880")
CHANNELS_58 = {f: "/".join(n for n, x in CHANNELS if x == f) + f" {f}" for _, f in CHANNELS}
BAND_24 = (2400, 2483)   # test: TX16S z protokołem CW nadaje na 2400 + RF Chan. MHz
DEFAULT_FREQS = {False: TABLE["R"], True: [2420, 2440, 2460, 2480]}   # częstotliwości nowych dronów: RaceBand / test 2,4 GHz
LOGS = Path(__file__).resolve().parent / "logs"   # logs/<data i czas startu>/<dron>.json: sygnał drona w czasie wyścigu


@dataclass
class Pilot:
    name: str
    freq: int          # MHz
    enter: float       # dB: sygnał powyżej -> dron przy bramce, zaczyna się przelot
    exit: float        # dB: sygnał poniżej -> przelot zakończony; dopóki nie spadnie, nowy przelot się nie zacznie (histereza)
    level: float | None = None       # ostatni poziom, dB
    max_level: float | None = None   # maksimum od Zastosuj/Start: według niego ustawia się próg wejścia
    inside: bool = False             # między wejściem a wyjściem
    peak: float = 0.0                # maksimum bieżącego przelotu
    peak_t: float = 0.0              # i jego czas: to właśnie jest czas przelotu (dron najbliżej bramki)
    passes: list = field(default_factory=list)   # czasy zaliczonych przelotów, [0] = holeshot
    log: list = field(default_factory=list)      # (czas, dB) od startu do końca wyścigu: wykres po wyścigu

    def sample(self, level, t):
        """Nowy poziom. Zwraca czas piku, gdy przelot się zakończył, w przeciwnym razie None."""
        self.level = level
        self.max_level = level if self.max_level is None else max(self.max_level, level)
        if not self.inside:
            if level >= self.enter:
                self.inside, self.peak, self.peak_t = True, level, t
        elif level < self.exit:
            self.inside = False
            return self.peak_t
        elif level > self.peak:
            self.peak, self.peak_t = level, t
        return None


class Race:
    """Stan wyścigu. feed() woła wątek odbioru, resztę interfejs, więc wszystko pod jednym zamkiem."""

    def __init__(self, log_dir=None):
        self.lock = threading.Lock()
        self.log_dir = log_dir   # None = bez zapisu (samotest)
        self.pilots, self.laps, self.min_lap = [], 3, 3.0
        self.start_t = self.stop_t = None   # time.time(); start_t None = wyścig jeszcze nie ruszył

    @property
    def running(self):
        return self.start_t is not None and self.stop_t is None

    def configure(self, pilots, laps, min_lap):
        with self.lock:
            self.pilots, self.laps, self.min_lap = pilots, laps, min_lap
            self.start_t = self.stop_t = None

    def start(self, t):
        with self.lock:
            for p in self.pilots:
                p.inside, p.max_level, p.passes, p.log = False, p.level, [], []
            self.start_t, self.stop_t = t, None

    def stop(self, t):
        with self.lock:
            if self.running:
                self._finish(t)

    def done(self, p):
        return len(p.passes) > self.laps   # holeshot + laps przelotów

    def feed(self, lo, hi, t, level_at):
        """Linia widma lo..hi MHz z czasem t: zaktualizować drony, których kanał w nią wpada."""
        with self.lock:
            for p in self.pilots:
                if lo <= p.freq < hi:
                    level = level_at(p.freq)
                    if self.running and t >= self.start_t:
                        # ponytail: każda próbka w pamięci (do ~400/s na drona); przerzedzać, jeśli wyścigi będą wielogodzinne
                        p.log.append((t, level))
                    self._passed(p, p.sample(level, t))

    def _passed(self, p, t):
        if t is None or not self.running or t < self.start_t or self.done(p):
            return
        if p.passes and t - p.passes[-1] < self.min_lap:
            # ponytail: zanik sygnału przy bramce (odbicia) dał drugi przelot - po prostu go odrzucamy,
            # nawet jeśli jego pik był wyższy; łączyć z poprzednim, jeśli okaże się to zauważalne w pomiarach
            return
        p.passes.append(t)
        if all(self.done(q) for q in self.pilots):
            self._finish(t)   # wszyscy ukończyli: zegar wyścigu stoi na ostatnim przelocie

    def _finish(self, t):
        """Koniec wyścigu: zegar staje, sygnał każdego drona trafia do log_dir/<data i czas startu>/<dron>.json."""
        self.stop_t = t
        if self.log_dir is None:
            return
        try:
            folder = self.log_dir / datetime.fromtimestamp(self.start_t).strftime("%Y-%m-%d_%H-%M-%S")
            folder.mkdir(parents=True, exist_ok=True)
            for p in self.pilots:
                (folder / f"{p.name}.json").write_text(json.dumps({
                    "enter": p.enter, "exit": p.exit,
                    "passes": [round(x - self.start_t, 3) for x in p.passes],
                    "samples": [[round(x - self.start_t, 3), round(v, 1)] for x, v in p.log]}))
        except OSError as e:   # woła to też wątek odbioru: wyjątek by go zabił, a z nim pomiar sygnału
            print(f"Nie zapisano logów wyścigu: {e}")

    def standings(self, now):
        """Dla każdego drona w kolejności wprowadzenia: okrążenie, na którym teraz jest, czasy (s), sygnał (dB)."""
        with self.lock:
            rows = []
            for p in self.pilots:
                laps = [b - a for a, b in zip(p.passes, p.passes[1:])]
                end = p.passes[-1] if self.done(p) else (self.stop_t or now)
                rows.append({
                    "name": p.name, "freq": p.freq, "level": p.level, "max_level": p.max_level,
                    "lap": min(len(p.passes), self.laps), "done": self.done(p), "passes": len(p.passes),
                    "last": laps[-1] if laps else None, "best": min(laps, default=None),
                    "total": None if self.start_t is None else end - self.start_t,
                })
            return rows


def check(pilots, laps, test24):
    """Błędy ustawień, przy których okrążenia liczyłyby się błędnie. Pusta lista = wszystko w porządku."""
    errors = [] if pilots else ["Dodaj co najmniej jednego drona"]
    names = [p.name for p in pilots]
    if len(set(names)) < len(names) or any("/" in n for n in names):
        errors.append("Nazwy dronów muszą być różne i bez „/”: to nazwy plików z logami")
    if laps < 1:
        errors.append("Liczba okrążeń musi być co najmniej 1")
    for p in pilots:
        if p.exit >= p.enter:
            errors.append(f"{p.name}: próg wyjścia musi być niższy od progu wejścia")
        if test24 and not BAND_24[0] <= p.freq <= BAND_24[1]:
            errors.append(f"{p.name}: częstotliwość poza {BAND_24[0]}–{BAND_24[1]} MHz")
    for a, b in combinations(pilots, 2):
        if abs(a.freq - b.freq) <= 2 * CH_HALF_BW:
            errors.append(f"{a.name} i {b.name}: kanały za blisko (≤ {2 * CH_HALF_BW} MHz), "
                          "sygnał jednego zaliczy okrążenie drugiemu")
    return errors


def sweep_range(pilots):
    """Tylko kanały dronów: im węższy zakres, tym częściej hackrf_sweep wraca do każdego kanału."""
    freqs = [p.freq for p in pilots]
    return f"{min(freqs) - 2 * CH_HALF_BW}:{max(freqs) + 2 * CH_HALF_BW}"


class RaceScanner(Scanner):
    """Scanner, który po każdej linii hackrf_sweep przekazuje poziomy kanałów do wyścigu."""

    def __init__(self, race, freq, lna, vga):
        self.race = race   # przed super().__init__(): tam startuje wątek odbioru, który od razu wywołuje read()
        super().__init__(freq, lna, vga)

    def read(self, line):
        super().read(line)
        date, clock, lo, hi = line.split(",")[:4]
        # czas samego hackrf_sweep (moment transferu USB), a nie odbioru linii: potok oddaje linie partiami
        t = datetime.strptime(f"{date} {clock.strip()}", "%Y-%m-%d %H:%M:%S.%f").timestamp()
        self.race.feed(float(lo) / 1e6, float(hi) / 1e6, t, self.level)

    def level(self, f):
        """Moc kanału jak w Scanner._peaks: maksimum wygładzonego widma w ±CH_HALF_BW."""
        return max(v for x, v in self.spectrum.items() if abs(x - f) <= CH_HALF_BW)


if __name__ == "__main__":
    tmp = tempfile.TemporaryDirectory()
    race, p = Race(Path(tmp.name)), Pilot("A", 5800, enter=-30, exit=-50)
    race.configure([p], laps=3, min_lap=3)

    def s(t, level):
        race.feed(5795, 5805, t, lambda f: level)

    s(50, -10)                                     # przed startem: poziom widoczny, przeloty się nie liczą
    assert p.level == -10 and p.passes == []
    race.start(100)
    for t, v in [(101, -20), (101.1, -15), (101.2, -25), (101.3, -55),   # holeshot, pik w 101.1
                 (105, -20), (105.1, -60),                               # okrążenie 1: 3.9 s
                 (105.5, -20), (105.6, -60),                             # odbicie po 0.5 s: < min_lap
                 (110, -20), (110.5, -40), (111, -25), (111.5, -10),     # długo przy bramce, -40 powyżej wyjścia:
                 (112, -55),                                             # jeden przelot, pik w 111.5
                 (120, -20), (120.1, -60),                               # okrążenie 3: meta
                 (130, -20), (130.1, -60)]:                              # po mecie się nie liczy
        s(t, v)
    assert p.passes == [101.1, 105, 111.5, 120], p.passes
    r = race.standings(now=200)[0]
    assert r["done"] and r["lap"] == 3 and not race.running
    assert round(r["best"], 6) == 3.9 and round(r["last"], 6) == 8.5 and r["total"] == 20
    saved = json.loads(next(Path(tmp.name).glob("*/A.json")).read_text())   # log: od startu do wykrycia mety
    assert saved["passes"] == [1.1, 5, 11.5, 20] and saved["enter"] == -30
    assert len(saved["samples"]) == 15 and saved["samples"][0] == [1, -20] and saved["samples"][-1] == [20.1, -60]
    tmp.cleanup()

    assert check([Pilot("A", 5800, -30, -30)], 3, False) == ["A: próg wyjścia musi być niższy od progu wejścia"]
    assert len(check([Pilot("A", 5800, -30, -50), Pilot("B", 5809, -30, -50)], 3, False)) == 1
    assert check([Pilot("A", 2500, -30, -50)], 3, True) == ["A: częstotliwość poza 2400–2483 MHz"]
    assert len(check([Pilot("A", 5800, -30, -50), Pilot("A", 5900, -30, -50)], 3, False)) == 1
    assert len(check([Pilot("../A", 5800, -30, -50)], 3, False)) == 1
    assert sweep_range([Pilot("A", 5658, 0, -1), Pilot("B", 5917, 0, -1)]) == "5648:5927"

    sc = RaceScanner.__new__(RaceScanner)            # parsowanie linii hackrf_sweep bez uruchamiania HackRF
    sc.race, sc.spectrum, sc.hold = Race(), {}, {}
    sc.race.configure([q := Pilot("Q", 2460, -30, -50)], 3, 3)
    sc.read("2026-11-08, 14:21:00.250000, 2455000000, 2460000000, 1000000.00, 20, -70, -70, -70, -70, -70")
    sc.read("2026-11-08, 14:21:00.250000, 2460000000, 2465000000, 1000000.00, 20, -12.5, -70, -70, -70, -70")
    assert q.level == -12.5 and q.inside and q.peak_t == datetime(2026, 11, 8, 14, 21, 0, 250000).timestamp()
    print("OK")
