# SDR Recorder

Nagrywa transmisje z działającej instancji [OpenWebRX](https://github.com/jketterl/openwebrx) do plików MP3 i udostępnia je na lokalnej stronie do odsłuchu.

Trzy kontenery Dockera:

- **recorder** — łączy się z OpenWebRX po WebSocket (jak przeglądarka), odbiera zdemodulowane audio + poziom sygnału (S-metr), sam wykrywa „ktoś nadaje" i zapisuje MP3.
- **transcriber** — transkrybuje nagrania (whisper.cpp, model `base Q5`, polski) starsze niż 5 min, zapisuje `.txt` ze znacznikami `[GG:MM:SS]`.
- **web** — serwuje stronę z listą nagrań, odtwarzaczem, przyciskiem „Usuń" oraz rozwijaną transkrypcją (port 8074).

## Jak działa

1. `recorder` loguje się do OpenWebRX jako headless klient `receiver`, wybiera profil SDR pokrywający docelową częstotliwość i ustawia demodulację (domyślnie NFM).
2. Squelch serwera jest wyłączony (ustawiony na otwarty) — bramkowanie robi sam rejestrator na podstawie **S-metra** i **energii audio**. Poziom szumu jest liczony jako **przesuwne minimum z ostatnich `NOISE_WINDOW_SECONDS`** (śledzi zmiany szumu w ciągu dnia), a próg otwarcia to `SQUELCH_MARGIN`/`RMS_MARGIN` dB ponad tym minimum. Dzięki temu działa **bufer pre-roll** (początek transmisji nie jest ucięty), sam szum nie jest nagrywany, a ciągła nośna/interferencja po chwili przestaje być rejestrowana.
3. Gdy pojawia się sygnał, rejestrator najpierw zapisuje bufor pre-roll (dźwięk sprzed wykrycia), potem strumień; kończy nagranie po `END_SILENCE_SECONDS` ciszy (twardy limit `MAX_RECORDING_SECONDS`).
4. Zamknięty plik trafia najpierw do prywatnego, bezstratnego WAV-a. Rejestrator odszumia go łagodnym `afftdn` (lepiej dopasowanym do wąskopasmowego NFM; DeepFilterNet3 można włączyć opcjonalnie), sprawdza, czy został użyteczny fragment dźwięku, i usuwa puste pliki. Dopiero po pozytywnym wyniku atomowo publikuje MP3 w `recordings/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.mp3`. Przy starcie czyści także puste nagrania pozostawione przez starszą wersję.
5. `transcriber` co ~1 min przeszukuje opublikowane nagrania starsze niż `MIN_AGE_SECONDS`, transkrybuje je (whisper.cpp, base Q5) i zapisuje obok pliku `.txt` w formacie `[GG:MM:SS] tekst` (oraz `.json` ze statusem).

## Uruchomienie

### Lokalnie (budowanie ze źródeł)

```bash
cp .env.example .env        # opcjonalnie, można też bez — są wartości domyślne
docker compose up -d --build
```

`docker compose` sam scala `docker-compose.override.yml`, który dokłada `build:`. Plik `docker-compose.yml` pozostaje dokładnie w takiej formie, w jakiej używa go serwer: sam `image:`, bez `build:`.

### Na serwerze (ciągnięcie gotowych obrazów)

Serwer ma własny katalog z tym repozytorium i **własny `.env`** — nigdy nie edytuj `docker-compose.yml` ręcznie:

```bash
cp .env.example .env
$EDITOR .env                # ustaw OPENWEBRX_WS_URL, WEB_PORT, THREADS
chmod +x deploy.sh
./deploy.sh                 # wdrożenie wersji z .env (domyślnie latest)
```

Strona odsłuchu: `http://<adres-serwera>:8074/`

## Budowanie i wdrażanie

Obrazy buduje **GitHub Actions** i pcha do **GitHub Container Registry**. Wdrożenie na serwerze jest **ręczne** — `deploy.sh` nigdy nie wdraża sam.

```
git push  →  Actions (build + push do ghcr.io)  →  ./deploy.sh <tag>  na serwerze
```

Buildy uruchamiają się **wyłącznie** przy wypchnięciu na `main` albo ręcznie przez „Run workflow" — wtedy w UI wybierasz gałąź. Pull requesty nic nie budują.

Cache warstw trzyma się na GitHubie, więc po pierwszym budowaniu — a głównie po tym kosztownym, z kompilacją whisper.cpp — kolejne trwają sekundy.

Obrazy dostają dwa tagi:

- `sha-xxxxxxx` — konkretny commit, do wdrażania reprodukowalnego i do rollbacku. Dostają go **wszystkie** buildy, także z gałęzi,
- `latest` — **tylko** buildy z `main`. Gałąź testowa nigdy go nie nadpisze, więc `./deploy.sh` bez argumentu zawsze znaczy „to, co jest na `main`".

Repozytorium jest publiczne, więc obrazy w GHCR też. Serwer ciągnie je anonimowo, **`docker login` nie jest potrzebny** i nie ma żadnych sekretów do skonfigurowania.

### `deploy.sh`

Skrypt stoi po stronie serwera i **nigdy nie wykonuje operacji na gicie** — aktualizację konfiguracji robisz osobno przez `git pull`.

```bash
./deploy.sh                 # wersja z .env (domyślnie latest)
./deploy.sh sha-abc1234     # konkretna wersja
./deploy.sh --rollback      # powrót do poprzednio udanej wersji
./deploy.sh --list          # historia wdrożeń
./deploy.sh --status        # co działa teraz
```

Co się dzieje przy `./deploy.sh <tag>`:

1. `docker compose pull` **zanim** cokolwiek się zmieni — zły obraz nie wywróci działającej usługi.
2. `docker compose up -d --remove-orphans`.
3. Health check z realnymi sygnałami, nie tylko z `docker ps`:
   - wszystkie trzy kontenery `running` i bez restartów,
   - `web` odpowiada HTTP na porcie z `.env`,
   - `transcriber` wypisał `using whisper binary` (czyli obraz i model są sprawne).
4. Nie powiodło się → **automatyczny rollback** na poprzednią wersję, wypis logów i kod wyjścia 1.
5. Powiodło się → wpis do `.deploy-history`, na końcu polecenie `curl` do sprawdzenia strony.

`recorder` jest sprawdzany osobno i **nie blokuje wdrożenia**: brak `receiver ready:` oznacza najczęściej zły `OPENWEBRX_WS_URL` w `.env`, a rollback obrazu tego nie naprawi, tylko zaburzy diagnostykę. Dostajesz ostrzeżenie, nie błędne wdrożenie.

Powtarzane `--rollback` cofa się coraz dalej wstecz. Historia trzyma 20 ostatnich wersji w `.deploy-history` (plik lokalny, ignorowany przez gita).

## Konfiguracja (zmienne w `.env`)

Wszystko poniżej ustawia się w `.env`, a `docker-compose.yml` czyta to przez `${ZMIENNA:-domyślna}`. Dzięki temu serwer ma własną konfigurację bez edycji plików śledzonych przez gita.

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `IMAGE_TAG` | `latest` | wersja obrazów; `deploy.sh` używa własnej wartości, więc tu nie musisz jej zmieniać |
| `OPENWEBRX_WS_URL` | `ws://192.168.68.67:8073/ws/` | adres WebSocket OpenWebRX. Gdy OpenWebRX jest na tym samym hoście: `ws://host.docker.internal:8073/ws/` |
| `FREQUENCIES` | `149287500:nfm` | lista `freq:mod` oddzielona przecinkami (na razie nagrywana jest pierwsza) |
| `SQUELCH_MARGIN` | `10` | próg otwarcia ponad poziomem szumu (S-metr, dB) |
| `RMS_MARGIN` | `20` | próg otwarcia ponad szumem (energia audio, dB) |
| `NOISE_WINDOW_SECONDS` | `600` | okno przesuwnego minimum szumu (śledzenie zmian szumu) |
| `MAX_RECORDING_SECONDS` | `600` | twardy limit długości nagrania (ochrona przed zablokowaną nośną) |
| `PRE_ROLL_SECONDS` | `2` | długość bufora początku nadawania |
| `END_SILENCE_SECONDS` | `3` | czas ciszy kończący nagranie |
| `MIN_DURATION_SECONDS` | `1` | odrzucanie krótszych nagrań |
| `MP3_BITRATE` | `48` | bitrate MP3 (kbps) |
| `OUTPUT_RATE` | `12000` | częstotliwość próbkowania audio |
| `RECORDINGS_DIR` | `./recordings` | katalog nagrań **na hoście** (w kontenerze zawsze `/recordings`) |
| `WEB_PORT` | `8074` | port strony odsłuchu **na hoście** (w kontenerze zawsze `8074`) |
| `DENOISE_ENABLED` | `1` | odszumianie po nagraniu i test sygnału |
| `DENOISE_BACKEND` | `afftdn` | backend: łagodny filtr dla NFM; `deepfilternet` jako opcja |
| `DENOISE_NR` | `8` | siła `afftdn` (używana tylko przy backendzie `afftdn`) |
| `DENOISE_NOISE_FLOOR_DB` | `-40` | zakładany poziom szumu dla `afftdn` |
| `DEEPFILTER_POST_FILTER` | `0` | dodatkowe tłumienie trudnych fragmentów w DeepFilterNet; domyślnie wyłączone, bo może ucinać mowę |
| `DEEPFILTER_COMPENSATE_DELAY` | `1` | kompensata opóźnienia STFT/modelu |
| `DENOISE_SILENCE_DB` | `-45` | próg uznania fragmentu za sygnał po odszumieniu |
| `DENOISE_MIN_SIGNAL_SECONDS` | `0.25` | minimalna długość fragmentu powyżej progu |
| `TZ` | `Europe/Warsaw` | strefa czasowa nazw plików |

### Transkryber

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `MIN_AGE_SECONDS` | `300` | transkrybuj nagrania starsze niż (s) |
| `LANGUAGE` | `pl` | język transkrypcji |
| `MODEL_PATH` | `/models/ggml-base-q5_1.bin` | model whisper.cpp (base Q5) |
| `THREADS` | `3` | liczba wątków whisper; bezpieczny kompromis dla J1800 |
| `WHISPER_BEAM_SIZE` | `2` | szerokość beam search; mniejsza wartość zmniejsza obciążenie |
| `WHISPER_BEST_OF` | `2` | liczba kandydatów dekodowania |
| `WHISPER_NO_SPEECH_THRESHOLD` | `0.6` | próg odrzucania fragmentów bez mowy |
| `WHISPER_SUPPRESS_NON_SPEECH` | `1` | tłumienie tokenów niespeechowych i halucynacji na szumie |
| `PRE_ROLL_SECONDS` | `2` | korekta znaczników czasu o bufor pre-roll |
| `MAX_FILE_MB` | `20` | pomijaj pliki większe niż (MB) |

W `.env.example` ustawione jest `THREADS=3`. J1800 ma 4 wątki logiczne, ale system je też wykorzystuje — przy `4` transkrypcja potrafi wypchnąć resztę.

## Uwagi do obrazów

- **`transcriber` kompiluje whisper.cpp ze źródeł.** Wersja jest przypięta w `transcriber/Dockerfile` (`ARG WHISPER_VERSION=v1.9.4`) — bez tego każdy build dostałby inną rewizję i obrazy nie byłyby powtarzalne.
- **`GGML_NATIVE=OFF` jest celowe.** ggml domyślnie kompiluje z `-march=native`, czyli pod procesor maszyny budującej. Runner GitHub Actions i J1800 to różne CPU, więc binarka zależałaby od tego, na czym akurat ścigał runner — zmiana runnerów po cichu zmieniałaby obraz, a instrukcje spoza AVX2 dałyby na J1800 `SIGILL`. Na ARM ustaw `ON`.
- **`WHISPER_BUILD_IS_DEV=OFF` jest celowe.** Whisper domyślnie zgłasza się jako „1.9.4-dev" nawet przy budowie z taga release. Ta flaga sprawia, że wersja w logach odpowiada rzeczywistości.
- **Model whisper (base Q5, ~60 MB) jest wbudowany w obraz.** Dzięki temu wdrożenie jest powtarzalne; wersję modelu zmienisz w `ARG MODEL_FILE`.
- **`.dockerignore` leży w katalogach `recorder/`, `web/`, `transcriber/`, nie w rogu repo.** Docker szuka go w katalogu kontekstu buildu, a konteksty to `./recorder` itd.
- **`recorder` zawiera oficjalny, statyczny binarny `deep-filter` 0.5.6 z wbudowanym modelem DeepFilterNet3.** Nie instaluje PyTorch ani nie wymaga GPU; wejście jest konwertowane na WAV 48 kHz tylko na czas obróbki po zakończeniu nagrania.

## Współistnienie z nasłuchem

Rejestrator jest stałym klientem i trzyma SDR na paśmie obejmującym `FREQUENCIES`. Gdy w przeglądarce przełączysz profil/pasmo poza to pasmo, nagrywanie się wstrzyma i wróci po powrocie.
