#!/bin/bash
# Desktop export runner — the thing the Dock/Desktop icon actually executes.
#
# Shows the Czech pre-flight overview (desktop/plan_cz.py), waits for a yes,
# then syncs server by server in the order the overview printed: the priority
# Discord first, backfill after. Splitting the run that way means an abort or a
# dead token still leaves the channels you care about up to date.
#
#   export.command [--debug] [--yes] [--workspace DIR]
#
# The workspace is the folder holding channels.yaml; the exports land wherever
# its `output_dir` points. Set it via --workspace, $DCE_WORKSPACE, or let the
# app bundle pass it in.
set -uo pipefail

# Finder hands GUI apps a bare PATH — Homebrew's python3 and the DCE.Cli shim
# both live outside it and would silently not resolve.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${DCE_WORKSPACE:-}"
DEBUG=0
ASSUME_YES=0
JOBS="${DCE_JOBS:-3}"

while [ $# -gt 0 ]; do
  case "$1" in
    --debug)     DEBUG=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --jobs|-j)   JOBS="$2"; shift 2 ;;
    *) echo "neznámý přepínač: $1" >&2; exit 2 ;;
  esac
done

teal() { printf '\033[38;5;43m%s\033[0m\n' "$1"; }
warn() { printf '\033[38;5;214m%s\033[0m\n' "$1"; }
bad()  { printf '\033[38;5;203m%s\033[0m\n' "$1"; }

# Any exit from here on leaves the window up — a launcher that vanishes on
# error tells you nothing.
hold() {
  echo
  printf '\033[2m─── okno zůstane otevřené · zavři ho Cmd+W ───\033[0m\n'
  read -r -p "" _ 2>/dev/null || true
}

die() { echo; bad "✗ $1"; hold; exit 1; }

if [ "$DEBUG" = 1 ]; then
  echo "── DEBUG ────────────────────────────────────────────"
  echo "  repo:      $REPO"
  echo "  workspace: $WORKSPACE"
  echo "  python3:   $(command -v python3 || echo '(nenalezen)')"
  echo "  dce.cli:   $(command -v discordchatexporter || echo '(nenalezen)')"
  echo "  jobs:      $JOBS"
  echo "  PATH:      $PATH"
  echo "─────────────────────────────────────────────────────"
  echo
  set -x
fi

[ -n "$WORKSPACE" ] || die "Není zadaná složka s channels.yaml (--workspace nebo \$DCE_WORKSPACE)."
[ -d "$WORKSPACE" ] || die "Složka neexistuje:\n  $WORKSPACE\n\nJe připojený disk SSD 990 PRO?"
[ -f "$WORKSPACE/channels.yaml" ] || die "V $WORKSPACE není channels.yaml."
[ -f "$REPO/dce_sync.py" ] || die "Nenašel jsem dce_sync.py v $REPO."
command -v python3 >/dev/null || die "python3 není k dispozici."

cd "$WORKSPACE" || die "Nelze otevřít $WORKSPACE."

PLAN="$REPO/desktop/plan_cz.py"
DCE=("python3" "$REPO/dce")

clear
python3 "$PLAN" --config channels.yaml --check-updates || die "Přehled selhal."

# Nothing to do — say so plainly instead of starting an empty sync.
PLAN_LINES="$(python3 "$PLAN" --config channels.yaml --emit-plan)"
if [ -z "$PLAN_LINES" ]; then
  teal "✓ Všechny kanály jsou aktuální — není co stahovat."
  hold
  exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
  printf '\033[1m  Spustit stahování? \033[0m\033[2m[Enter = ano · q = konec]\033[0m '
  read -r answer
  case "$answer" in
    q|Q|n|N) echo; teal "Zrušeno, nic se nestáhlo."; exit 0 ;;
  esac
fi
echo

# Snapshot the archive so the closing summary can report what is genuinely new.
OUT_DIR="$(REPO="$REPO" python3 - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
from dce_sync import load_config, output_dir_from_cfg
p = Path("channels.yaml").resolve()
print(output_dir_from_cfg(load_config(p), p))
PY
)" || die "Nepodařilo se zjistit output_dir."
BEFORE="$(mktemp -t dce-before)"
find "$OUT_DIR" -maxdepth 1 -type f -print0 2>/dev/null \
  | xargs -0 stat -f '%N|%z' 2>/dev/null | sort > "$BEFORE"

START=$(date +%s)
FAILED_GROUPS=()

while IFS=$'\t' read -r server channels; do
  [ -n "$channels" ] || continue
  echo
  teal "▶  $server"
  printf '\033[2m   %s\033[0m\n' "$channels"
  echo
  # shellcheck disable=SC2086 — channels is a deliberate word list
  if ! "${DCE[@]}" sync $channels -j "$JOBS" --retries 2 --watch 20; then
    FAILED_GROUPS+=("$server")
    warn "   ⚠ $server: část kanálů selhala (pokračuji dál)"
  fi
done <<< "$PLAN_LINES"

ELAPSED=$(( $(date +%s) - START ))
echo
printf '\033[38;5;43m%s\033[0m\n' "── HOTOVO ───────────────────────────────────────────────────────────────────────────────"
REPO="$REPO" OUT_DIR="$OUT_DIR" BEFORE="$BEFORE" ELAPSED="$ELAPSED" python3 - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
sys.path.insert(0, os.path.join(os.environ["REPO"], "desktop"))
from dce_sync import _human_size
from plan_cz import plural

out = Path(os.environ["OUT_DIR"])
before = {}
for line in Path(os.environ["BEFORE"]).read_text().splitlines():
    name, _, size = line.rpartition("|")
    if name:
        before[name] = int(size)

now = {str(f): f.stat().st_size for f in out.iterdir() if f.is_file()}
new = {n: s for n, s in now.items() if n not in before}
grown = {n: s - before[n] for n, s in now.items() if n in before and s > before[n]}

el = int(os.environ["ELAPSED"])
print(f"  Trvalo         {f'{el // 60} min {el % 60} s' if el >= 60 else f'{el} s'}")
print(f"  Archiv         {len(now)} {plural(len(now), 'soubor', 'soubory', 'souborů')}"
      f" · {_human_size(sum(now.values()))}")
if new:
    print(f"  Nové soubory   {len(new)} {plural(len(new), 'soubor', 'soubory', 'souborů')}"
          f" · {_human_size(sum(new.values()))}")
    for n in sorted(new):
        print(f"    \033[38;5;43m{Path(n).name}\033[0m  \033[2m{_human_size(new[n])}\033[0m")
if grown:
    print(f"  Zvětšené       {len(grown)}")
    for n in sorted(grown):
        print(f"    {Path(n).name}  \033[2m+{_human_size(grown[n])}\033[0m")
if not new and not grown:
    print("  \033[2mŽádná nová data — kanály už byly aktuální.\033[0m")
print(f"\n  Vše leží v: {out}")
PY
rm -f "$BEFORE"

if [ ${#FAILED_GROUPS[@]} -gt 0 ]; then
  echo
  warn "⚠ Neúspěšné servery: ${FAILED_GROUPS[*]}"
  warn "  Nejčastější příčina je propadlý token → dce token set <NOVÝ>"
fi

echo
printf '\033[1m  Otevřít složku s exporty ve Finderu? \033[0m\033[2m[Enter = ano · q = ne]\033[0m '
read -r open_answer
case "$open_answer" in
  q|Q|n|N) ;;
  *) open "$OUT_DIR" ;;
esac

hold
