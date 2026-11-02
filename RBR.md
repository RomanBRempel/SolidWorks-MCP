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

**Скорость по журналу реальной сессии** (`rbr/speed.py`, 1.3.0; GAT-27 v5, 113 фич)

| Место | Было | Стало |
|---|---|---|
| Отказ `add_fillet`: диагностика каждого ребра | 99,7 с на 48 рёбер | ≤ `SWMCP_FILLET_DIAG_S` (15) с, непроверенные рёбра названы |
| `list_edges(within_mm / min_length_mm)` | 46–49 с: 4 COM-вызова на каждое ребро тела | 1 вызов на ребро для фильтра, полные данные — только для попавших |
| `rebuild_profile` | — | время перестроения по фичам (`IFeatureStatistics`) |

**Прозрачность**

- Журнал всех вызовов: `%LOCALAPPDATA%\solidworks-mcp\journal\YYYY-MM-DD.jsonl` — инструмент, аргументы, полный код скриптов, документ, мс, ошибка, изменение дерева. Инструмент `get_journal`.
- `tool_source(name)` — исходный код любого инструмента.
- `run_script` возвращает `features {before, after, added}`.

**Надёжность** (`rbr/guard.py`, с 1.1.0) — после инцидента 2026-10-19: оболочка упала с `RPC_E_SERVERFAULT`, SolidWorks остался «замороженным» флагами сервера.

| Мера | Как работает |
|---|---|
| Сброс UI после сбоя | любой упавший изменяющий вызов → `CommandInProgress=False`, вид/дерево/эскизы включены во всех документах; оставшийся флаг снимается перед следующим вызовом и при подключении к SW |
| `release_ui(dry_run)` | ручной сброс / диагностика |
| Read-only без заморозки | `get_*`, `list_*`, `read_sketch`, `shell_check`… — без снимка, флагов и перерисовки (−~0,5 с на тяжёлой детали) |
| Межпроцессная блокировка | mutex `Local\solidworks-mcp-rbr` на каждый вызов; ожидание `SWMCP_LOCK_WAIT_S` (30 с) |
| Владение документом | менявший процесс владеет документом `SWMCP_OWNER_MIN` (10) мин; изменения из другого процесса отклоняются, чтение разрешено; `release_document` |
| Fail-fast | если вызов идёт дольше `SWMCP_BUSY_FAIL_S` (20 с), новые сразу получают «занят: инструмент, N с» вместо ожидания в очереди |
| `server_health` | без SolidWorks: текущий вызов и возраст, очередь, держатель блокировки, владельцы, последние ошибки |
| `get_journal(since, entries=False)` | телеметрия p50/p95 по инструментам из файла, без SolidWorks; в журнале есть `pid` |
| `shell_check(thickness)` | до оболочки: толщина материала лучами (< 2t — стенки сходятся, полости нет → `fail`, < 2,5t → `risk`) + радиусы кривизны свободных поверхностей (< t → `risk`); `max_safe_thickness_mm` |
| Карантин после `RPC_E_SERVERFAULT` (с 1.2.0) | падение SW внутри вызова → изменения отклоняются до перезапуска SW (новый PID снимает карантин сам), чтение работает; `release_ui(clear_quarantine=True)` — вручную. Повод: 2026-10-19 после второго падения оболочки `open_part` в том же SW повис |
| `shell_check` (1.2.2) | толщина материала — лучами по тесселяции в Python (`rbr/meshwall.py`): на GAT-27 v5 0,5 с на 7939 лучей против 29 с у `RayIntersections` на 2560. Кривизна: сопряжения (blend, 4007) и свободные поверхности с выпуклым R < t → `risk` с советом перенести скругления после оболочки; цилиндр/сфера/тор SW обрезает сам (проверено). `budget_s`, `partial`, `timing_s`, счёт по типам поверхностей |
| `add_shell` без эффекта | оболочка, не убравшая материал (объём не изменился), — ошибка с откатом. Проверено: 10,5 мм на детали шириной 19,6 мм SW «строил» с `ok` |

Ограничение: несколько чатов через **один** процесс сервера для владения — один владелец; разделяются только процессы (`pids` в журнале покажет, сколько их).

**Скрипты**: в пространстве имён `run_script` (1.3.1) — `api(obj, contains)` (реальные методы/свойства объекта в этой версии SW), `features(type)` (дерево одним вызовом), алиас `swapp`, `np`; в описании — правила COM: out-параметры как кортеж, `wrap()` сырых dispatch. Повод: в журнале ошибки `swapp`, `tuple.InvokeTypes`, `ILoftFeatureData.StartConstraint`, `numpy`. `run_script`, библиотека `list_scripts` / `get_script` / `save_script` / `run_saved_script(name, args)`.
Встроенные: `rbr/lib/*.py`; пользовательские: `%LOCALAPPDATA%\solidworks-mcp\scripts` или папка из `SWMCP_SCRIPTS` (можно общую в Google Drive/git).

