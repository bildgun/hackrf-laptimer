#!/home/bildgun/hackrf/.venv/bin/python
"""Skaner widma HackRF w przeglądarce (NiceGUI).

Uruchomienie: ./gui.py    ->  http://127.0.0.1:8080
Wyjście:      Ctrl+C
"""
from nicegui import app, ui

from scanner import Scanner

# szybkie zakresy, MHz (szerokość jest wielokrotnością 20: inaczej hackrf_sweep rozszerzy górną granicę)
PRESETS = {"800–1000": (800, 1000), "2,4 GHz": (2400, 2500), "5,8 GHz FPV": (5300, 5960)}


class ScannerApp:
    """Ustawienia odbioru i wykres na jednej stronie. Skaner jeden na serwer, bo HackRF jest jeden."""

    def __init__(self):
        self.cfg = {"fmin": 800, "fmax": 1000, "lna": 24, "vga": 30, "amp": False, "ymin": -75, "ymax": 0, "hold": False}
        self.scanner = None
        app.on_startup(self.restart)
        app.on_shutdown(self.stop)

    def restart(self):
        c = self.cfg
        if c["fmin"] is None or c["fmax"] is None or c["fmin"] >= c["fmax"]:
            ui.notify("„Od” musi być mniejsze niż „Do”", type="warning")
            return
        self.stop()   # najpierw zwolnić HackRF: nowy hackrf_sweep nie otworzy zajętego urządzenia
        self.scanner = Scanner(f"{c['fmin']:.0f}:{c['fmax']:.0f}", c["lna"], c["vga"], c["amp"])

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
                # wartości domyślne utrwalają fmin/fmax tej iteracji; NiceGUI ich nie nadpisuje
                ui.button(name, on_click=lambda fmin=fmin, fmax=fmax: self.preset(fmin, fmax)).props("outline no-caps")
        with ui.row(align_items="end"):
            ui.number("Od [MHz]", min=1, max=6000, precision=0).bind_value(self.cfg, "fmin")
            ui.number("Do [MHz]", min=1, max=6000, precision=0).bind_value(self.cfg, "fmax")
            ui.select(list(range(0, 41, 8)), label="LNA [dB]").bind_value(self.cfg, "lna")
            ui.select(list(range(0, 63, 2)), label="VGA [dB]").bind_value(self.cfg, "vga")
            ui.switch("AMP").bind_value(self.cfg, "amp").tooltip("+14 dB, nie włączaj w pobliżu nadajnika")
            ui.button("Zastosuj", on_click=self.restart)
            ui.number("Y min [dB]").bind_value(self.cfg, "ymin")
            ui.number("Y max [dB]").bind_value(self.cfg, "ymax")
            # każde przełączenie zaczyna gromadzenie od nowa: wył./wł. = reset
            ui.switch("Max-hold", on_change=lambda: self.scanner.clear_hold()).bind_value(self.cfg, "hold") \
                .tooltip("maksimum od włączenia; wyłącz i włącz, aby wyczyścić")
        # wygląd analizatora widma: czarne tło, przerywana siatka, cienki ślad bez wypełnienia
        # (ECharts 6 ustawia kolor etykiet osi w motywie, więc globalny textStyle go nie zmienia)
        axis = {"axisLine": {"onZero": False, "lineStyle": {"color": "#888"}}, "axisLabel": {"color": "#aaa"},
                "splitLine": {"lineStyle": {"color": "#333", "type": "dashed"}}}
        chart = ui.echart({
            "animation": False,
            "backgroundColor": "#000",
            "textStyle": {"color": "#aaa"},
            "tooltip": {"trigger": "axis", "backgroundColor": "#111", "borderColor": "#444", "textStyle": {"color": "#ddd"}},
            "legend": {"textStyle": {"color": "#aaa"}},
            "xAxis": {**axis, "type": "value", "name": "MHz", "min": "dataMin", "max": "dataMax", "splitNumber": 10},
            "yAxis": {**axis, "type": "value", "name": "dB", "interval": 10},   # 10 dB na działkę, jak w rx.py
            "series": [
                {"name": "Na żywo", "type": "line", "showSymbol": False, "color": "#0f0", "lineStyle": {"width": 1}, "data": []},
                {"name": "Max-hold", "type": "line", "showSymbol": False, "color": "#ff0", "lineStyle": {"width": 1}, "data": []},
            ],
        }).classes("w-full h-[70vh]")
        status = ui.label()

        def refresh():
            xs, ys, hs, floor, groups = self.scanner.snapshot()
            chart.options["series"][0]["data"] = [[x, round(y, 1)] for x, y in zip(xs, ys)]
            chart.options["series"][1]["data"] = [[x, round(h, 1)] for x, h in zip(xs, hs)
                                                  if self.cfg["hold"] and h is not None]
            chart.options["yAxis"].update(min=self.cfg["ymin"], max=self.cfg["ymax"])
            if (code := self.scanner.proc.poll()) is not None:
                status.text = f"hackrf_sweep zakończył działanie (kod {code}), szczegóły w konsoli. Sprawdź: hackrf_info"
            elif floor is not None:
                top = " | ".join(f"{'/'.join(n)} {f} MHz {p:.0f} dB" for f, p, n in groups[:5]) or "brak"
                status.text = f"Szum: {floor:.0f} dB   Najsilniejsze: {top}"

        ui.timer(1 / 5, refresh)


if __name__ == "__main__":
    try:
        ui.run(ScannerApp().page, title="Skaner widma", host="127.0.0.1", reload=False, dark=True)
    except KeyboardInterrupt:   # uvicorn po zatrzymaniu ponownie zgłasza Ctrl+C; HackRF jest już wtedy zamknięty
        pass
