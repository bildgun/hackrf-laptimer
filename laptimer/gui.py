#!/home/bildgun/hackrf/.venv/bin/python
"""Licznik okrążeń dronów po sygnale VTX 5,8 GHz (HackRF) w przeglądarce (NiceGUI).

Uruchomienie: ./gui.py    ->  http://127.0.0.1:8080
Wyjście:      Ctrl+C
"""
import json
import time

from nicegui import app, ui

from race import BAND_24, CHANNELS_58, DEFAULT_FREQS, LOGS, Pilot, Race, RaceScanner, check, sweep_range

THRESHOLDS = {"enter": -35, "exit": -50}   # dB, początkowe progi nowego drona; kalibruje się je według kolumny „Sygnał”
COLUMNS = [{"name": k, "label": label, "field": k, "align": "left"} for k, label in [
    ("name", "Pilot"), ("ch", "Kanał"), ("lap", "Okrążenie"), ("last", "Ostatnie"),
    ("best", "Najlepsze"), ("total", "Czas całkowity"), ("signal", "Sygnał / max [dB]")]]
# krótki sygnał w przeglądarce po każdym zaliczonym przelocie; jeden AudioContext na stronę, bo przeglądarka ogranicza ich liczbę
BEEP = """const a = window.beepCtx ??= new AudioContext(), o = a.createOscillator();
a.resume(); o.frequency.value = 880; o.connect(a.destination); o.start(); o.stop(a.currentTime + 0.15);"""


def fmt(s):
    """Sekundy -> m:ss.ss"""
    if s is None:
        return "—"
    s = round(s, 2)
    return f"{int(s // 60)}:{s % 60:05.2f}"


