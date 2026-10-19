# SolidWorks-MCP — форк RBR

Форк [hjbaard/SolidWorks-MCP](https://github.com/hjbaard/SolidWorks-MCP) с расширением RBR.
Все доработки — в подпакете `src/solidworks_mcp/rbr/`; файлы оригинала не изменены.

## Ветки

| Ветка | Содержимое |
|---|---|
| `main` | зеркало `upstream/main`, обновляется автоматически, руками не трогать |
| `rbr` | `main` + расширение RBR; ветка по умолчанию, отсюда ставится сервер |
| `sync/upstream-<sha>` | автоматический мерж новой версии upstream → PR в `rbr` |

## Слежение за upstream

`.github/workflows/upstream-sync.yml` — по понедельникам 06:17 UTC и вручную (Actions → Upstream sync → Run):

1. зеркалит `upstream/main` в `main`;
2. если upstream ушёл вперёд — мержит его в `sync/upstream-<sha>`, гоняет unit-тесты на Windows, открывает PR в `rbr` со списком коммитов и новыми строками CHANGELOG;
3. при конфликте — открывает issue.

`tests/rbr/test_upstream_contract.py` хранит хэши методов оригинала, которые расширение патчит или использует. Если upstream их изменит — тест в PR падает: прочитать изменение, при необходимости поправить `rbr/fastpath.py` / `rbr/journal.py`, обновить хэши `python tests/rbr/test_upstream_contract.py --update`.

Перед мержем PR — живая проверка на SolidWorks: `python -m solidworks_mcp.rbr --selftest`.

## Что добавлено

**Скорость** (`rbr/fastpath.py`), деталь 157 фич, SW 2025:

| Место | Было | Стало |
|---|---|---|
| Снимок истории перед каждым вызовом | 860–1190 мс | 1–5 мс |
| Обход дерева фич (`list_features`, история, откат) | 200–1100 мс | 130–150 мс |

**Прозрачность**

- Журнал всех вызовов: `%LOCALAPPDATA%\solidworks-mcp\journal\YYYY-MM-DD.jsonl` — инструмент, аргументы, полный код скриптов, документ, мс, ошибка, изменение дерева. Инструмент `get_journal`.
- `tool_source(name)` — исходный код любого инструмента.
- `run_script` возвращает `features {before, after, added}`.

**Скрипты**: `run_script`, библиотека `list_scripts` / `get_script` / `save_script` / `run_saved_script(name, args)`.
Встроенные: `rbr/lib/*.py`; пользовательские: `%LOCALAPPDATA%\solidworks-mcp\scripts` или папка из `SWMCP_SCRIPTS` (можно общую в Google Drive/git).

**Инструменты**: `set_units`, `get_custom_properties`, `set_custom_properties`, `list_configurations`, `activate_configuration`, `add_configuration`, `export_face_dxf`, `assembly_bom`.
Новые детали и сборки сразу переводятся в MMGS (`SWMCP_NEW_DOC_UNITS=off` отключает).

## Установка (Windows)

```powershell
git clone -b rbr https://github.com/RomanBRempel/SolidWorks-MCP.git $HOME\source\SolidWorks-MCP
uv tool install --force --editable $HOME\source\SolidWorks-MCP
```

Конфиг MCP-клиента (`claude_desktop_config.json`):

```json
"solidworks": {
  "command": "C:\\Users\\<user>\\AppData\\Roaming\\uv\\tools\\solidworks-mcp\\Scripts\\python.exe",
  "args": ["-m", "solidworks_mcp.rbr"]
}
```

Обновление: `git -C $HOME\source\SolidWorks-MCP pull` и перезапуск клиента (editable — переустановка не нужна).

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SWMCP_HOME` | `%LOCALAPPDATA%\solidworks-mcp` | журнал и пользовательские скрипты |
| `SWMCP_SCRIPTS` | `<SWMCP_HOME>\scripts` | папка библиотеки скриптов |
| `SWMCP_NEW_DOC_UNITS` | `MMGS` | единицы новых документов; `off` — не трогать |

## Тесты

```powershell
uv run --extra dev pytest -m "not solidworks"      # Windows: оригинал + rbr
pytest tests/rbr --confcutdir=tests/rbr             # любая ОС: только rbr, на фейковом COM
```
