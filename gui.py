#!/home/bildgun/hackrf/.venv/bin/python
"""Skaner widma HackRF w przeglądarce (NiceGUI).

Uruchomienie: ./gui.py    ->  http://127.0.0.1:8080
Wyjście:      Ctrl+C
"""
import json
from datetime import datetime
from functools import partial
from pathlib import Path

from nicegui import app, ui

from scanner import CsvRecorder, Scanner, status_text

RECORDINGS = Path(__file__).resolve().parent / "recordings"  # recordings/<data i godzina startu>.csv

# Szybkie zakresy w MHz. Szerokość trzymamy jako wielokrotność 20 MHz,
# inaczej hackrf_sweep sam przesunie górną granicę.
PRESETS = {"800–1000": (800, 1000), "2,4 GHz": (2400, 2500), "5,8 GHz FPV": (5300, 5960)}

# Marginesy wykresu w pikselach. Spektrogram pod nim ma takie same, żeby częstotliwości wypadały jedna pod drugą.
MARGIN_LEFT = 50
MARGIN_RIGHT = 50
SPECTROGRAM_ROWS = 150  # przy odświeżaniu co 0,2 s to 30 sekund historii

# Spektrogram rysuje przeglądarka. Przy każdym odświeżeniu dostaje tylko nowy wiersz poziomów:
# przesuwa dotychczasowy obraz o piksel w dół i dorysowuje ten wiersz na górze.
SPECTROGRAM_JS = """
<script>
function spectrogramRow(id, start, levels, ymin, ymax) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  // Inny zakres częstotliwości: stara historia nie pasuje do osi, więc zaczynamy od czystego obrazu
  if (canvas.width !== levels.length || canvas.dataset.start !== String(start)) {
    canvas.width = levels.length;
    canvas.dataset.start = start;
  }
  const ctx = canvas.getContext("2d");
  ctx.drawImage(canvas, 0, 1);
  levels.forEach((level, x) => {
    const t = Math.min(Math.max((level - ymin) / (ymax - ymin), 0), 1);
    ctx.fillStyle = `hsl(120, 100%, ${80 * t}%)`;  // od czerni przez zieleń do prawie bieli
    ctx.fillRect(x, 0, 1, 1);
  });
}
</script>
"""


def chart_options():
    """Wygląd analizatora widma: czarne tło, przerywana siatka i cienki ślad bez wypełnienia."""
    # Kolor etykiet osi trzeba ustawić przy każdej osi osobno:
    # ECharts 6 bierze go z motywu, więc globalny textStyle nic tu nie zmienia.
    axis = {
        "axisLine": {"onZero": False, "lineStyle": {"color": "#888"}},
        "axisLabel": {"color": "#aaa"},
        "splitLine": {"lineStyle": {"color": "#333", "type": "dashed"}},
    }
    return {
        "animation": False,
        "backgroundColor": "#000",
        "textStyle": {"color": "#aaa"},
        "tooltip": {"trigger": "axis", "backgroundColor": "#111", "borderColor": "#444", "textStyle": {"color": "#ddd"}},
        "legend": {"textStyle": {"color": "#aaa"}},
        "grid": {"left": MARGIN_LEFT, "right": MARGIN_RIGHT},
        "xAxis": {**axis, "type": "value", "name": "MHz", "min": "dataMin", "max": "dataMax", "splitNumber": 10},
        "yAxis": {**axis, "type": "value", "name": "dB", "interval": 10},
        "series": [
            {"name": "Na żywo", "type": "line", "showSymbol": False, "color": "#0f0", "lineStyle": {"width": 1}, "data": []},
            {"name": "Max-hold", "type": "line", "showSymbol": False, "color": "#ff0", "lineStyle": {"width": 1}, "data": []},
        ],
    }


