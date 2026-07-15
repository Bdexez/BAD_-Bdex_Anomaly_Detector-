"""Suite de tests : `python -m unittest discover -s tests -t .`

Les readers loggent volontairement leurs pannes ; on ne veut pas de ce bruit
dans la sortie des tests, seulement les échecs d'assertions.
"""

import logging

logging.disable(logging.CRITICAL)
