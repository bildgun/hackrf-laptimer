#!/home/bildgun/hackrf/.venv/bin/python
"""Skaner widma HackRF w przeglądarce (NiceGUI).

Uruchomienie: ./gui.py    ->  http://127.0.0.1:8080
Wyjście:      Ctrl+C
"""
from functools import partial

from nicegui import app, ui

from scanner import Scanner, status_text

# Szybkie zakresy w MHz. Szerokość trzymamy jako wielokrotność 20 MHz,
# inaczej hackrf_sweep sam przesunie górną granicę.
PRESETS = {"800–1000": (800, 1000), "2,4 GHz": (2400, 2500), "5,8 GHz FPV": (5300, 5960)}


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
            "lna": 24, "vga": 30, "amp": False,
            "ymin": -75, "ymax": 0,
            "hold": False,
        }
        self.scanner = None
        app.on_startup(self.restart)
        app.on_shutdown(self.stop)

    def restart(self):
        cfg = self.cfg
        if cfg["fmin"] is None or cfg["fmax"] is None or cfg["fmin"] >= cfg["fmax"]:
            ui.notify("„Od” musi być mniejsze niż „Do”", type="warning")
            return
        # Najpierw zwalniamy HackRF, bo nowy hackrf_sweep nie otworzy zajętego urządzenia
        self.stop()
        freq = f"{cfg['fmin']:.0f}:{cfg['fmax']:.0f}"
        self.scanner = Scanner(freq, cfg["lna"], cfg["vga"], cfg["amp"])

    def stop(self):
        if self.scanner:
            self.scanner.close()

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

        chart = ui.echart(chart_options()).classes("w-full h-[70vh]")
        status = ui.label()

        def refresh():
            freqs, levels, held, floor, peaks = self.scanner.snapshot()
            live, max_hold = chart.options["series"]
            live["data"] = [[f, round(level, 1)] for f, level in zip(freqs, levels)]
            if self.cfg["hold"]:
                max_hold["data"] = [[f, round(level, 1)] for f, level in zip(freqs, held) if level is not None]
            else:
                max_hold["data"] = []
            chart.options["yAxis"].update(min=self.cfg["ymin"], max=self.cfg["ymax"])

            code = self.scanner.proc.poll()
            if code is not None:
                status.text = f"hackrf_sweep zakończył działanie (kod {code}), szczegóły w konsoli. Sprawdź: hackrf_info"
            elif floor is not None:
                status.text = status_text(floor, peaks)

        ui.timer(1 / 5, refresh)


if __name__ == "__main__":
    try:
        ui.run(ScannerApp().page, title="Skaner widma", host="127.0.0.1", reload=False, dark=True)
    except KeyboardInterrupt:
        # uvicorn po zatrzymaniu jeszcze raz rzuca Ctrl+C, a HackRF jest wtedy już zamknięty
        pass
