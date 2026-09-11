"""Explicit bounded adapters around installed Qlib model and dataset interfaces.

Optional imports are confined here. No alternate model is substituted on failure.
"""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd
from qlib.contrib.model.gbdt import LGBModel
from qlib.contrib.model.linear import LinearModel
from qlib.data.dataset import Dataset
from sklearn.linear_model import Ridge


class ArrayDataset(Dataset):
    """Verified finite matrix; preserve all prediction rows and avoid Handler copies."""

    def __init__(self, x, y=None, index=None):
        super().__init__()
        self._x, self._y, self._index = x, y, index
        self.segments = {"test": None} if y is None else {"train": None}

    def prepare(self, segment, col_set="feature", **kwargs):
        if segment not in self.segments or col_set != "feature":
            raise ValueError("only explicit feature-only prediction preparation is supported")
        return pd.DataFrame(self._x, index=self._index, copy=False)


class PinnedRidge(LinearModel):
    """Qlib LinearModel prediction/state, explicit sklearn Ridge fitting parameters."""

    def __init__(self, params):
        super().__init__(estimator="ridge", alpha=params["alpha"], fit_intercept=True)
        self.params = params

    def fit(self, dataset, reweighter=None):
        if reweighter is not None:
            raise ValueError("fixed unweighted protocol")
        model = Ridge(**self.params)
        model.fit(dataset._x, dataset._y)
        self.coef_, self.intercept_ = model.coef_, model.intercept_
        self.n_iter_ = np.asarray(model.n_iter_).tolist()
        return self


class BoundedLGBModel(LGBModel):
    """Native Qlib fit/logging/predict; direct arrays avoid whole-frame duplication."""

    def _prepare_data(self, dataset, reweighter=None):
        if reweighter is not None or set(dataset.segments) != {"train"}:
            raise ValueError("fixed training only; validation and reweighting are disabled")
        return [(lgb.Dataset(dataset._x, label=dataset._y), "train")]


def make_model(kind, config):
    if kind == "ridge":
        return PinnedRidge(config[kind])
    if kind == "lightgbm":
        return BoundedLGBModel(**config[kind])
    raise ValueError("model outside fixed two-model contract")
