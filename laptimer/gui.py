#!/home/bildgun/hackrf/.venv/bin/python
"""Licznik okrążeń dronów po sygnale VTX 5,8 GHz (HackRF) w przeglądarce (NiceGUI).

Uruchomienie: ./gui.py    ->  http://127.0.0.1:8080
Wyjście:      Ctrl+C
"""
import json
import time
from functools import partial

from fastapi import Response
from nicegui import app, ui

from race import (BAND_24, CHANNELS_58, DEFAULT_FREQS_24, DEFAULT_FREQS_58, LOGS, Pilot, Race, RaceScanner,
                  check, sweep_range)
from video import VideoReceiver, png

# Startowe progi nowego drona w dB. Właściwe ustawia się przy kalibracji, patrząc na kolumnę „Sygnał”.
THRESHOLDS = {"enter": -35, "exit": -50}

COLUMNS = [
    {"name": key, "label": label, "field": key, "align": "left"}
    for key, label in [
        ("name", "Pilot"),
        ("ch", "Kanał"),
        ("lap", "Okrążenie"),
        ("last", "Ostatnie"),
        ("best", "Najlepsze"),
        ("total", "Czas całkowity"),
        ("signal", "Sygnał / max [dB]"),
    ]
]

# Krótki dźwięk w przeglądarce po każdym zaliczonym przelocie.
# AudioContext jest jeden na stronę, bo przeglądarki ograniczają ich liczbę.
BEEP = """
const ctx = window.beepCtx ??= new AudioContext();
const osc = ctx.createOscillator();
ctx.resume();
osc.frequency.value = 880;
osc.connect(ctx.destination);
osc.start();
osc.stop(ctx.currentTime + 0.15);
"""


def format_time(seconds):
    """Sekundy jako m:ss.ss, a brak czasu jako kreska."""
    if seconds is None:
        return "—"
    seconds = round(seconds, 2)
    minutes = int(seconds // 60)
    return f"{minutes}:{seconds % 60:05.2f}"


def table_row(index, row, laps):
    """Wiersz tablicy wyników na podstawie wiersza z Race.standings()."""
    if row["total"] is None:
        lap = "—"
    elif row["done"]:
        lap = "meta ✓"
    else:
        lap = f"{row['lap']}/{laps}"

    if row["level"] is None:
        signal = "—"
    else:
        signal = f"{row['level']:.0f} / {row['max_level']:.0f}"

    return {
        "id": index,
        "name": row["name"],
        "ch": CHANNELS_58.get(row["freq"], f"{row['freq']} MHz"),
        "lap": lap,
        "last": format_time(row["last"]),
        "best": format_time(row["best"]),
        "total": format_time(row["total"]),
        "signal": signal,
    }


def log_chart_options():
    """Wykres jak w skanerze widma (~/hackrf/gui.py), tylko na osi X jest czas od startu."""
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
        # Przelot trwa ułamek sekundy, więc trzeba móc przybliżać kółkiem myszy albo suwakiem
        "dataZoom": [{"type": "inside"}, {"type": "slider"}],
        "xAxis": {**axis, "type": "value", "name": "s", "min": "dataMin", "max": "dataMax"},
        "yAxis": {**axis, "type": "value", "name": "dB", "scale": True},
        "series": [{
            "type": "line",
            "showSymbol": False,
            "sampling": "lttb",
            "color": "#0f0",
            "lineStyle": {"width": 1},
            "data": [],
            "markLine": {"symbol": "none", "lineStyle": {"color": "#ff0"}, "label": {"color": "#ff0"}, "data": []},
        }],
    }


