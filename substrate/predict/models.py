"""Small, transparent models: L2-regularized logistic regression (direction) and
ridge regression (magnitude). Features are standardized on the training window
only, so nothing from the future leaks into a fit."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CLIP = 3.0


@dataclass
class Scaler:
    mean: np.ndarray
    sd: np.ndarray

    @classmethod
    def fit(cls, X: np.ndarray) -> "Scaler":
        sd = X.std(axis=0)
        return cls(X.mean(axis=0), np.where(sd > 1e-9, sd, 1.0))

    def __call__(self, X: np.ndarray) -> np.ndarray:
        # Cap at 3 sd: 2026-style moves sit far outside history, and an uncapped
        # linear model extrapolates them into absurd certainty.
        return np.clip((X - self.mean) / self.sd, -CLIP, CLIP)


def _design(Z: np.ndarray) -> np.ndarray:
    return np.hstack([np.ones((Z.shape[0], 1)), Z])


@dataclass
class Logistic:
    scaler: Scaler
    w: np.ndarray

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, l2: float = 2.0, iters: int = 30) -> "Logistic":
        sc = Scaler.fit(X)
        A = _design(sc(X))
        w = np.zeros(A.shape[1])
        pen = np.full(A.shape[1], l2)
        pen[0] = 0.0                                   # intercept is not shrunk
        for _ in range(iters):                         # Newton-Raphson
            p = 1 / (1 + np.exp(-np.clip(A @ w, -30, 30)))
            g = A.T @ (p - y) + pen * w
            H = (A * (p * (1 - p))[:, None]).T @ A + np.diag(pen) + 1e-9 * np.eye(len(w))
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-7:
                break
        return cls(sc, w)

    def prob(self, X: np.ndarray) -> np.ndarray:
        return 1 / (1 + np.exp(-np.clip(_design(self.scaler(X)) @ self.w, -30, 30)))

    def contributions(self, x: np.ndarray, names: list[str]) -> list[dict]:
        """Log-odds contribution of each feature for one row (positive = pushes 'up')."""
        z = self.scaler(x[None, :])[0]
        return [{"feature": n, "value": float(v), "logodds": float(c)}
                for n, v, c in zip(names, x, z * self.w[1:])]


@dataclass
class Ridge:
    scaler: Scaler
    w: np.ndarray

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, l2: float = 5.0) -> "Ridge":
        sc = Scaler.fit(X)
        A = _design(sc(X))
        pen = np.full(A.shape[1], l2)
        pen[0] = 0.0
        w = np.linalg.solve(A.T @ A + np.diag(pen), A.T @ y)
        return cls(sc, w)

    def predict(self, X: np.ndarray) -> np.ndarray:
        return _design(self.scaler(X)) @ self.w


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))