class ScannerApp:
    """Ustawienia odbioru i wykres na jednej stronie.

    Skaner jest jeden na cały serwer, bo HackRF też jest jeden.
    """

    def __init__(self):
        self.cfg = {
            "fmin": 800, "fmax": 1000,
            "lna": 24, "vga": 8, "amp": False,
            "ymin": -75, "ymax": 0,
            "hold": False,
            "recording": False,
        }
        self.scanner = None
        self.recorder = None  # CsvRecorder trwającego albo ostatniego zapisu do pliku
        app.on_startup(self.restart)
        app.on_shutdown(self.stop)
        app.on_shutdown(self.stop_recording)

    def restart(self):
        cfg = self.cfg
        if cfg["fmin"] is None or cfg["fmax"] is None or cfg["fmin"] >= cfg["fmax"]:
            ui.notify("„Od” musi być mniejsze niż „Do”", type="warning")
            return
        # Najpierw zwalniamy HackRF, bo nowy hackrf_sweep nie otworzy zajętego urządzenia
        self.stop()
        freq = f"{cfg['fmin']:.0f}:{cfg['fmax']:.0f}"
        self.scanner = Scanner(freq, cfg["lna"], cfg["vga"], cfg["amp"])
        # Zapis trwa dalej w tym samym pliku, także po zmianie zakresu: każdy wiersz ma swoją częstotliwość
        self.scanner.recorder = self.recorder

    def stop(self):
        if self.scanner:
            self.scanner.close()

    def set_recording(self, on):
        # Przy kilku otwartych kartach wywołuje się w każdej z nich, więc działa tylko przy faktycznej zmianie
        recording = self.recorder is not None and not self.recorder.closed
        if on and not recording:
            RECORDINGS.mkdir(exist_ok=True)
            name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".csv"
            self.recorder = CsvRecorder(RECORDINGS / name)
            self.scanner.recorder = self.recorder
        elif not on and recording:
            self.recorder.close()

    def stop_recording(self):
        if self.recorder:
            self.recorder.close()

    def preset(self, fmin, fmax):
        self.cfg.update(fmin=fmin, fmax=fmax)
        self.restart()

    def page(self):
        ui.number.default_classes("w-24")
        ui.select.default_classes("w-24")

        with ui.row():
            for name, (fmin, fmax) in PRESETS.items():
                ui.button(name, on_click=partial(self.preset, fmin, fmax)).props("outline no-caps")

        with ui.row(align_items="end"):
            ui.number("Od [MHz]", min=1, max=6000, precision=0).bind_value(self.cfg, "fmin")
            ui.number("Do [MHz]", min=1, max=6000, precision=0).bind_value(self.cfg, "fmax")
            ui.select(list(range(0, 41, 8)), label="LNA [dB]").bind_value(self.cfg, "lna")
            ui.select(list(range(0, 63, 2)), label="VGA [dB]").bind_value(self.cfg, "vga")
            ui.switch("AMP").bind_value(self.cfg, "amp").tooltip("+14 dB, nie włączaj w pobliżu nadajnika")
            ui.button("Zastosuj", on_click=self.restart)
            ui.number("Y min [dB]").bind_value(self.cfg, "ymin")
            ui.number("Y max [dB]").bind_value(self.cfg, "ymax")
            # Każde przełączenie zbiera maksimum od nowa, więc wyłączenie i włączenie działa jak reset
            hold = ui.switch("Max-hold", on_change=lambda: self.scanner.clear_hold())
            hold.bind_value(self.cfg, "hold")
            hold.tooltip("maksimum od włączenia; wyłącz i włącz, aby wyczyścić")
            recording = ui.switch("Zapis CSV", on_change=lambda e: self.set_recording(e.value))
            recording.bind_value(self.cfg, "recording")
            recording.tooltip("czas, MHz i dB każdego odczytu z hackrf_sweep, do pliku recordings/<data_godzina>.csv")

        chart = ui.echart(chart_options()).classes("w-full h-[45vh]")
        ui.add_head_html(SPECTROGRAM_JS)
        spectrogram = ui.element("canvas").props(f"height={SPECTROGRAM_ROWS}")
        spectrogram.style(f"width: calc(100% - {MARGIN_LEFT + MARGIN_RIGHT}px); height: 25vh; "
                          f"margin-left: {MARGIN_LEFT}px; background: #000; image-rendering: pixelated")
        ui.label("Spektrogram: najnowsze u góry, kolor od czerni (Y min) do jasnej zieleni (Y max)").classes(
            "text-xs text-gray-400")
        status = ui.label()
        recording_status = ui.label()

        def refresh():
            freqs, levels, held, floor, peaks = self.scanner.snapshot()
            live, max_hold = chart.options["series"]
            live["data"] = [[f, round(level, 1)] for f, level in zip(freqs, levels)]
            if self.cfg["hold"]:
                max_hold["data"] = [[f, round(level, 1)] for f, level in zip(freqs, held) if level is not None]
            else:
                max_hold["data"] = []
            chart.options["yAxis"].update(min=self.cfg["ymin"], max=self.cfg["ymax"])

            if levels and None not in (self.cfg["ymin"], self.cfg["ymax"]):
                row = [round(level) for level in levels]
                args = json.dumps([spectrogram.html_id, freqs[0], row, self.cfg["ymin"], self.cfg["ymax"]])
                ui.run_javascript(f"spectrogramRow(...{args})")

            code = self.scanner.proc.poll()
            if code is not None:
                status.text = f"hackrf_sweep zakończył działanie (kod {code}), szczegóły w konsoli. Sprawdź: hackrf_info"
            elif floor is not None:
                status.text = status_text(floor, peaks)

            # Ścieżka zostaje widoczna także po zakończeniu zapisu, żeby było wiadomo, gdzie szukać pliku
            recorder = self.recorder
            if recorder:
                if recorder.error:
                    label = f"Zapis przerwany ({recorder.error})"
                elif recorder.closed:
                    label = "Zapisano"
                else:
                    label = "Zapis"
                size = recorder.path.stat().st_size / 1e6
                recording_status.text = f"{label}: {recorder.path} ({size:.1f} MB)"

        ui.timer(1 / 5, refresh)


if __name__ == "__main__":
    try:
        ui.run(ScannerApp().page, title="Skaner widma", host="127.0.0.1", reload=False, dark=True)
    except KeyboardInterrupt:
        # uvicorn po zatrzymaniu jeszcze raz rzuca Ctrl+C, a HackRF jest wtedy już zamknięty
        pass