class LapTimerApp:
    """Ustawienia wyścigu i tablica wyników.

    Wyścig i skaner są jedne na cały serwer, bo HackRF też jest jeden.
    """

    def __init__(self):
        self.cfg = {
            "test24": False,
            "laps": 3,
            "min_lap": 3,
            "lna": 24,
            "vga": 8,
            "pilots": [
                {"name": "Dron 1", "freq": DEFAULT_FREQS_58[0], **THRESHOLDS},
                {"name": "Dron 2", "freq": DEFAULT_FREQS_58[1], **THRESHOLDS},
            ],
        }
        self.race = Race(LOGS)
        self.scanner = None
        self.sweep = None  # (zakres, LNA, VGA), z którymi działa teraz hackrf_sweep
        # Wideo z jednego drona. HackRF obsługuje naraz tylko jeden program, więc na ten czas
        # hackrf_sweep jest zatrzymany i sygnał dronów nie jest mierzony.
        self.video = None
        self.video_title = ""
        app.on_startup(self.apply)
        # Ctrl+C w trakcie wyścigu też go zatrzymuje, żeby logi sygnału się zapisały
        app.on_shutdown(self.stop_race)
        app.on_shutdown(self.stop_scanner)
        app.on_shutdown(self.stop_video)
        app.add_api_route("/video.png", self.video_png)

    def default_freqs(self):
        return DEFAULT_FREQS_24 if self.cfg["test24"] else DEFAULT_FREQS_58

    def apply(self):
        """Przenosi ustawienia z ekranu do wyścigu. Zwraca False, jeśli coś się nie zgadza.

        hackrf_sweep jest restartowany tylko wtedy, gdy zmienił się zakres albo wzmocnienie.
        """
        cfg = self.cfg
        if self.race.running:
            ui.notify("Najpierw zatrzymaj wyścig", type="warning")
            return False

        fields = [cfg["laps"], cfg["min_lap"]]
        for pilot in cfg["pilots"]:
            fields += [pilot["freq"], pilot["enter"], pilot["exit"]]
        if None in fields:
            ui.notify("Uzupełnij wszystkie pola", type="warning")
            return False

        pilots = []
        for number, pilot in enumerate(cfg["pilots"], 1):
            name = pilot["name"] or f"Dron {number}"
            pilots.append(Pilot(name, int(pilot["freq"]), pilot["enter"], pilot["exit"]))

        errors = check(pilots, cfg["laps"], cfg["test24"])
        if errors:
            for error in errors:
                ui.notify(error, type="warning")
            return False

        self.race.configure(pilots, int(cfg["laps"]), cfg["min_lap"])

        sweep = (sweep_range(pilots), cfg["lna"], cfg["vga"])
        # Przy tym samym zakresie hackrf_sweep zostaje: restart to około sekundy bez danych,
        # a czas wyścigu już biegnie
        if self.video or sweep != self.sweep or self.scanner.proc.poll() is not None:
            # Nowy hackrf_sweep nie otworzy HackRF, dopóki trzyma go stary albo wideo
            self.stop_video()
            self.stop_scanner()
            self.scanner = RaceScanner(self.race, *sweep)
            self.sweep = sweep
        return True

    def start(self):
        # Wyścig zawsze rusza z tymi ustawieniami, które są teraz na ekranie
        if self.apply():
            self.race.start(time.time())

    def stop_race(self):
        self.race.stop(time.time())

    def stop_scanner(self):
        if self.scanner:
            self.scanner.close()

    def show_video(self, pilot):
        """Przełącza HackRF z pomiaru sygnału na wideo z kanału tego drona."""
        if self.race.running:
            ui.notify("Wideo wstrzymuje pomiar sygnału, więc w trakcie wyścigu jest niedostępne", type="warning")
            return
        self.stop_video()
        self.stop_scanner()
        self.video = VideoReceiver(pilot["freq"], self.cfg["lna"], self.cfg["vga"])
        channel = CHANNELS_58.get(pilot["freq"], f"{pilot['freq']} MHz")
        self.video_title = f"{pilot['name']} — {channel}"

    def close_video(self):
        """Wraca z wideo do pomiaru sygnału, z tymi samymi ustawieniami co wcześniej."""
        self.stop_video()
        self.scanner = RaceScanner(self.race, *self.sweep)

    def stop_video(self):
        if self.video:
            self.video.close()
            self.video = None

    def video_png(self):
        """Ostatni półobraz z wideo. Strona pobiera go za każdym razem, gdy przyjdzie nowy."""
        video = self.video
        if video is None or video.image is None:
            return Response(status_code=404)
        return Response(png(video.image), media_type="image/png")

    def set_band(self, test24):
        self.cfg["test24"] = test24
        # Kanały z poprzedniego pasma nie mają już sensu, więc każdy dron dostaje domyślny kanał nowego
        freqs = self.default_freqs()
        for i, pilot in enumerate(self.cfg["pilots"]):
            pilot["freq"] = freqs[i % len(freqs)]
        self.pilots_editor.refresh()

    def add_pilot(self):
        pilots = self.cfg["pilots"]
        freqs = self.default_freqs()
        # Progi bierzemy od poprzedniego drona, bo VTX-y zwykle są do siebie podobne
        previous = pilots[-1] if pilots else THRESHOLDS
        pilots.append({
            "name": f"Dron {len(pilots) + 1}",
            "freq": freqs[len(pilots) % len(freqs)],
            "enter": previous["enter"],
            "exit": previous["exit"],
        })
        self.pilots_editor.refresh()

    def remove_pilot(self, pilot):
        self.cfg["pilots"].remove(pilot)
        self.pilots_editor.refresh()

    @ui.refreshable_method
    def pilots_editor(self):
        for pilot in self.cfg["pilots"]:
            with ui.row(align_items="end"):
                ui.input("Pilot").bind_value(pilot, "name")
                if self.cfg["test24"]:
                    freq = ui.number("MHz", min=BAND_24[0], max=BAND_24[1], precision=0)
                    freq.bind_value(pilot, "freq")
                    freq.tooltip("TX16S, protokół CW: 2400 + RF Chan.")
                else:
                    channel = ui.select(CHANNELS_58, label="Kanał", with_input=True)
                    channel.bind_value(pilot, "freq")
                    channel.classes("w-36")
                ui.number("Wejście [dB]").bind_value(pilot, "enter").tooltip("sygnał powyżej = dron w bramce")
                exit_threshold = ui.number("Wyjście [dB]")
                exit_threshold.bind_value(pilot, "exit")
                exit_threshold.tooltip("sygnał poniżej = dron odleciał; dopiero wtedy może się zaliczyć kolejny przelot")
                if not self.cfg["test24"]:
                    video = ui.button(icon="videocam", on_click=partial(self.show_video, pilot)).props("flat round")
                    video.tooltip("wideo z kamery tego drona")
                ui.button(icon="delete", on_click=partial(self.remove_pilot, pilot)).props("flat round")

    def page(self):
        ui.add_css(".board td, .board th { font-size: 1.5rem !important; }")
        ui.number.default_classes("w-28")
        ui.select.default_classes("w-28")

        with ui.row(align_items="center"):
            ui.button("Start", icon="play_arrow", color="positive", on_click=self.start)
            ui.button("Stop", icon="stop", color="negative", on_click=self.stop_race)
            status = ui.label().classes("text-xl")
        table = ui.table(columns=COLUMNS, rows=[], row_key="id").classes("w-full board")

        with ui.card() as video_card:
            with ui.row(align_items="center"):
                video_title = ui.label().classes("text-2xl")
                ui.button("Zamknij wideo", icon="close", on_click=self.close_video).props("outline")
            video_status = ui.label()
            video_image = ui.interactive_image().classes("w-[640px] max-w-full")
        video_card.visible = False  # pokazuje ją update_video, gdy wideo jest włączone

        with ui.expansion("Ustawienia", icon="settings", value=True).classes("w-full"):
            with ui.row(align_items="end"):
                ui.switch("Test 2,4 GHz", value=self.cfg["test24"], on_change=lambda e: self.set_band(e.value))
                ui.number("Okrążenia", min=1, precision=0).bind_value(self.cfg, "laps")
                min_lap = ui.number("Min. okrążenie [s]", min=0)
                min_lap.bind_value(self.cfg, "min_lap")
                min_lap.tooltip("krótszy przelot po poprzednim = odbicie sygnału, nie okrążenie")
                ui.select(list(range(0, 41, 8)), label="LNA [dB]").bind_value(self.cfg, "lna")
                ui.select(list(range(0, 63, 2)), label="VGA [dB]").bind_value(self.cfg, "vga")
            self.pilots_editor()
            with ui.row():
                ui.button("Dodaj drona", icon="add", on_click=self.add_pilot).props("outline")
                ui.button("Zastosuj", on_click=self.apply).tooltip("nowe ustawienia bez startu, np. do kalibracji progów")

        def show_drones(event):
            # Po zmianie wyścigu dron wybiera się od nowa: ta sama nazwa w innym wyścigu to inny plik
            if event.value:
                drones = sorted(f.stem for f in (LOGS / event.value).glob("*.json"))
            else:
                drones = []
            drone_select.set_options(drones, value=None)

        def show_log(event):
            """Sygnał drona w czasie. Żółte linie: poziome to progi wejścia i wyjścia, pionowe to zaliczone przeloty."""
            if not event.value:
                return
            log = json.loads((LOGS / race_select.value / f"{event.value}.json").read_text())
            series = chart.options["series"][0]
            series["data"] = log["samples"]
            marks = [{"yAxis": log["enter"]}, {"yAxis": log["exit"]}]
            marks += [{"xAxis": t} for t in log["passes"]]
            series["markLine"]["data"] = marks

        with ui.expansion("Sygnał po wyścigu", icon="show_chart").classes("w-full"):
            with ui.row():
                race_select = ui.select([], label="Wyścig", on_change=show_drones).classes("w-56")
                drone_select = ui.select([], label="Dron", on_change=show_log).classes("w-56")
            chart = ui.echart(log_chart_options()).classes("w-full h-[60vh]")

        def update_race_list():
            races = sorted((d.name for d in LOGS.glob("*")), reverse=True)
            if races != race_select.options:
                race_select.set_options(races)

        passes_seen = 0  # przeloty przy poprzednim odświeżeniu; jeśli przybyło, gramy dźwięk

        def refresh():
            nonlocal passes_seen
            now = time.time()
            race = self.race
            standings = race.standings(now)

            passes = sum(row["passes"] for row in standings)
            if passes > passes_seen:
                ui.run_javascript(BEEP)
            passes_seen = passes

            table.rows = [table_row(i, row, race.laps) for i, row in enumerate(standings)]

            code = self.scanner.proc.poll()
            if self.video:
                status.text = "Wideo z drona, pomiar sygnału wstrzymany"
            elif code is not None:
                status.text = f"hackrf_sweep zakończył działanie (kod {code}), szczegóły w konsoli. Sprawdź: hackrf_info"
            elif race.start_t is None:
                status.text = "Kalibracja: przeleć przez bramkę i ustaw progi według kolumny Sygnał"
            elif race.running:
                status.text = f"Wyścig: {format_time(now - race.start_t)}"
            else:
                status.text = f"Koniec: {format_time(race.stop_t - race.start_t)}"

        shown_image = None  # image_time półobrazu, który jest teraz na stronie

        def update_video():
            nonlocal shown_image
            video = self.video
            video_card.visible = video is not None
            if video is None:
                return
            video_title.text = self.video_title

            code = video.proc.poll()
            if code is not None:
                video_status.text = f"hackrf_transfer zakończył działanie (kod {code}), szczegóły w konsoli. Sprawdź: hackrf_info"
            elif video.image_time is None or time.monotonic() - video.image_time > 0.5:
                video_status.text = "Brak obrazu: dron wyłączony, inny kanał albo za słaby sygnał"
            else:
                video_status.text = ""

            if video.image_time != shown_image:
                shown_image = video.image_time
                # Po przełączeniu na innego drona stary obraz znika, zanim przyjdzie nowy
                video_image.set_source(f"/video.png?{shown_image}" if shown_image else "")

        ui.timer(1 / 10, refresh)
        ui.timer(1 / 20, update_video)
        # Logi zapisują się dopiero na koniec wyścigu, więc listę trzeba co jakiś czas odświeżać
        ui.timer(2, update_race_list)


if __name__ == "__main__":
    try:
        ui.run(LapTimerApp().page, title="Licznik okrążeń", host="127.0.0.1", reload=False, dark=True)
    except KeyboardInterrupt:
        # uvicorn po zatrzymaniu jeszcze raz rzuca Ctrl+C, a HackRF jest wtedy już zamknięty
        pass
