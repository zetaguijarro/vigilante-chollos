import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from vigilante import ronda
ronda('juguetes_bebe')
