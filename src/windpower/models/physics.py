"""Site-equivalent cubic power curve; parameters are not turbine specifications."""
from dataclasses import dataclass, asdict
import numpy as np
from windpower.features.build import C

@dataclass(frozen=True)
class PhysicsCurve:
    cut_in: float
    rated: float
    plateau: float

    def __post_init__(self):
        if not (np.isfinite([self.cut_in, self.rated, self.plateau]).all()
                and 0 <= self.cut_in < self.rated and 0 <= self.plateau <= 1):
            raise ValueError('Need 0 <= cut_in < rated and 0 <= plateau <= 1')

    def predict(self, speed):
        speed = np.asarray(speed, dtype=float)
        if not np.isfinite(speed).all() or (speed < 0).any():
            raise ValueError('Wind speed must be finite and nonnegative')
        # Saturate speed before cubing; no assumed cut-out without turbine metadata.
        s = np.clip(speed, self.cut_in, self.rated)
        return self.plateau * (s**3-self.cut_in**3)/(self.rated**3-self.cut_in**3)

    def parameters(self):
        return asdict(self)

    @classmethod
    def fit(cls, speed, actual, settings):
        """Training arrays only. Bounded grid, analytical LS plateau, deterministic ties."""
        speed, actual = np.asarray(speed, float), np.asarray(actual, float)
        if speed.ndim != 1 or speed.shape != actual.shape or not len(speed):
            raise ValueError('Need equally sized nonempty one-dimensional arrays')
        if not np.isfinite(actual).all() or (actual < 0).any() or (actual > 1).any():
            raise ValueError('Training labels must be finite normalized power')
        step = settings['grid_step']
        if step <= 0 or not 0 <= settings['plateau_min'] <= settings['plateau_max'] <= 1:
            raise ValueError('Invalid grid or plateau bounds')
        cuts = np.arange(settings['cut_in_min'], settings['cut_in_max']+step/2, step)
        rateds = np.arange(settings['rated_min'], settings['rated_max']+step/2, step)
        best = None
        for cut in cuts:
            for rated in rateds:
                if rated <= cut: continue
                basis = cls(float(cut), float(rated), 1.).predict(speed)
                denom = float(basis @ basis)
                plateau = np.clip(float(basis @ actual)/denom if denom else settings['plateau_min'],
                                  settings['plateau_min'], settings['plateau_max'])
                loss = float(np.mean((plateau*basis-actual)**2))
                if best is None or loss < best[0]:
                    best = loss, cls(float(cut), float(rated), float(plateau))
        if best is None: raise ValueError('No feasible cut-in/rated pair')
        return best[1]

@dataclass
class PhysicsGuidedModel:
    mode: str
    curve: PhysicsCurve | None = None
    estimator: object = None

    def physics(self, x):
        x = np.asarray(x, float)
        return self.curve.predict(np.hypot(x[:, C.index('u100')], x[:, C.index('v100')]))

    def predict(self, x):
        x = np.asarray(x, float)
        if self.mode == 'baseline': raw = self.estimator.predict(x)
        elif self.mode == 'physics': raw = self.physics(x)
        elif self.mode == 'residual': raw = self.physics(x) + self.estimator.predict(x)
        elif self.mode == 'injected':
            raw = self.estimator.predict(np.column_stack([x, self.physics(x)]))
        else: raise ValueError(f'Unknown mode {self.mode}')
        # Residual itself is never clipped: negative corrections are essential.
        return np.clip(raw, 0, 1)