**Инструменты**: `set_units`, `get_custom_properties`, `set_custom_properties`, `list_configurations`, `activate_configuration`, `add_configuration`, `export_face_dxf`, `assembly_bom`.
Новые детали и сборки сразу переводятся в MMGS (`SWMCP_NEW_DOC_UNITS=off` отключает).

**Долгие операции и нагрузка** (1.4.0, по итогам TORUK rev2; клиент-мост режет ответ на 60 с)

| Что | Как работает |
|---|---|
| Фоновые задания | `run_script_async(code, args)` / `run_saved_script_async(name, args)` → `{job_id}` сразу; скрипт выполняется в той же COM-очереди под guard и журналом. `get_job(job_id)` → `{state: queued\|running\|done\|failed, elapsed_s, result, error, traceback, features_added}`; `list_jobs(last_n)`. Записи — `<SWMCP_HOME>\jobs\*.json`, живут 24 ч и видны из любого процесса сервера |
| `run_script` не обрывается | синхронный вызов ждёт до `SWMCP_SYNC_WAIT_S` (50 с) и, если скрипт ещё идёт, отвечает `{job_id, state: running}` — работа продолжается, результат заберёт `get_job`. Скрипт никогда не убивается по таймауту ответа |
| `sync_equation_file(path=None)` | перечитать связанный файл глобальных переменных: новые имена добавлены, изменённые обновлены, пропавшие из файла названы; уравнения размеров (`"D1@..."`) не трогаются. Каждый отказ — с причиной (дубль имени, битое уравнение-остаток — удаляется и повторяется автоматически, ссылка, которую ничто не резолвит). Штатный `UpdateValuesFromExternalEquationFile` новые имена не добавляет, а `Add2` молча отвечает −1 |
| `server_health` + | память SLDWORKS.exe (working set, % от RAM — без COM, через WinAPI), число открытых документов (быстрый зонд, только когда COM-поток свободен), предупреждение при > `SWMCP_DOCS_WARN` (30) документах или > `SWMCP_MEMORY_WARN_PCT` (70) % памяти |
| `close_hidden_documents(keep=[])` | закрыть невидимые, сохранённые и не занятые другим процессом документы (сборки тихо открывают свои детали; после десятков документов всё ползёт). Активный, текущий, видимые и несохранённые не трогаются |
| `check_containment(envelope, keepout=None)` | по каждому телу keepout-детали: объём, % внутри тел envelope-детали, мм³ снаружи — пересечением копий через `IBody2::Operations2`; детали не меняются. Детали должны делить начало координат (master-model от одного скелета) |
| `set_equations` при отказе | называет номер уравнения в списке и его текст (весь список откатывается, как раньше) |
| `screenshot` вне экрана | перед снимком документ активируется внутри SolidWorks и перерисовывается; однотонный PNG (свёрнутое/закрытое окно → белый кадр из графического буфера) распознаётся декодером на stdlib и помечается `blank: true` с подсказкой, с одной повторной попыткой |

**Скрипты 1.4.0** — в пространстве имён `run_script` добавлены:
`vec(*doubles)` и `pt(x, y, z)` — правильные VARIANT для `CreateSpline2`, `MathUtility.CreatePoint` и т.п. (питоновский список приходит туда как None или мусор 1e-311); `ui_unlocked()` — контекстный менеджер, временно снимающий `CommandInProgress` и заморозку графики (без него `InsertSketch`/`EditSketch` не открывают эскиз); `tree_before()` + `features_added(before)` + `rename_new(before, name)` — имена добавленных фич и переименование (присваивание `IFeature.Name` объекту, который вернул `InsertRefPlane`, падает «Несовпадение типов» — фича, перечитанная из дерева, переименовывается); `combine(op, main, tools)` — объединение тел проверенным путём `combine_bodies` (`InsertCombineFeature` из скрипта напрямую не срабатывает); `mirror_body(body, plane, merge=True)` — зеркало тела БЕЗ слияния SolidWorks (его `Merge=True` давал битое тело: у полуобвода 7,9 л зеркало с объединением выдало 1,85 л) с последующей сваркой через combine и проверкой, что объём примерно удвоился; `whats_wrong()` — ошибки перестроения, которые SolidWorks знает, когда `Insert*` вернул голый `None`.

**Flow Simulation** (1.5.0). Нужны надстройка Flow Simulation и её API SDK (`<папка Flow>\API\flow simulation api sdk x64.exe`; он регистрирует `NIKCommonApi2`, перезагрузка не нужна). Документ должен быть сохранён: проект Flow лежит рядом с файлом.

