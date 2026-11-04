"""Scorers and candidate generation.

Importing the package registers the built-in scorers, so `autoclipper.models.base.load_scorer`
and `make_scorer` find them whichever module is imported first.
"""

from autoclipper.models import baselines as baselines
