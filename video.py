"""Odbiór analogowego wideo FPV z HackRF przez hackrf_transfer. Same dane, bez interfejsu.

Sposób jak na https://pysdr.org/content/fpv_video: demodulacja FM, a z otrzymanego sygnału wideo
wycinamy kolejne linie obrazu, każdą od jej impulsu synchronizacji. Obraz jest czarno-biały.
Kamera może być PAL albo NTSC: długość linii i półobrazu wynika z samego sygnału.
"""
import fcntl
import itertools
import struct
import subprocess
import threading
import time
import zlib

import numpy as np

# Hz, tyle co w nagraniu z PySDR. Sygnał FPV odchyla się od kanału o około ±3 MHz, więc się mieści,
# a 20 MHz to dwa razy więcej pracy dla procesora przy demodulacji.
SAMPLE_RATE = 10_000_000

# Czasy w linii w mikrosekundach, od początku impulsu synchronizacji.
# PAL i NTSC różnią się tu o ułamki mikrosekundy, więc wystarczają wspólne wartości.
SYNC_LEVEL_AT = 2     # środek impulsu synchronizacji (trwa 4,7 µs)
BLACK_LEVEL_AT = 8.7  # za impulsem i błyskiem koloru, jeszcze przed obrazem: tu sygnał ma poziom czerni
ACTIVE = (9.5, 62)    # widoczna część linii

# Liczone od początku synchronizacji pionowej: pierwsze linie półobrazu są puste (wygaszanie),
# a ostatnie to już impulsy przed następną synchronizacją
TOP_LINES = 18
BOTTOM_LINES = 3


def find_pulses(video, us):
    """Impulsy synchronizacji w zdemodulowanym sygnale: (początki linii, początki impulsów pionowych).

    us to liczba próbek na mikrosekundę.
    """
    # Synchronizacja to najniższy poziom w sygnale wideo, próg leży między nią a czernią
    tip, top = np.percentile(video[::16], [1, 99])
    low = video < tip + 0.15 * (top - tip)
    starts = np.flatnonzero(low[1:] & ~low[:-1]) + 1
    ends = np.flatnonzero(~low[1:] & low[:-1]) + 1
    ends = ends[ends > starts[0]] if len(starts) else ends[:0]
    starts = starts[:len(ends)]
    widths = (ends - starts) / us

    lines = starts[(widths > 3) & (widths < 8)]  # 4,7 µs; krótsze to impulsy wyrównujące wokół synchronizacji pionowej
    broad = starts[widths > 15]                  # około 27 µs, kilka takich co pół linii to synchronizacja pionowa
    return lines, broad


