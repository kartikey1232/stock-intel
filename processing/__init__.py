"""Turns raw collected data into insight (indicators, sentiment, signals)."""

import warnings

# pandas-ta 0.4.x sets a pandas option that pandas 3 deprecates; harmless, so silence it.
# Lives here because this package is the only importer of pandas_ta.
warnings.filterwarnings("ignore", message=".*copy_on_write.*")
