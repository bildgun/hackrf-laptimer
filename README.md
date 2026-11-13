# HackRF: skaner widma i licznik okrążeń FPV

Narzędzia do HackRF One oparte na `hackrf_sweep` i `hackrf_transfer`:

- `rx.py` — skaner widma w terminalu (domyślnie pasmo FPV 5,8 GHz), z podpisanymi kanałami analogowymi.
- `gui.py` — skaner widma w przeglądarce (NiceGUI), ze spektrogramem, max-hold i szybkimi zakresami.
- `laptimer/` — licznik okrążeń dronów po sygnale VTX, z zapisem sygnału z każdego wyścigu i podglądem wideo z drona.
- `video.py` — odbiór analogowego wideo FPV przez `hackrf_transfer`, używany przez licznik okrążeń.

## Wymagania

- HackRF One i narzędzia `hackrf` (`hackrf_sweep`, `hackrf_transfer`, `hackrf_info`)
- Python 3.11+

## Instalacja

```bash
sudo apt install hackrf          # Debian/Ubuntu; w innych dystrybucjach pakiet zwykle też nazywa się hackrf
hackrf_info                      # sprawdzenie, czy HackRF jest widoczny

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Użycie

Wszystkie polecenia uruchamia się w aktywnym `.venv`. Naraz może działać tylko jeden program, bo HackRF obsługuje tylko jeden `hackrf_sweep`.

### Skaner w terminalu

```bash
python rx.py                     # 5300–5960 MHz
python rx.py -f 5600:5960        # węższy zakres
python rx.py --lna 32 --vga 8    # słabe, odległe sygnały
python rx.py --lna 8 --vga 8     # nadajnik blisko (mniej przesterowania)
```

`--amp` włącza przedwzmacniacz +14 dB, nie używaj go w pobliżu nadajnika. Wszystkie opcje: `python rx.py --help`. Wyjście: Ctrl+C.

### Skaner w przeglądarce

```bash
python gui.py                    # http://127.0.0.1:8080
```

Zakres, LNA, VGA i AMP ustawia się na stronie, zmiany działają po kliknięciu „Zastosuj”. Max-hold czyści się, wyłączając go i włączając ponownie.

Pod wykresem jest spektrogram z ostatnich około 30 sekund, najnowsze na górze. Kolor idzie od czerni (Y min) do jasnej zieleni (Y max), więc pola Y min i Y max ustawiają też jego kontrast. Po zmianie zakresu spektrogram zaczyna się od nowa.

Przełącznik „Zapis CSV” zapisuje widmo do `recordings/<data_godzina>.csv`, wiersz na każdy bin z każdego przebiegu `hackrf_sweep`:

```
time,freq_mhz,level_db
2026-11-12 19:07:15.339123,5815.5,-66.54
```

`time` to data i godzina pomiaru z `hackrf_sweep`, `freq_mhz` to środek binu (1 MHz), a `level_db` to surowy poziom, bez wygładzania z wykresu. Zapis trwa w tym samym pliku także po zmianie zakresu. Plik rośnie szybko: dla 5300–5960 MHz to około 0,5 MB/s, czyli prawie 2 GB na godzinę. Aktualny rozmiar widać pod wykresem.

### Licznik okrążeń

```bash
python laptimer/gui.py           # http://127.0.0.1:8080
```

1. W „Ustawieniach” dodaj drony i wybierz ich kanały (domyślnie RaceBand R1 i R2). Po każdej zmianie kliknij „Zastosuj”.
2. Kalibracja: przeleć przez bramkę i ustaw progi według kolumny „Sygnał / max”. **Wejście** ustaw poniżej szczytu sygnału przy bramce, a **Wyjście** wyraźnie niżej niż Wejście.
3. Kliknij „Start”. Pierwszy przelot to holeshot, kolejne to okrążenia. Przelot wcześniej niż „Min. okrążenie” po poprzednim jest odrzucany jako odbicie sygnału.
4. Wyścig kończy się, gdy wszyscy ukończą okrążenia, albo po kliknięciu „Stop”.

Sygnał każdego drona zapisuje się w `laptimer/logs/<data_godzina>/<dron>.json`. Wykres jest w sekcji „Sygnał po wyścigu”.

Przełącznik „Test 2,4 GHz” pozwala sprawdzić licznik bez VTX: aparatura TX16S z protokołem CW nadaje na 2400 + RF Chan. MHz.

#### Wideo z drona

Przycisk z kamerą przy dronie w „Ustawieniach” pokazuje obraz z jego kamery, podpisany nazwą drona i kanałem. Działa z analogowym FPV 5,8 GHz (PAL i NTSC), obraz jest czarno-biały. HackRF ma jeden tor odbiorczy o szerokości do 20 MHz, a kanały RaceBand są oddalone o 37 MHz, więc odbiera naraz albo widmo, albo wideo. Na czas wideo pomiar sygnału jest wstrzymany. „Zamknij wideo” albo „Start” wracają do pomiaru, a w trakcie wyścigu wideo jest niedostępne. Obraz odświeża się 12,5 razy na sekundę, a dekodowanie zajmuje do około połowy jednego rdzenia procesora.
