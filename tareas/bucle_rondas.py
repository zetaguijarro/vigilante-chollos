"""Rondas automáticas cada 30 minutos dentro del horario (Zeta, 21/11/2026: "mañana las programas desde las 8, por cada
ronda tiene que pasar 30 minutos").

- Una ronda = rápidas y después navegador, cada una en su propio proceso (como las tareas antiguas).
- Empieza a las 08:00 (o al momento, si ya es horario) y lanza una ronda cada 30 minutos contados desde que empezó la
  anterior; si una ronda tarda más de 30 minutos, la siguiente empieza al acabar.
- Fuera del horario de la configuración (08:00-00:45) espera.
- Para pararlo: borrar data/bucle.on (o `vigilante.py bucle parar`). Lo que hace queda en data/bucle.log.
"""
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import vigilante as v  # noqa: E402

CADA_MIN = 30
ENCENDIDO = v.DATOS / "bucle.on"
REGISTRO = v.DATOS / "bucle.log"


def apuntar(texto):
    with open(REGISTRO, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {texto}\n")


def una_ronda():
    for script in ("vig_rapidas.py", "vig_navegador.py"):
        inicio = time.time()
        r = subprocess.run([sys.executable, str(RAIZ / "tareas" / script)], cwd=str(RAIZ), capture_output=True, text=True)
        apuntar(f"{script}: salida {r.returncode} en {round(time.time() - inicio)} s")
        if r.returncode != 0:
            apuntar((r.stderr or "")[-500:])
            v.alerta_sistema(f"bucle:{script}", f"⛔ La ronda {script} terminó con error {r.returncode}. Mira data/bucle.log.")


def main(desde="08:00"):
    ENCENDIDO.write_text(datetime.now().isoformat(timespec="minutes"), encoding="utf-8")
    h, m = (int(x) for x in desde.split(":"))
    arranque = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    if arranque < datetime.now() and not v.en_horario(v.cargar_config()):
        arranque += timedelta(days=1)
    apuntar(f"bucle encendido: primera ronda a partir de {arranque:%d/%m %H:%M}, una cada {CADA_MIN} min")
    while ENCENDIDO.exists():
        ahora = datetime.now()
        if ahora < arranque or not v.en_horario(v.cargar_config()):
            time.sleep(30)
            continue
        inicio = datetime.now()
        apuntar("ronda: empieza")
        try:
            una_ronda()
        except Exception as e:
            apuntar(f"ronda: error {e}")
        siguiente = inicio + timedelta(minutes=CADA_MIN)
        apuntar(f"ronda: termina; la siguiente a las {siguiente:%H:%M}")
        while ENCENDIDO.exists() and datetime.now() < siguiente:
            time.sleep(20)
    apuntar("bucle apagado (falta data/bucle.on)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "08:00")
