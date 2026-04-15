# RadioWęzeł (wersja pełna v0.2)

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
- pozwala wybierać wielu klientów jednocześnie,
- streamuje dźwięk z wejścia mikrofonowego do zaznaczonych klientów,
- skanuje katalog muzyki i katalog dźingli (`mp3`, `wav`, `ogg`),
- tworzy kolejkę i pozwala wstawiać dźingle przed/po utworze,
- ma automatyczne uruchamianie i zatrzymywanie kolejki wg harmonogramu,
- ma globalny offset ustawiany na klientach,
- ma prosty mechanizm parowania (hasło).

## Wymagania

- Python 3.11+
- (dla MP3 przez pydub) zainstalowany FFmpeg w systemie

## Instalacja

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

## Konfiguracja

```bash
cp server-config.example.json server-config.json
cp client-config.example.json client-config.json
```

Na każdym kliencie ustaw unikalny `client_id` i nazwę `client_name`.

## Uruchomienie

### Serwer

```bash
python -m radio_wz.server.server_app --config server-config.json
```

### Klient

```bash
python -m radio_wz.client.client_service --config client-config.json
```

## Uruchamianie klienta jako usługa (Windows)

Najprościej przez NSSM lub Task Scheduler ("Run whether user is logged on or not").
Wtedy klient działa po starcie systemu bez aktywnej sesji RDP.

## Uwagi wdrożeniowe

- Zalecany osobny VLAN dla radiowęzła.
- Ustaw stałe IP serwera/klientów lub DHCP reservation.
- Sprawdź zaporę Windows (porty UDP 42500/42510 i TCP 42520).


## Stabilność i bezpieczeństwo (v0.3)

- per-połączenie parowania klienta (brak globalnej autoryzacji),
- porównanie hasła przez `hmac.compare_digest`,
- walidacja komend sterujących i parametrów,
- ochrona przed wielokrotnym uruchomieniem nadawania,
- bezpieczne zamykanie wątków i czyszczenie kolejki pakietów,
- timeouty i obsługa błędów sieci/audio,
- walidacja czasu harmonogramu,
- automatyczne usuwanie nieaktywnych klientów.