class FieldDecoder:
    """Wyciąga z próbek IQ jeden półobraz (pole), czyli obraz między dwiema synchronizacjami pionowymi."""

    def __init__(self, sample_rate):
        self.us = sample_rate / 1e6
        # Średnia z kilku sąsiednich próbek tłumi szum i podnośną dźwięku (5,5–6,5 MHz)
        taps = max(1, round(0.15 * self.us))
        self.kernel = np.ones(taps, np.float32) / taps
        self.sign = 1  # -1, jeśli nadajnik moduluje odwrotnie i synchronizacja wychodzi na górze

    def decode(self, iq):
        """Ostatni pełny półobraz w tych próbkach (uint8) albo None.

        Próbek musi być na co najmniej 40 ms: wtedy zawsze zmieszczą się dwie synchronizacje pionowe.
        """
        # Demodulacja FM: obrót fazy między sąsiednimi próbkami to chwilowa częstotliwość
        video = np.angle(iq[1:] * iq[:-1].conj())
        video = self.sign * np.convolve(video, self.kernel, "same")

        lines, broad = find_pulses(video, self.us)
        # Początek synchronizacji pionowej to pierwszy impuls z serii, następne są co pół linii
        fields = broad[np.diff(broad, prepend=-np.inf) > 1000 * self.us]
        if len(fields) < 2:
            # Albo nie ma sygnału, albo nadajnik moduluje odwrotnie. W obu przypadkach odwrócenie nic nie psuje.
            self.sign = -self.sign
            return None

        start, end = fields[-2], fields[-1]
        return self.render(video, start, end, lines[(lines > start) & (lines < end)])

    def render(self, video, start, end, lines):
        if len(lines) < 100:
            return None  # zbyt zaszumiony półobraz
        us = self.us
        line_length = np.median(np.diff(lines))
        # PAL ma 312,5 linii w półobrazie, a NTSC 262,5. Inna liczba to zakłócenia albo zgubiona synchronizacja pionowa.
        if not 250 < (end - start) / line_length < 320:
            return None
        # Numer linii od początku synchronizacji pionowej. W co drugim półobrazie linie są przesunięte
        # o pół linii względem synchronizacji, a +0.25 daje w obu przypadkach te same numery.
        row = np.floor((lines - start) / line_length + 0.25).astype(int) - TOP_LINES
        height = int((end - start) / line_length) - TOP_LINES - BOTTOM_LINES
        visible = (row >= 0) & (row < height)
        lines, row = lines[visible], row[visible]

        # Poziomy odniesienia bierzemy z samego sygnału, bo zależą od dewiacji nadajnika i odstrojenia.
        # Biel leży nad czernią 2,5 raza dalej niż synchronizacja pod nią (NTSC: 100 i 40 IRE).
        # W PAL to 2,33 raza, więc biel wyjdzie tam odrobinę ciemniejsza, ale w żadnym systemie się nie obetnie.
        sync = np.median(video[lines + int(SYNC_LEVEL_AT * us)])
        black = np.median(video[lines + int(BLACK_LEVEL_AT * us)])
        white = black + (black - sync) * 2.5

        # Półobraz ma co drugą linię pełnego obrazu, więc każdą pokażemy dwa razy. Szerokość dobieramy do 4:3.
        width = round(2 * height * 4 / 3)
        columns = np.linspace(ACTIVE[0] * us, ACTIVE[1] * us, width).astype(int)
        picture = np.full((height, width), black, np.float32)  # linia bez synchronizacji zostaje czarna
        picture[row] = video[lines[:, None] + columns]
        picture = np.clip((picture - black) / (white - black), 0, 1)
        return np.repeat((picture * 255).astype(np.uint8), 2, axis=0)


def png(image):
    """Obraz w odcieniach szarości (tablica uint8) jako plik PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    height, width = image.shape
    # Każdy wiersz PNG zaczyna się bajtem filtra, 0 to brak filtra
    rows = np.hstack((np.zeros((height, 1), np.uint8), image))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8 bitów, odcienie szarości
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows.tobytes(), 1)) + chunk(b"IEND", b""))


class VideoReceiver:
    """Uruchamia hackrf_transfer na kanale drona, a wątek w tle na bieżąco składa z sygnału obraz."""

    def __init__(self, freq, lna=24, vga=8, amp=False):
        cmd = ["hackrf_transfer", "-r", "-", "-f", str(int(freq * 1_000_000)), "-s", str(SAMPLE_RATE),
               "-l", str(lna), "-g", str(vga), "-a", str(int(amp))]
        # stderr zostaje w konsoli, jak w Scanner: błędy hackrf_transfer widać od razu.
        # Co sekundę pojawia się tam też krótka statystyka odbioru.
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        # Potok mieści domyślnie 64 kB, czyli ułamek milisekundy sygnału. Gdy wątek odbioru dekoduje obraz,
        # hackrf_transfer musiałby czekać, a HackRF gubiłby wtedy próbki. 1 MB to około 50 ms zapasu.
        fcntl.fcntl(self.proc.stdout, fcntl.F_SETPIPE_SZ, 1 << 20)
        self.decoder = FieldDecoder(SAMPLE_RATE)
        self.image = None       # ostatni półobraz, None dopóki nie złapiemy synchronizacji
        self.image_time = None  # time.monotonic() ostatniego półobrazu
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        chunk = 2 * SAMPLE_RATE // 25  # 40 ms; każda próbka to dwa bajty ze znakiem: I i Q
        for number in itertools.count():
            data = self.proc.stdout.read(chunk)
            if len(data) < chunk:
                break  # hackrf_transfer się zakończył, a resztkę danych można pominąć
            if number % 2:
                # Co drugi kawałek pomijamy. 12,5 obrazu na sekundę wystarcza do podglądu,
                # a procesor zostaje dla reszty programu, np. licznika okrążeń.
                continue
            iq = np.frombuffer(data, np.int8).astype(np.float32).view(np.complex64)
            image = self.decoder.decode(iq)
            if image is not None:
                self.image = image
                self.image_time = time.monotonic()

    def close(self):
        self.proc.terminate()
        self.proc.wait()
