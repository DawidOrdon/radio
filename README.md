# RadioWęzeł (wersja pełna v0.3)

Aplikacja radiowęzła szkolnego oparta o Python, z serwerem desktop i klientem działającym w tle.

## Funkcjonalności

### Klient
- rozgłasza się w sieci LAN przez UDP broadcast,
- odbiera audio UDP i odtwarza je na wybranym wyjściu audio,
- posiada bufor opóźnienia (`offset_ms`) dla zsynchronizowanego startu,
- posiada autoreconnect (ciągłe discovery i nasłuch),
- udostępnia port sterujący TCP do parowania, zmiany offsetu i wyjścia audio,
- może działać jako proces tła (zalecane uruchomienie jako usługa Windows).

### Serwer (GUI desktop)
- wykrywa klientów automatycznie,
- pozwala wybierać wielu klientów jednocześnie (checkboxy przy klientach),
- układ GUI daje więcej miejsca na listę muzyki i przerwy/harmonogram, mniej na panel klientów,
- streamuje dźwięk z wejścia mikrofonowego do zaznaczonych klientów (przycisk Start transmisji),
- skanuje katalog muzyki i katalog dźingli (`mp3`, `wav`, `ogg`),
- tworzy kolejkę i pozwala wstawiać dźingle przed/po utworze,
- ma Pauza/Wznów dla kolejki: po wznowieniu utwór leci od miejsca pauzy, a mikrofon działa niezależnie,
- można uruchomić kolejkę ręcznie z automatyczną pauzą o wskazanej godzinie (Start do godziny),
- po kliknięciu Start transmisji można od razu uruchomić kolejkę bez ręcznego restartu trybu,
- mikrofon (wejście z interfejsu) jest miksowany z kolejką, więc działa cały czas także podczas muzyki,
- offset wpływa na harmonogram: start kolejki następuje po czasie `offset` (np. 12:14 + 10s).
- kolejka nie usuwa automatycznie utworów z listy (przesuwa tylko wskaźnik odtwarzania),
- ma automatyczne uruchamianie i zatrzymywanie kolejki wg wielu przedziałów harmonogramu,
- pozwala zapisać kilka presetów kolejek i przypisać konkretną kolejkę do konkretnej godziny/przerwy,
- harmonogram na koniec przerwy robi pauzę, a na kolejnej przerwie może wznowić/uruchomić odpowiedni preset,
- ma globalny offset ustawiany na klientach (wpisywany ręcznie w sekundach),
- ma przycisk wyboru wyjścia audio klienta z listy urządzeń odczytanej z klienta,
- ma wyszukiwarkę utworów i dźingli w panelu serwera (ułatwienie dla dużych bibliotek),
- ma ton testowy 1kHz do szybkiego sprawdzenia zaznaczonych klientów,
- pokazuje status streamu (START/STOP/TEST TON).
- ma prosty mechanizm parowania (hasło).

## Wymagania (Windows 11)

- Python 3.11+
- FFmpeg w systemie (dla plików MP3 przez `pydub`)
- Upewnij się, że **ffmpeg i ffprobe** są w PATH (np. `C:\\ffmpeg\\bin`)

## Instalacja (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Instalacja (Windows CMD)

```cmd
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Konfiguracja (Windows)

### PowerShell

```powershell
Copy-Item server-config.example.json server-config.json
Copy-Item client-config.example.json client-config.json
```

### CMD

```cmd
copy server-config.example.json server-config.json
copy client-config.example.json client-config.json
```

Na każdym kliencie ustaw unikalny `client_id` i nazwę `client_name`.

## Uruchomienie (Windows)

### Serwer

```powershell
python -m radio_wz.server.server_app --config server-config.json
```

### Klient

```powershell
python -m radio_wz.client.client_service --config client-config.json
```


### Klient (GUI / front)

```powershell
python -m radio_wz.client.client_gui --config client-config.json
```

GUI klienta pozwala podejrzeć status i lokalnie zmienić offset / output device.
Dodatkowo pokazuje wskaźnik "Audio RX" (czy aktualnie leci dźwięk) i umożliwia odtworzenie lokalnego tonu testowego.

## Uruchamianie klienta jako usługa (Windows)

Najprościej przez NSSM lub Task Scheduler (`Run whether user is logged on or not`).
Wtedy klient działa po starcie systemu bez aktywnej sesji RDP.

### Uwaga: RDP / odłączony monitor a dźwięk

Na Windows wyjście audio może zniknąć po rozłączeniu Pulpitu zdalnego albo po odpięciu monitora HDMI/DP (urządzenie audio z monitora przestaje istnieć).

W tej wersji klient, gdy straci skonfigurowane urządzenie audio, automatycznie przełącza się na domyślne wyjście systemowe. Jeśli to nie pomaga:
- ustaw na kliencie stałe fizyczne wyjście audio (np. Realtek/USB DAC), nie „Remote Audio” z RDP,
- uruchamiaj klienta jako usługę/zadanie systemowe (bez zależności od sesji użytkownika),
- jeśli sprzęt nie ma stałego wyjścia audio, użyj „dummy HDMI” albo wirtualnego urządzenia audio.

## Firewall (Windows)

Otwórz porty:
- UDP 42500 (discovery)
- UDP 42510 (audio)
- TCP 42520 (control)

## Stabilność i bezpieczeństwo (v0.3)

- per-połączenie parowania klienta (brak globalnej autoryzacji),
- porównanie hasła przez `hmac.compare_digest`,
- walidacja komend sterujących i parametrów,
- ochrona przed wielokrotnym uruchomieniem nadawania,
- bezpieczne zamykanie wątków i czyszczenie kolejki pakietów,
- timeouty i obsługa błędów sieci/audio,
- walidacja czasu harmonogramu,
- automatyczne usuwanie nieaktywnych klientów.


## Zmiany UI (runtime)

- offset podajesz ręcznie jako sekundy (np. `2` lub `2.5`),
- harmonogram pozwala dodawać wiele przedziałów START/STOP (np. kilka przerw),
- parametry audio (`sample_rate`, `channels`, `blocksize`, `mic_input_device`) zmieniasz z poziomu GUI bez edycji pliku konfiguracyjnego,
- tryb mikrofonu działa jako pass-through: serwer nie przetwarza sygnału, tylko przekazuje wejście audio do klientów.


## Tuning przy przerywaniu dźwięku

Jeśli dźwięk przerywa, zwiększ w `client-config.json`:
- `offset_ms` (np. 2500-4000),
- `jitter_target_packets` (np. 120-180),
- ewentualnie `blocksize` (np. 960 -> 1440).

Dodatkowo upewnij się, że sieć LAN nie jest przeciążona i że urządzenie audio klienta działa z tym samym `sample_rate` i `channels` co serwer.


## Budowanie klienta do EXE (Windows)

Szybka ścieżka (PowerShell):

```powershell
./scripts/build_client_exe.ps1
```

Po buildzie plik znajdziesz tutaj:

```text
dist/RadioWezelClientGUI.exe
```

Manualnie (bez skryptu):

```powershell
.\.venv\Scripts\pip install pyinstaller
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed --name RadioWezelClientGUI --hidden-import sounddevice .\radio_wz\client\client_gui.py
```


Jeśli `client-config.json` nie istnieje, klient GUI utworzy domyślny plik automatycznie przy pierwszym starcie.
