"""Rondas automáticas cada 30 minutos dentro del horario (Zeta, 21/11/2026: "mañana las programas desde las 8, por cada
ronda tiene que pasar 30 minutos").

- Una ronda = rápidas y navegador a la vez, cada una en su propio proceso (desde el 21/11/2026 por la tarde).
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


def rondas_vivas():
    """Procesos de ronda (vig_rapidas / vig_navegador) que siguen en marcha: de un bucle anterior o lanzados a mano."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object "
                            "{ $_.CommandLine -match 'vig_rapidas|vig_navegador' }).Count"],
                           capture_output=True, text=True, timeout=60)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def una_ronda():
    """Rápidas y navegador A LA VEZ (Zeta, 21/11/2026): no compiten (una descarga, la otra usa Chrome; la rápida nunca
    toca el Chrome del perfil) y la base admite escrituras simultáneas (WAL). La ronda dura lo que la más larga."""
    inicio = time.time()
    procesos = {}
    for script in ("vig_navegador.py", "vig_rapidas.py"):
        err = open(v.DATOS / f"bucle_{script}.err", "w", encoding="utf-8")
        procesos[script] = (subprocess.Popen([sys.executable, str(RAIZ / "tareas" / script)], cwd=str(RAIZ),
                                             stdout=subprocess.DEVNULL, stderr=err, text=True), err)
    for script, (proc, err) in procesos.items():
        codigo = proc.wait()
        err.close()
        apuntar(f"{script}: salida {codigo}, terminó a los {round(time.time() - inicio)} s de empezar la ronda")
        if codigo != 0:
            apuntar((v.DATOS / f"bucle_{script}.err").read_text(encoding="utf-8", errors="replace")[-500:])
            v.alerta_sistema(f"bucle:{script}", f"⛔ La ronda {script} terminó con error {codigo}. Mira data/bucle.log.")


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
        if rondas_vivas():  # otra ronda sigue en marcha: no se lanza una encima
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
