# agent-browser-termux

Persistent-сессия Playwright-шим, который имитирует CLI [agent-browser](https://github.com/vercel-labs/agent-browser) и работает на Termux + ваш существующий `~/playwright-termux` (Chromium + playwright-core).

После установки команда `agent-browser <…>` появляется в `$PATH` и работает ровно так, как ждёт чат-приложение из этого репо. LLM в чате полноценно управляет реальным Chromium на телефоне.

Никаких хостед-сервисов, никаких VPS. Всё локально на вашем устройстве.

## Что делает

- Один **демон** держит Chromium и BrowserContext постоянно — cookies, login, форма не теряются между командами.
- **CLI-обёртка** при первом вызове сама запускает демон в фоне (детачится). Дальше работает как любой обычный CLI.
- Совместимый набор команд: `open`, `back/forward/reload`, `screenshot`, `snapshot [-i]` с `@eN`-рефами для AI, `click/fill/type/press/upload/hover/dblclick/focus`, `mouse move|down|up|click|wheel`, `eval [-b]`, `get url|title|html|text`, `wait`, `tabs list|new|switch|close`, `cookies get|clear`, `set viewport`.
- `setInputFiles` через Playwright — нативный канал загрузки файлов в формы (например, при подаче документов в вуз).
- Сохраняет storage-state в `~/.cache/agent-browser/storage-state.json` — авторизация переживает перезапуск.

## Установка одной командой

В Termux на телефоне:

```bash
curl -sL https://raw.githubusercontent.com/RamadanIU/Chat/main/tools/agent-browser-termux/install.sh | bash
```

Что сделает скрипт:

1. Поставит `nodejs`, `git`, `curl` если их ещё нет (`pkg install -y …`).
2. Создаст или обновит `~/playwright-termux/` (если у вас он уже есть — оставит как есть, только подкинет `node_modules/playwright-core` если не было).
3. Положит `daemon.js` и `cli.js` в `~/playwright-termux/agent-browser-shim/`.
4. Создаст обёртку `$PREFIX/bin/agent-browser` (для Termux это `/data/data/com.termux/files/usr/bin/agent-browser`).
5. Прогонит `agent-browser version` для проверки.

После установки:

```bash
agent-browser open https://example.com
agent-browser screenshot ~/workspace/browser/screen.png
agent-browser snapshot -i           # дерево с @e1, @e2, ... для LLM
agent-browser click "@e3"
agent-browser fill 'input[name="email"]' "test@test.com"
agent-browser upload 'input[type=file]' ~/Documents/diplom.pdf
agent-browser kill                  # остановить демон
```

## Подключение к чат-приложению

В чате заходите в **Settings → Терминал** и указываете адрес вашего Termux-сервера (тот, через который сейчас работает `/exec` / `/term`). Чат-приложение продолжит шить туда команды — но теперь `agent-browser …` будет реально работать, потому что наш шим резолвится в `$PATH`.

Никаких изменений в самом чат-приложении не требуется.

## Авто-старт демона

Демон стартует **автоматически** при первом вызове `agent-browser <cmd>` — CLI-обёртка форкает его в фоне, ждёт `/health` и форвардит команду. Если уже запущен — переиспользует.

Контролируется переменными окружения (опционально):

```bash
export AGENT_BROWSER_PORT=9876         # порт демона (по умолчанию 9876)
export AGENT_BROWSER_HEADLESS=true     # false включает headed режим если есть X
export AGENT_BROWSER_VIEWPORT_W=1280
export AGENT_BROWSER_VIEWPORT_H=800
export PLAYWRIGHT_TERMUX_ROOT=$HOME/playwright-termux
export AGENT_BROWSER_EXTRA_ARGS=""     # доп. аргументы Chromium через пробел
```

Базовые аргументы запуска Chromium — те же что в вашем рабочем `~/playwright-termux/test-launch.js`: `--no-sandbox --disable-gpu --disable-dev-shm-usage`. Ничего лишнего шим не добавляет — известно что `--no-zygote`, `--disable-features=site-per-process` и подобные ломают Termux-Chromium 138. Если нужны дополнительные флаги — указывайте их в `AGENT_BROWSER_EXTRA_ARGS` и перезапускайте демон через `agent-browser kill`.

## Полный список команд

```
open <url> [--wait load|domcontentloaded|networkidle]
back | forward | reload
set viewport <w> <h>
screenshot <path> [--full] [--clip x,y,w,h]
snapshot [-i] [-d <depth>]                 # interactive-only mode добавляет @e-рефы
click|dblclick|hover|focus <selector|@ref>
fill <selector|@ref> <text>
type <selector|@ref> <text> [--delay ms]
press [<selector|@ref>] <key>              # Enter, Tab, Escape, Backspace, ArrowDown, …
upload <selector|@ref> <path...>           # один или несколько файлов
mouse move <x> <y> [steps]
mouse down|up [left|right|middle]
mouse click <x> <y> [left|right|middle]
mouse wheel <deltaY> [<deltaX>]
eval <code>                                # JS в page-context, результат в stdout как JSON
eval -b <base64>                           # тот же eval, но код пришёл в base64
get url|title|html|text [<selector>]
wait <selector|@ref> [timeout-ms]
wait timeout <ms>
wait load|domcontentloaded|networkidle
tabs list | tabs new [url] | tabs switch <i> | tabs close <i>
cookies get [<domain>] | cookies clear
status | version | kill | help
```

### Селекторы

- стандартный CSS: `input[name="email"]`, `.btn-primary`
- text-локатор: `text="Войти"`
- role-локатор: `role=button[name="Submit"]`
- ссылка по AI-снэпшоту: `@e1`, `@e2`, … (создаются при `snapshot -i`)

## Логи и диагностика

```bash
# Логи демона
tail -f ~/.cache/agent-browser/daemon.log

# Состояние
agent-browser status

# Посмотреть, что демон видит на странице (без AI)
agent-browser snapshot -i

# Чистый рестарт
agent-browser kill && agent-browser open about:blank
```

## Решение проблем

### `agent-browser: daemon failed to start within 30s`

Откройте лог: `cat ~/.cache/agent-browser/daemon.log`. Чаще всего:

- `CHROMIUM_PATH` не задан или указывает на несуществующий бинарь — поправьте `~/playwright-termux/.env`. Можно проверить так:
  ```bash
  source ~/playwright-termux/.env && file "$CHROMIUM_PATH"
  ```
- Не стоит `playwright-core`. Запустите `cd ~/playwright-termux && npm install playwright-core dotenv`.
- Порт 9876 занят. Установите другой:
  ```bash
  AGENT_BROWSER_PORT=9999 agent-browser kill   # старый
  AGENT_BROWSER_PORT=9999 agent-browser version
  ```

### `Target page, context or browser has been closed`

Chromium стартовал но сразу умер. Проверьте, что ваш родной `node ~/playwright-termux/test-launch.js` работает — он использует ровно те же аргументы, что и шим. Если родной тест работает, а шим нет — пришлите вывод `cat ~/.cache/agent-browser/daemon.log`. Если родной тоже падает — проблема в самом Chromium / Playwright-core.

### `setInputFiles` падает

Это значит, что переданный селектор не указывает на `<input type=file>` или элемент скрыт. Сделайте `snapshot -i`, найдите ref с пометкой `[file]`, и `agent-browser upload @e7 ~/file.pdf`.

### Браузер уходит в OOM на телефоне

Сократите viewport: `agent-browser set viewport 800 600`. Закройте лишние вкладки `agent-browser tabs close N`. Тяжёлые SPA вроде Google Docs могут не стартовать на 2–3 ГБ RAM — это ограничение телефона, не шима.

## Развёртывание не на Termux

Скрипт работает на любом Linux:

```bash
curl -sL https://raw.githubusercontent.com/RamadanIU/Chat/main/tools/agent-browser-termux/install.sh | bash
```

Положит обёртку в `~/.local/bin/agent-browser`. Добавьте `~/.local/bin` в `PATH`, если ещё нет.

## Удаление

```bash
agent-browser kill
rm -rf ~/playwright-termux/agent-browser-shim ~/.cache/agent-browser
rm $PREFIX/bin/agent-browser   # в Termux
# или: rm ~/.local/bin/agent-browser
```
