# Plan: memdoctor (versión Karpathy)

~200 LOC, 3 signals, 1 commit. 0 deps nuevas. 0 persistencia. Cortado del plan original tras review.

## Qué hace claude-doctor

Parsea `~/.claude/projects/<encoded>/*.jsonl`, detecta 12 señales de fricción y genera reglas para CLAUDE.md. Repo: millionco/claude-doctor (MIT).

## Qué portamos

**3 signals** (no 12). El resto es decoration o proxy ruidoso de estos:

| Signal | Detección | ¿Por qué éste? |
|---|---|---|
| `correction-heavy` | ≥20% user msgs matchean CORRECTION_PATTERNS | "El modelo se equivoca seguido" |
| `error-loop` | ≥3 consecutive tool_results con is_error=true | "El modelo atascado" |
| `keep-going-loop` | ≥2 msgs matchean KEEP_GOING_PATTERNS | "El modelo no termina" |

**Cortados del scope:**
- `rapid-corrections` — proxy ruidoso de correction-heavy (si corrige rápido, ya matcheó el regex)
- `repeated-instructions` — Jaccard O(n²) para señal difusa
- `negative-drift`, `high-turn-ratio` — weighted scoring sin ROI claro
- `negative-sentiment` (AFINN) — 2400 palabras calibradas para Amazon reviews; SENTINEL_TOKENS custom de 15 tokens ya capturan el 90% del sentiment de coding
- `abandonment`, `restart-cluster` — cross-session, fuera de scope del MVP
- `edit-thrashing`, `excessive-exploration` — ya cubiertos parcialmente por mempatterns

## Arquitectura

**1 archivo**: `tools/memdoctor.py` (~200 LOC)

```python
# Constants
CORRECTION_PATTERNS = [...]           # port desde constants.ts
KEEP_GOING_PATTERNS = [...]
META_MESSAGE_PATTERNS = [...]          # filter out
ERROR_LOOP_THRESHOLD = 3
CORRECTION_RATE_THRESHOLD = 0.2
MIN_CORRECTIONS_TO_FLAG = 2
KEEP_GOING_MIN_TO_FLAG = 2

RULES_MAP = {
    "correction-heavy": "When the user corrects you, stop and re-read their message...",
    "error-loop": "After 2 consecutive tool failures, change your approach entirely.",
    "keep-going-loop": "Complete the FULL task before stopping.",
}

# Functions (no classes)
def parse_sessions(project_filter: str | None = None) -> Iterator[dict]:
    """Walk ~/.claude/projects/<encoded>/*.jsonl, yield dicts with msgs + tool_results."""

def detect_correction_heavy(session: dict) -> str | None: ...
def detect_error_loop(session: dict) -> str | None: ...
def detect_keep_going(session: dict) -> str | None: ...

def detect_signals(session: dict) -> list[str]:
    return [s for s in (detect_correction_heavy(session), detect_error_loop(session), detect_keep_going(session)) if s]

def format_rules(signals: set[str]) -> str:
    return "\n".join(f"- {RULES_MAP[s]}" for s in sorted(signals))

def main() -> int:
    # parse args, iterate sessions, aggregate signals, print summary + optional --rules block
```

**Sin clases**: `DoctorOrchestrator`, `SignalDetector`, `RulesGenerator` del plan original — colapsadas a funciones.

## CLI

**1 comando**: `engram doctor [--project=slug] [--rules]`

Sin `--save`, `--apply`, `--json`, `--session-id`. Esas flags se agregan en v2 si hacen falta.

## Parser

**Reusar `memcapture.TranscriptParser.parse_file()` solo para text extraction.** No refactorizar a módulo compartido — premature generalization. Si el detector necesita algo que `SessionData` no expone (ej: timestamps por turno), hacer un mini-parser propio dentro de `memdoctor.py` y listo.

## Lo que NO hacemos

- ❌ `~/.claude/doctor/model.json` — no hay caso de uso sin banner
- ❌ `guidance.md` persistido — corre on-demand
- ❌ `--apply` escribiendo a `~/.claude/CLAUDE.md` — riesgoso, el user tiene CLAUDE.md curado
- ❌ Hook automático (PreCompact/Stop) — complejidad sin validar
- ❌ Integración banner SessionStart — agregar en v2 si se usa
- ❌ AFINN dep o dict estático 2400 palabras
- ❌ Refactor de `TranscriptParser` a módulo shared

## Tests

Copiar 3 fixtures de `/tmp/claude-doctor/packages/cli/tests/fixtures/`:
- `correction-heavy-session.jsonl`
- `error-loop-session.jsonl`
- `keep-going-session.jsonl`
- `happy-session.jsonl` (negative control)

`tests/test_memdoctor.py` — 1 test por detector. ~50 LOC total.

## Atribución

Header en `memdoctor.py`:
```python
# Signal detection ported from millionco/claude-doctor (MIT)
# https://github.com/millionco/claude-doctor
```

## Estimación

- Implementación + tests: **3-4 horas**
- Total: 1 commit, ~200 LOC de código + ~50 LOC de tests

## Próximos pasos

1. Implementar `tools/memdoctor.py` con TDD usando las 4 fixtures
2. Sumar `"doctor"` al DISPATCH dict de `engram.py` cuando se haga el refactor pendiente
3. Mini sección en README: "engram doctor — detectá fricción en tus sesiones"

## v2 (si el uso valida)

- `--rules --apply` (con backup de CLAUDE.md)
- `--save` a `~/.claude/doctor/model.json` para banner cacheado
- +2 signals: `rapid-corrections`, `excessive-exploration`
- Banner SessionStart con top signal del proyecto actual
