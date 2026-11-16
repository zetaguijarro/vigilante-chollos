import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from vigilante import ronda_grupo
ronda_grupo('navegador')
