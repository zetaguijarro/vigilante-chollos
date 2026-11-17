#!/usr/bin/env bash
# Copia a la carpeta de Hermes las dos habilidades y el plugin del botón. Lanzar desde Git Bash, en la
# raíz del proyecto:  bash scripts/instalar_en_hermes.sh
# Después hay que REINICIAR Hermes: carga habilidades y plugins al arrancar.
set -e
HERMES="${HERMES_DIR:-/c/Users/Zeus/AppData/Local/hermes}"
[ -d "$HERMES" ] || { echo "No encuentro la carpeta de Hermes en $HERMES (pon HERMES_DIR=...)"; exit 1; }
for s in verifica-chollo redacta-chollo; do
  mkdir -p "$HERMES/skills/$s"
  cp "hermes/skills/$s/SKILL.md" "$HERMES/skills/$s/SKILL.md"
  echo "habilidad $s instalada"
done
mkdir -p "$HERMES/plugins/vigilante-botones"
cp hermes/plugins/vigilante-botones/__init__.py hermes/plugins/vigilante-botones/plugin.yaml "$HERMES/plugins/vigilante-botones/"
echo "plugin vigilante-botones instalado"
echo "Ahora reinicia Hermes."