class LapTimerApp:
    """Ustawienia wyścigu i tablica wyników. Wyścig i skaner jeden na serwer, bo HackRF jest jeden."""

    def __init__(self):
        self.cfg = {"test24": False, "laps": 3, "min_lap": 3, "lna": 24, "vga": 30, "pilots": [
            {"name": f"Dron {i}", "freq": f, **THRESHOLDS} for i, f in enumerate(DEFAULT_FREQS[False][:2], 1)]}
        self.race = Race(LOGS)
        self.scanner = self.sweep = None
        app.on_startup(self.apply)
        app.on_shutdown(lambda: self.race.stop(time.time()))   # Ctrl+C w trakcie wyścigu: logi sygnału i tak się zapiszą
        app.on_shutdown(self.stop_scanner)

    def apply(self):
        """Ustawienia -> wyścig (+ nowy hackrf_sweep, jeśli zmienił się zakres lub wzmocnienie). False = błąd."""
        c = self.cfg
        if self.race.running:
            ui.notify("Najpierw zatrzymaj wyścig", type="warning")
            return False
        if None in [c["laps"], c["min_lap"]] + [p[k] for p in c["pilots"] for k in ("freq", "enter", "exit")]:
            ui.notify("Uzupełnij wszystkie pola", type="warning")
            return False
        pilots = [Pilot(p["name"] or f"Dron {i}", int(p["freq"]), p["enter"], p["exit"])
                  for i, p in enumerate(c["pilots"], 1)]
        if errors := check(pilots, c["laps"], c["test24"]):
            for e in errors:
                ui.notify(e, type="warning")
            return False
        self.race.configure(pilots, int(c["laps"]), c["min_lap"])
        sweep = (sweep_range(pilots), c["lna"], c["vga"])
        # ten sam zakres -> nie ruszamy hackrf_sweep: restart to ~1 s bez danych, a czas wyścigu już biegnie
        if sweep != self.sweep or self.scanner.proc.poll() is not None:
            self.stop_scanner()   # najpierw zwolnić HackRF: nowy hackrf_sweep nie otworzy zajętego urządzenia
            self.scanner, self.sweep = RaceScanner(self.race, *sweep), sweep
        return True

    def start(self):
        if self.apply():   # wyścig zawsze idzie według tego, co jest teraz na ekranie
            self.race.start(time.time())

    def stop_scanner(self):
        if self.scanner:
            self.scanner.close()

    def set_band(self, test24):
        self.cfg["test24"] = test24
        freqs = DEFAULT_FREQS[test24]
        for i, p in enumerate(self.cfg["pilots"]):
            p["freq"] = freqs[i % len(freqs)]   # kanały z innego pasma nie mają tu sensu
        self.pilots_editor.refresh()

    def add_pilot(self):
        pilots, freqs = self.cfg["pilots"], DEFAULT_FREQS[self.cfg["test24"]]
        last = pilots[-1] if pilots else THRESHOLDS   # progi jak u poprzedniego: VTX-y są zwykle podobne
        pilots.append({"name": f"Dron {len(pilots) + 1}", "freq": freqs[len(pilots) % len(freqs)],
                       "enter": last["enter"], "exit": last["exit"]})
        self.pilots_editor.refresh()

    def remove_pilot(self, p):
        self.cfg["pilots"].remove(p)
        self.pilots_editor.refresh()

    @ui.refreshable_method
    def pilots_editor(self):
        for p in self.cfg["pilots"]:
            with ui.row(align_items="end"):
                ui.input("Pilot").bind_value(p, "name")
                if self.cfg["test24"]:
                    ui.number("MHz", min=BAND_24[0], max=BAND_24[1], precision=0).bind_value(p, "freq") \
                        .tooltip("TX16S, protokół CW: 2400 + RF Chan.")
                else:
                    ui.select(CHANNELS_58, label="Kanał", with_input=True).bind_value(p, "freq").classes("w-36")
                ui.number("Wejście [dB]").bind_value(p, "enter").tooltip("sygnał powyżej = dron w bramce")
                ui.number("Wyjście [dB]").bind_value(p, "exit") \
                    .tooltip("sygnał poniżej = dron odleciał; dopiero wtedy może się zaliczyć kolejny przelot")
                ui.button(icon="delete", on_click=lambda p=p: self.remove_pilot(p)).props("flat round")

    def page(self):
        ui.add_css(".board td, .board th { font-size: 1.5rem !important; }")
        ui.number.default_classes("w-28")
        ui.select.default_classes("w-28")
        with ui.row(align_items="center"):
            ui.button("Start", icon="play_arrow", color="positive", on_click=self.start)
            ui.button("Stop", icon="stop", color="negative", on_click=lambda: self.race.stop(time.time()))
            status = ui.label().classes("text-xl")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full board")
        with ui.expansion("Ustawienia", icon="settings", value=True).classes("w-full"):
            with ui.row(align_items="end"):
                ui.switch("Test 2,4 GHz", value=self.cfg["test24"], on_change=lambda e: self.set_band(e.value))
                ui.number("Okrążenia", min=1, precision=0).bind_value(self.cfg, "laps")
                ui.number("Min. okrążenie [s]", min=0).bind_value(self.cfg, "min_lap") \
                    .tooltip("krótszy przelot po poprzednim = odbicie sygnału, nie okrążenie")
                ui.select(list(range(0, 41, 8)), label="LNA [dB]").bind_value(self.cfg, "lna")
                ui.select(list(range(0, 63, 2)), label="VGA [dB]").bind_value(self.cfg, "vga")
            self.pilots_editor()
            with ui.row():
                ui.button("Dodaj drona", icon="add", on_click=self.add_pilot).props("outline")
                ui.button("Zastosuj", on_click=self.apply).tooltip("nowe ustawienia bez startu, np. do kalibracji progów")
        with ui.expansion("Sygnał po wyścigu", icon="show_chart").classes("w-full"):
            with ui.row():
                # nowy wyścig = od nowa wybór drona (value=None): ta sama nazwa w innym wyścigu to inny plik
                race_sel = ui.select([], label="Wyścig", on_change=lambda e: drone_sel.set_options(
                    sorted(f.stem for f in (LOGS / e.value).glob("*.json")) if e.value else [], value=None)).classes("w-56")
                drone_sel = ui.select([], label="Dron", on_change=lambda e: show_log(e.value)).classes("w-56")
            # wygląd jak w skanerze widma (~/hackrf/gui.py), tylko oś X to czas od startu
            axis = {"axisLine": {"onZero": False, "lineStyle": {"color": "#888"}}, "axisLabel": {"color": "#aaa"},
                    "splitLine": {"lineStyle": {"color": "#333", "type": "dashed"}}}
            chart = ui.echart({
                "animation": False,
                "backgroundColor": "#000",
                "textStyle": {"color": "#aaa"},
                "tooltip": {"trigger": "axis", "backgroundColor": "#111", "borderColor": "#444", "textStyle": {"color": "#ddd"}},
                "dataZoom": [{"type": "inside"}, {"type": "slider"}],   # przelot trwa ułamek sekundy: kółko myszy / suwak przybliża
                "xAxis": {**axis, "type": "value", "name": "s", "min": "dataMin", "max": "dataMax"},
                "yAxis": {**axis, "type": "value", "name": "dB", "scale": True},
                "series": [{"type": "line", "showSymbol": False, "sampling": "lttb", "color": "#0f0", "lineStyle": {"width": 1},
                            "data": [], "markLine": {"symbol": "none", "lineStyle": {"color": "#ff0"}, "label": {"color": "#ff0"},
                                                     "data": []}}],
            }).classes("w-full h-[60vh]")

        def show_log(drone):
            """Sygnał drona w czasie; żółte linie: poziome = progi wejścia i wyjścia, pionowe = zaliczone przeloty."""
            if drone:
                log = json.loads((LOGS / race_sel.value / f"{drone}.json").read_text())
                series = chart.options["series"][0]
                series["data"] = log["samples"]
                series["markLine"]["data"] = [{"yAxis": log["enter"]}, {"yAxis": log["exit"]}] + [{"xAxis": t} for t in log["passes"]]

        def list_races():
            races = sorted((d.name for d in LOGS.glob("*")), reverse=True)   # najnowszy na górze
            if races != race_sel.options:
                race_sel.set_options(races)

        passes = 0   # suma przelotów przy poprzednim odświeżeniu: wzrost = dźwięk

        def refresh():
            nonlocal passes
            now, r = time.time(), self.race
            rows = r.standings(now)
            if (n := sum(s["passes"] for s in rows)) > passes:
                ui.run_javascript(BEEP)
            passes = n
            table.rows = [{
                "id": i, "name": s["name"], "ch": CHANNELS_58.get(s["freq"], f"{s['freq']} MHz"),
                "lap": "—" if s["total"] is None else "meta ✓" if s["done"] else f"{s['lap']}/{r.laps}",
                "last": fmt(s["last"]), "best": fmt(s["best"]), "total": fmt(s["total"]),
                "signal": "—" if s["level"] is None else f"{s['level']:.0f} / {s['max_level']:.0f}",
            } for i, s in enumerate(rows)]
            if (code := self.scanner.proc.poll()) is not None:
                status.text = f"hackrf_sweep zakończył działanie (kod {code}), szczegóły w konsoli. Sprawdź: hackrf_info"
            elif r.start_t is None:
                status.text = "Kalibracja: przeleć przez bramkę i ustaw progi według kolumny Sygnał"
            elif r.running:
                status.text = f"Wyścig: {fmt(now - r.start_t)}"
            else:
                status.text = f"Koniec: {fmt(r.stop_t - r.start_t)}"

        ui.timer(1 / 10, refresh)
        ui.timer(2, list_races)   # nowy wyścig pojawia się na liście po zapisaniu logów


if __name__ == "__main__":
    try:
        ui.run(LapTimerApp().page, title="Licznik okrążeń", host="127.0.0.1", reload=False, dark=True)
    except KeyboardInterrupt:   # uvicorn po zatrzymaniu ponownie zgłasza Ctrl+C; HackRF jest już wtedy zamknięty
        pass
