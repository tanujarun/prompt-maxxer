"""Voice-band spectrum for the settings visualizer.

Sixteen log-spaced bands from 90 Hz to 7.5 kHz: the range where speech has its
energy, spaced the way hearing is, so each column covers a similar musical
interval rather than low pitches crowding into one bar.

Each band's height combines two things. Shape: how strong the band is relative
to the strongest band in the same moment. Loudness: how loud the moment is
overall. Multiplying them keeps a quiet room flat (low loudness) while speech
lights the bands where the voice actually is (shape), and makes the display far
less sensitive to microphone gain than absolute levels would be.
"""

from __future__ import annotations

import numpy as np

BANDS = 16
LOW_HZ = 90.0
HIGH_HZ = 7500.0
#: Analysis window. Long enough to resolve the lowest band, short enough that
#: the bars still move with syllables.
WINDOW_S = 0.064

#: Overall RMS level that maps to empty and to full height.
LOUDNESS_FLOOR_DB = -55.0
LOUDNESS_CEILING_DB = -20.0
#: How far below the strongest band a band can sit and still show at all.
#: Tuned on speech: at 36 dB nearly every band stayed lit and the bottom rows
#: read as a solid wall; 24 dB lets the weaker bands drop out, so the display
#: shows where the voice actually is.
SHAPE_RANGE_DB = 24.0


class BandAnalyzer:
    def __init__(self, sample_rate: int, bands: int = BANDS) -> None:
        self.sample_rate = sample_rate
        self.size = 1 << int(np.ceil(np.log2(sample_rate * WINDOW_S)))
        self._buffer = np.zeros(self.size, dtype=np.float32)
        self._window = np.hanning(self.size).astype(np.float32)

        freqs = np.fft.rfftfreq(self.size, 1.0 / sample_rate)
        high = min(HIGH_HZ, sample_rate / 2 * 0.95)
        edges = np.geomspace(LOW_HZ, high, bands + 1)
        self._bins: list[np.ndarray] = []
        for low, top in zip(edges[:-1], edges[1:]):
            idx = np.flatnonzero((freqs >= low) & (freqs < top))
            if idx.size == 0:
                # The narrowest low bands can fall between two FFT bins.
                idx = np.array([int(np.argmin(np.abs(freqs - (low + top) / 2)))])
            self._bins.append(idx)

    def feed(self, samples: np.ndarray) -> None:
        """Append the newest samples, keeping the most recent window."""
        n = len(samples)
        if n >= self.size:
            self._buffer[:] = samples[-self.size :]
        elif n:
            self._buffer[:-n] = self._buffer[n:]
            self._buffer[-n:] = samples

    def bands(self) -> list[float]:
        """Band heights for the current window, each 0 to 1."""
        rms = float(np.sqrt(np.mean(np.square(self._buffer))))
        loudness_db = 20.0 * np.log10(rms + 1e-9)
        loudness = (loudness_db - LOUDNESS_FLOOR_DB) / (LOUDNESS_CEILING_DB - LOUDNESS_FLOOR_DB)
        if loudness <= 0:
            return [0.0] * len(self._bins)
        loudness = min(1.0, loudness)

        power = np.abs(np.fft.rfft(self._buffer * self._window)) ** 2
        # Summing, not averaging, each band's bins: log-spaced bands widen
        # toward the top, so summing tilts the result the way speech needs,
        # instead of letting its naturally weaker high frequencies vanish.
        energy_db = 10.0 * np.log10(np.array([power[idx].sum() for idx in self._bins]) + 1e-12)
        shape = np.clip((energy_db - energy_db.max() + SHAPE_RANGE_DB) / SHAPE_RANGE_DB, 0.0, 1.0)
        return [round(float(v), 2) for v in shape * loudness]