| Инструмент | Что делает |
|---|---|
| `flow_create_project(name, template="internal_water")` | проект из шаблона `.fwp` (имя в папке шаблонов Flow или полный путь); расчётная область подгоняется под модель автоматически. Тип задачи и жидкость задаёт шаблон: с Flow идёт только `internal_water`, шаблон для внешнего обтекания воздухом делается один раз в мастере Flow (затем «Создать шаблон») |
| `flow_add_boundary_condition(kind, faces, value=None, name=None)` | входы `inlet_velocity` (м/с), `inlet_mass_flow` (кг/с), `inlet_volume_flow` (м³/с), `inlet_mach`, `inlet_mass_flux`; выходы `outlet_*`; давления `static_pressure`, `total_pressure`, `environment_pressure` (Па); стенки `real_wall`, `real_external_wall`, `ideal_wall` (без значения). Внутренней задаче нужно хотя бы одно условие давления |
| `flow_add_goal(kind, faces, value="average", name=None)` | поверхностная цель: давления, температура, плотность, скорость, расходы, силы и моменты по осям. `value` — `average`/`min`/`max`/`bulk_average`; у интегральных целей (силы, расходы, моменты) его нет |
| `flow_set_conditions(velocity_mps, pressure_pa, temperature_k, mesh_level)` | начальные условия (для внешней задачи — набегающий поток) и уровень автоматической сетки 1…7 |
| `flow_solve(timeout_s=120)` | сохраняет документ, строит сетку, считает, возвращает цели `{name, value, unit}` в СИ и загружает результаты в SolidWorks (узел «Результаты» Flow — там строятся эпюры; сам `Solve` их не загружает). Долгий расчёт отвечает `{job_id, state: running}` → `get_job`. Отказ Flow приходит с его причиной из `%TEMP%\ngp.log` |
| `flow_goals(service=False)`, `flow_info()` | значения целей последнего расчёта; проекты документа, активный проект, его условия и цели |

Грани: `'#5'` (индекс `list_faces`), `'+x'` / `'+x:inner'`, `'all'`, или `['selection']` — то, что выделено в SolidWorks.

Как устроено: все вызовы Flow идут через NIK API в отдельном процессе `rbr/flow_worker.py` — эта библиотека загружается в вызывающий процесс и роняет его access violation (чтение любого параметра расчётной области), так что падает воркер, а не сервер. На время вызова включается тихий режим Flow (`SetSilent` классического API): иначе вопросы «сбросить расчётную область?» / «скорректировать сетку?» ждут клика в SolidWorks. Классический API `FloWorks.App` для построения проекта не годится: выходы, давления и тип цели им не задаются, а `Solve` молча отказывает. Проверено на Flow Simulation 2025 SP3: полая коробка 100×50×20 (стенка 3 мм, вода), вход 1 м/с, выход — давление окружающей среды: расчёт 7 с, расход на выходе −6,16·10⁻⁴ м³/с = 1 м/с × 44×14 мм².

**Система координат эскизов** стандартных плоскостей RU-шаблона («Спереди», «Сверху», «Справа»):
- Спереди: (sx, sy) = (X, Y);
- Справа: (sx, sy) = (−Z, Y);
- Сверху: (sx, sy) = (X, −Z).

`InsertRefPlane`: флаг разворота плоскости — `8 + 256` (reverse + distance); `ReverseDirection` через `ModifyDefinition` плоскость НЕ переворачивает — пересоздавайте с флагом.

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

numpy для скриптов (`np` в `run_script`): `uv tool install --force --editable <путь> --with numpy`.

Обновление: `git -C $HOME\source\SolidWorks-MCP pull` и перезапуск клиента (editable — переустановка не нужна).

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SWMCP_HOME` | `%LOCALAPPDATA%\solidworks-mcp` | журнал и пользовательские скрипты |
| `SWMCP_SCRIPTS` | `<SWMCP_HOME>\scripts` | папка библиотеки скриптов |
| `SWMCP_NEW_DOC_UNITS` | `MMGS` | единицы новых документов; `off` — не трогать |
| `SWMCP_LOCK_WAIT_S` | `30` | ожидание межпроцессной блокировки, с |
| `SWMCP_OWNER_MIN` | `10` | время владения документом, мин; `0` — выключить |
| `SWMCP_BUSY_FAIL_S` | `20` | после скольких секунд текущего вызова новые получают отказ |
| `SWMCP_SYNC_WAIT_S` | `50` | сколько синхронный `run_script` ждёт до ответа `{job_id, state: running}` |
| `SWMCP_DOCS_WARN` | `30` | порог предупреждения server_health по открытым документам |
| `SWMCP_MEMORY_WARN_PCT` | `70` | порог предупреждения по памяти SLDWORKS.exe, % от RAM |

## Тесты

```powershell
uv run --extra dev pytest -m "not solidworks"      # Windows: оригинал + rbr
pytest tests/rbr --confcutdir=tests/rbr             # любая ОС: только rbr, на фейковом COM
```
