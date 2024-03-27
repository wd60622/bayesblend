from __future__ import annotations

import typing
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Hashable, List, Literal, Sequence, Set, Tuple, Union

import numpy as np
import pandas as pd
from cmdstanpy import CmdStanMCMC, CmdStanModel
from scipy.optimize import OptimizeResult, minimize
from scipy.stats import dirichlet

STACKING_MODEL = (
    Path(__file__).parent.resolve().joinpath("stan_files").joinpath("stacking.stan")
)

HIER_STACKING_MODEL = (
    Path(__file__)
    .parent.resolve()
    .joinpath("stan_files")
    .joinpath("hierarchical_stacking.stan")
)

HIER_STACKING_MODEL_POOLING = (
    Path(__file__)
    .parent.resolve()
    .joinpath("stan_files")
    .joinpath("hierarchical_stacking_pooling.stan")
)

PointwiseDiagnostics = Dict[str, Sequence[float]]

ContinuousTransforms = Literal["identity", "standardize", "relu"]

CONTINUOUS_TRANSFORMS = list(typing.get_args(ContinuousTransforms))

CovariateInfo = Dict[str, Union[Set[Any], Dict[str, float]]]

Weights = Dict[str, np.ndarray]

Priors = Dict[str, Union[List[Union[float, int]], float, int]]

CMDSTAN_DEFAULTS = {
    "chains": 4,
    "parallel_chains": 4,
}

__all__ = [
    "MleStacking",
    "BayesStacking",
    "HierarchicalBayesStacking",
    "PseudoBma",
]


class BayesBlendModel(ABC):
    """Abstract base class for estimating stacking weights for blending model predictions.

    This ABC provides a template for all specific weighting models/methods,
    which all inherit the base class. Each subclass should have a defined
    `fit` method, and an optional `coefficients` property and `predict` method
    if the model has relevant coefficients that can be used to predict new weights
    given input covariates.

    Attributes:
        pointwise_diagnostics: Dictionary of pointwise diagnostics for each model to
            be weighted. The specific diagnostic requires varies across models. For example:
            `{"model1": [-10, ...], "model2": [...]}`.
    """

    def __init__(
        self,
        pointwise_diagnostics: PointwiseDiagnostics,
    ) -> None:
        self.pointwise_diagnostics = pointwise_diagnostics
        self._coefficients: Dict[str, np.ndarray]
        self._weights: Weights | None = None
        self._model_info: Union[OptimizeResult, CmdStanMCMC] | None = None

    @abstractmethod
    def fit(self) -> BayesBlendModel:
        """Fit the blending model."""
        pass

    @abstractmethod
    def predict(self) -> Weights:
        """Predict from the blending model."""
        pass

    @property
    def num_models(self) -> int:
        """Return the number of models being compared."""
        return len(self.pointwise_diagnostics)

    @property
    def model_name(self) -> str:
        """The model name."""
        return self.__class__.__name__

    @property
    def weights(self) -> Weights:
        """Return a dictionary of weights.

        For Bayesian models, this property returns the
        mean posterior weights for simplicty.
        """
        if self._weights is None:
            return {}
        return {
            model: np.mean(weight, axis=0, keepdims=True)
            for model, weight in self._weights.items()
        }

    @property
    def coefficients(self) -> Dict[str, np.ndarray]:
        """Return the model coefficients, if any."""
        if not self._coefficients:
            warnings.warn(f"{self.model_name} does have any coefficients.")
            return {}
        return {
            name: np.mean(value, axis=0, keepdims=True)
            for name, value in self._coefficients.items()
        }

    @property
    def model_info(self) -> Union[OptimizeResult, CmdStanMCMC] | None:
        """Return the fitted model information."""
        return self._model_info

    def blend(
        self, predictions: Dict[str, Dict[str, np.ndarray]], seed: int | None = None
    ):
        np.random.seed(seed)

        M = len(predictions)
        S = np.shape(list(list(predictions.values())[0].values())[0])[0]
        N = np.shape(list(list(predictions.values())[0].values()))[-1]

        # ensure same number of posterior samples
        for k, v in predictions.items():
            for par, s in v.items():
                if np.shape(s)[0] != S:
                    raise ValueError(
                        "The number of MCMC samples in `draws` is not consistent"
                    )

        # use array to deal with multiple possible weights across observations
        weight_array = np.concatenate(list(self.weights.values())).T
        draws_idx_list = [
            np.random.choice(list(range(M)), S, p=weights) for weights in weight_array
        ]

        if len(draws_idx_list) != 1 and len(draws_idx_list) != N:
            raise ValueError(
                "Dimensions of `weights` do not match those of `draws`. Either a single "
                "set of weights should be supplied that will be applied to all observations "
                "in `draws`, or exactly one set of weights for each observation in `draws` "
                "should be supplied for pointwise blending."
            )

        if len(draws_idx_list) == 1:
            draws_idx_list = draws_idx_list * N

        blend: Dict = defaultdict(List[float])
        blend_idx = {i: j for i, j in zip(predictions.keys(), range(M))}
        for k, v in predictions.items():
            blend_id = blend_idx[k]
            for par, s in v.items():
                blended_list = [
                    list(s[draws_idx == blend_id, idx])
                    for idx, draws_idx in enumerate(draws_idx_list)
                ]
                if par in blend:
                    curr = blend[par]
                    blend[par] = [c + b for c, b in zip(curr, blended_list)]
                else:
                    blend[par] = blended_list

        return {par: np.asarray(blend[par]).T for par in blend}


class MleStacking(BayesBlendModel):
    """Subclass to compute weights by MLE stacking.

    Constrained optimization is used to seek the set of model weights
    that maximises the log score (or minimizes the negative log score)
    across log predictive densities. Approximate log predictive densities
    can be obtained via PSIS-LOO or PSIS-LFO.

    Attributes:
        pointwise_diagnostics: As in the base `BayesBlendModel` class.
    """

    def __init__(
        self,
        pointwise_diagnostics: PointwiseDiagnostics,
        optimizer_options: Dict[str, Any] | None = None,
    ) -> None:
        self.optimizer_options = optimizer_options
        super().__init__(pointwise_diagnostics)

    def _obj_fun(self, w, *args):
        """Negative sum of the weighted log predictive densities"""
        Y = args[0]
        log_scores = np.log(Y @ w)
        return -sum(log_scores)

    def _grad(self, w, *args):
        """Gradient/jacobian of the objective function"""
        Y = args[0]
        N, K = Y.shape
        grad = np.diag(np.ones(N) / (Y @ w)) @ Y
        return -grad.sum(axis=0)

    def _eq_constraint(self, w):
        # constraint for the optimization:
        #    sum(w) == 1,
        # so sum(w) - 1 == 0
        return sum(w) - 1

    def fit(self) -> MleStacking:
        # get the raw log predictive densities, i.e. p(y_i | y_-i) from the
        # pointwise_diagnostics object
        lpd_points = np.asarray(list(self.pointwise_diagnostics.values())).T
        _, K = lpd_points.shape

        res = minimize(
            fun=self._obj_fun,
            jac=self._grad,
            args=(np.exp(lpd_points)),
            x0=np.repeat(1 / K, K),
            method="SLSQP",
            constraints=dict(type="eq", fun=self._eq_constraint),
            bounds=[(0, 1) for _ in range(K)],
            options=self.optimizer_options,
        )

        self._weights = {
            model: np.atleast_2d(weight)
            for model, weight in zip(self.pointwise_diagnostics.keys(), res.x)
        }
        self._model_info = res
        return self

    def predict(self) -> Weights:
        return self.weights


class BayesStacking(BayesBlendModel):
    """Subclass to compute weights by Bayesian stacking.

    MCMC is used to estimate the set of posterior model weights
    that maxismises the log score (or minimizes the negative log score)
    across log predictive densities.

    Attributes:
        pointwise_diagnostics: As in the base `BayesBlendModel` class.
        priors: Dictionary of (prior, values) to be passed to Stan.
        cmdstan_control: Dictionary of keyword arguments to send to cmdstan model
            sampling routine.
        seed: Random number seed to use for model fitting.
    """

    def __init__(
        self,
        pointwise_diagnostics: PointwiseDiagnostics,
        priors: Priors | None = None,
        cmdstan_control: Dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__(pointwise_diagnostics)
        self.cmdstan_control = (
            CMDSTAN_DEFAULTS
            if cmdstan_control is None
            else (cmdstan_control | CMDSTAN_DEFAULTS)
        )

        if priors is None:
            self._priors: Priors = {
                "w_prior": [1] * self.num_models,
            }
        else:
            if len(priors) != 1 or "w_prior" not in priors:
                raise ValueError(
                    "`priors` should be a dictionary of one key-value pair "
                    "of the weights `w_prior` and vector of shapes, e.g. ('w_prior', [1, 1, 1])."
                )
            if isinstance(priors["w_prior"], (float, int)):
                priors["w_prior"] = [priors["w_prior"]] * self.num_models
            elif len(priors["w_prior"]) != self.num_models:
                raise ValueError(
                    f"Length of `w_prior` prior vector ({len(priors['w_prior'])}) "
                    f"does not equal the number of models ({self.num_models})."
                )
            self._priors = priors

        self.seed = seed
        self.cmdstan_control["seed"] = self.seed

    def fit(self) -> BayesStacking:
        model = CmdStanModel(
            stan_file=STACKING_MODEL,
        )

        lpd_points = np.asarray(list(self.pointwise_diagnostics.values())).T
        N, M = lpd_points.shape

        fit = model.sample(
            data={
                "N": N,
                "M": M,
                "y": list(lpd_points),
                **self._priors,
            },
            **self.cmdstan_control,
        )

        self._weights = {
            model: np.atleast_2d(weight).T
            for model, weight in zip(
                self.pointwise_diagnostics.keys(), fit.stan_variable("w").T
            )
        }
        self._model_info = fit
        return self

    def predict(self) -> Weights:
        return self.weights

    @property
    def priors(self) -> Priors:
        return self._priors


class HierarchicalBayesStacking(BayesBlendModel):
    """Subclass to compute weights by hierarchical Bayesian stacking.

    MCMC is used to estimate the set of posterior pointwise model weights
    that maximizes the log score (or minimizes the negative log score)
    across log predictive densities, where pointwise model weights
    are a function of input covariates.

    Attributes:
        pointwise_diagnostics: As in the base `BayesBlendModel` class.
        discrete_covariates: Dictionary of covariate name and value pairs,
            where the value is a sequence with an element for each of the
            cells/points contained in each model in `pointwise_diagnostics`. Dummy
            codes are generated automatically.
        continuous_covariates: Dictionary of covariate name and value pairs,
            where the value is a sequence with an element for each of the
            cells/points contained in each model in `pointwise_diagnostics`. Values
            are entered as input to the hierarchical stacking model as-is.
        continuous_covariates_transform: The type of transform to use for
            continuous covariates. Must be one of CONTINUOUS_TRANSFORMS
            and defaults to 'standardize'. Note that continuous covariates
            are standardized by 2 times the standard deviation in line with
            Gelman (2008; Statistics in Medicine).
        partial_pooling: Bool specifying if partial pooling should be used when
            estimating discrete and continuous covariates.
        adaptive: Should the prior scale parameters adapt to the amount of
            data? This is useful in a low-data setting, where strong
            prior information might need to be weakened beyond what the data
            can update the likelihood directly.
        priors: Dictionary of (prior, values) to be passed to Stan. When
            `partial_pooling=True`, there are a few key priors that can be set
            to control pooling. "tau_mu_global" controls the global mean that
            covariate coefficients get pooled toward. 0 forces the global mean to
            be 0, and 1 allows it to be estimated. "tau_mu_disc" and "tau_mu_cont"
            are the standard deviations of the model-specific deviations from the
            global mean for discrete and continuous covariates, respectively: 0 forces
            all models to share the same global mean (complete pooling), and > 0 allows
            for models to deviate from the global mean (no-pooling). Finally,
            "tau_sigma_disc" and "tau_sigma_cont" control pooling of discrete and
            continuous covariate coefficients toward the model-specific means. 0 forces
            all discrete/continuous covariate coefficients to take on the model-specific
            means, and 1 allows for partial pooling across respective covariates within models.
        autofit_override: Dictionary of keyword arguments to send to the `dunsink`
            autofit routine. e.g., `{"divergence_rate_threshold": 0.01}`.
        seed: Random number seed to use for model fitting.
    """

    # Global model parameter names
    GLOBAL_POOLING_HYPERPARAMETERS = ["mu_global"]
    DISCRETE_POOLING_PARAMETERS = ["mu_disc", "sigma_disc"]
    CONTINUOUS_POOLING_PARAMETERS = ["mu_cont", "sigma_cont"]
    ALPHA = "alpha"
    BETA_DISC = "beta_disc"
    BETA_CONT = "beta_cont"
    PARAMETERS = [
        ALPHA,
        BETA_DISC,
        BETA_CONT,
        DISCRETE_POOLING_PARAMETERS,
        CONTINUOUS_POOLING_PARAMETERS,
        GLOBAL_POOLING_HYPERPARAMETERS,
    ]

    # Default parameter dictionary
    DEFAULT_PRIORS: Priors = {
        # for all models
        "alpha_loc": 0,
        "alpha_scale": 1,
        "lambda_loc": 4,
        # for pooling model
        "tau_mu_global": 1,
        "tau_mu_disc": 1,
        "tau_mu_cont": 1,
        "tau_sigma_disc": 1,
        "tau_sigma_cont": 1,
        # for non pooling model
        "beta_disc_loc": 0,
        "beta_cont_loc": 0,
        "beta_disc_scale": 1,
        "beta_cont_scale": 1,
    }

    def __init__(
        self,
        pointwise_diagnostics: PointwiseDiagnostics,
        discrete_covariates: Dict[str, Sequence] | None = None,
        continuous_covariates: Dict[str, Sequence] | None = None,
        continuous_covariates_transform: ContinuousTransforms = "standardize",
        partial_pooling: bool = False,
        adaptive: bool = False,
        priors: Priors | None = None,
        cmdstan_control: Dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> None:
        if not discrete_covariates and not continuous_covariates:
            raise ValueError(
                "`HierarchicalBayesStacking` requires specifying either `discrete_covariates` or `continuous_covariates` (or both)."
            )
        self.discrete_covariates = discrete_covariates
        self.continuous_covariates = continuous_covariates
        self.covariate_info = self._get_covariate_info(
            discrete_covariates, continuous_covariates
        )
        self.continuous_covariates_transform = continuous_covariates_transform.lower()
        if self.continuous_covariates_transform not in CONTINUOUS_TRANSFORMS:
            raise ValueError(
                f"`continuous_covariates_transform`  = {continuous_covariates_transform} "
                f"not found. Must be one of {CONTINUOUS_TRANSFORMS}."
            )
        self.partial_pooling = partial_pooling
        self.cmdstan_control = (
            CMDSTAN_DEFAULTS
            if cmdstan_control is None
            else (cmdstan_control | CMDSTAN_DEFAULTS)
        )
        self.seed = seed
        self.cmdstan_control["seed"] = self.seed
        # check if we have enough data for partial pooling to make sense
        n_discrete = (
            len(set().union(*list(discrete_covariates.values())))
            if discrete_covariates
            else 0
        )
        n_continuous = len(continuous_covariates) if continuous_covariates else 0
        if partial_pooling and (n_discrete + n_continuous < 3):
            warnings.warn(
                f"There are only {n_discrete + n_continuous} distinct covariates. "
                "Partial pooling may not perform well with < 3 distinct covariates."
            )

        self.adaptive = adaptive

        if priors is None:
            self._priors = self.DEFAULT_PRIORS
        else:
            # validate priors
            self._priors = {
                **self.DEFAULT_PRIORS,
                **{param.lower(): value for param, value in priors.items()},
            }
            unknown_priors = self._priors.keys() - self.DEFAULT_PRIORS.keys()
            if unknown_priors:
                raise ValueError(f"Unrecognized priors {unknown_priors}.")

        super().__init__(pointwise_diagnostics)

    def _prepare_covariates(
        self,
        discrete_covariates: Dict[str, Sequence] | None = None,
        continuous_covariates: Dict[str, Sequence] | None = None,
        covariate_info: CovariateInfo | None = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        if discrete_covariates is None and continuous_covariates is None:
            raise ValueError(
                "One of `discrete_covariates` or `continuous_covariates` must "
                "be specified to use hieararchical stacking."
            )

        discrete_covariates = (
            {key: value for key, value in sorted(discrete_covariates.items())}
            if discrete_covariates is not None
            else None
        )
        continuous_covariates = (
            {key: value for key, value in sorted(continuous_covariates.items())}
            if continuous_covariates is not None
            else None
        )

        def _standardize(values: Sequence, key: str) -> Sequence:
            if self.covariate_info[key]["2sd"] == 0:  # type: ignore
                raise ValueError(
                    f"Continuous covariate {key} cannot be standardized because "
                    "it has a standard deviation of 0."
                )

            # per Gelman 2008 (Statistics in Medicine), standardize by 2sd
            return [
                (v - self.covariate_info[key]["mean"]) / self.covariate_info[key]["2sd"]  # type: ignore
                for v in values
            ]

        def _relu(values: Sequence, key: str, plus: bool = True) -> Sequence:
            fn = max if plus else min
            return [fn(0, v - self.covariate_info[key]["median"]) for v in values]  # type: ignore

        if continuous_covariates is not None:
            if self.continuous_covariates_transform == "standardize":
                continuous_covariates = {
                    key: _standardize(values, key)
                    for key, values in continuous_covariates.items()
                }
            elif self.continuous_covariates_transform == "relu":
                plus = {
                    key + "_plus": _relu(values, key)
                    for key, values in continuous_covariates.items()
                }
                minus = {
                    key + "_minus": _relu(values, key, plus=False)
                    for key, values in continuous_covariates.items()
                }
                continuous_covariates = plus | minus

        discrete_covariate_info = (
            {key: v for key, v in covariate_info.items() if isinstance(v, set)}
            if covariate_info is not None
            else None
        )

        discrete_covariates_padded = (
            _make_dummy_vars(
                discrete_covariates,
                discrete_covariate_info,
            )
            if discrete_covariates is not None
            else {}
        )
        continuous_covariates_padded = (
            continuous_covariates if continuous_covariates is not None else {}
        )

        return (
            np.asarray([v for v in discrete_covariates_padded.values()]).T,
            np.asarray([v for v in continuous_covariates_padded.values()]).T,
        )

    def _get_covariate_info(
        self,
        discrete_covariates: Dict[str, Sequence] | None = None,
        continuous_covariates: Dict[str, Sequence] | None = None,
    ) -> CovariateInfo:
        discrete_covariate_set = (
            {k: set(v) for k, v in discrete_covariates.items()}
            if discrete_covariates is not None
            else {}
        )
        continuous_covariate_set = (
            {
                k: {"mean": np.mean(v), "2sd": np.std(v) * 2, "median": np.median(v)}
                for k, v in continuous_covariates.items()
            }
            if continuous_covariates is not None
            else {}
        )
        return discrete_covariate_set | continuous_covariate_set

    def _validate_prediction_covariates(
        self,
        discrete_covariates: Dict[str, Sequence] | None = None,
        continuous_covariates: Dict[str, Sequence] | None = None,
    ) -> None:
        pred_covariate_info = self._get_covariate_info(
            discrete_covariates, continuous_covariates
        )

        if not set(pred_covariate_info) == set(self.covariate_info):
            raise ValueError(
                "The set of covariates passed to the `predict` method must match "
                "those used to fit the stacking model."
            )

        if self.discrete_covariates is not None:
            pred_minus_train = {
                k: set(pred_covariate_info[k]) - set(self.covariate_info[k])
                for k in self.discrete_covariates
            }

            in_pred_not_train = {
                k: f"Value(s) {v} in prediction, but not fitted model covariate levels."
                for k, v in pred_minus_train.items()
                if v
            }

            if in_pred_not_train:
                text = (
                    "The following discrete covariate levels were passed to `predict`, but were "
                    f"not in the fitted model: \n\n {in_pred_not_train} \n"
                )
                if not self.partial_pooling:
                    raise ValueError(
                        text
                        + "\nPrediction with imputed level coefficients is only available "
                        "when `partial_pooling=True`."
                    )
                warnings.warn(
                    text
                    + "\nThe group-level discrete covariate distribution will be used to impute "
                    "coefficients for these new levels given the partial pooling structure."
                )

    def fit(self) -> HierarchicalBayesStacking:
        discrete, continuous = self._prepare_covariates(
            self.discrete_covariates, self.continuous_covariates
        )
        lpd_points = np.asarray(list(self.pointwise_diagnostics.values())).T

        N_data, M = lpd_points.shape
        N_discrete, K = discrete.shape if len(discrete) > 0 else (N_data, 0)
        N_continuous, P = continuous.shape if len(continuous) > 0 else (N_data, 0)

        if not N_data == N_discrete == N_continuous:
            raise ValueError(
                "Dimensions of data, discrete covariates, and continuous covariates do not match."
            )

        model = CmdStanModel(
            stan_file=(
                HIER_STACKING_MODEL_POOLING
                if self.partial_pooling
                else HIER_STACKING_MODEL
            )
        )

        fit = model.sample(
            data={
                "N": N_data,
                "M": M,
                "K": K,
                "P": P,
                "X": list(_concat_array_empty([discrete, continuous], axis=1)),
                "y": list(lpd_points),
                "adaptive": int(self.adaptive),
                **self._priors,
            },
            **self.cmdstan_control,
        )

        self._weights = {
            model: weight.T
            for model, weight in zip(
                self.pointwise_diagnostics.keys(), fit.stan_variable("w").T
            )
        }
        self._model_info = fit

        parameters = [self.ALPHA]
        parameters += [self.BETA_DISC] if K else []
        parameters += [self.BETA_CONT] if P else []
        pooling_parameters = self.DISCRETE_POOLING_PARAMETERS if K else []
        pooling_parameters += self.CONTINUOUS_POOLING_PARAMETERS if P else []
        pooling_hyperparameters = self.GLOBAL_POOLING_HYPERPARAMETERS
        if self.partial_pooling:
            parameters += pooling_hyperparameters + pooling_parameters

        self._coefficients = {
            par: (
                np.atleast_2d(fit.stan_variable(par)).T
                if par in pooling_hyperparameters
                else fit.stan_variable(par)
            )
            for par in parameters
        }

        return self

    def _generate_new_level_coefficient(
        self, idx: int, rng: np.random.RandomState
    ) -> np.ndarray:
        return np.atleast_2d(
            rng.normal(
                loc=self._coefficients["mu_disc"][idx],
                scale=self._coefficients["sigma_disc"][idx],
            )
        ).T

    def predict(
        self,
        discrete_covariates: Dict[str, Sequence] | None = None,
        continuous_covariates: Dict[str, Sequence] | None = None,
    ) -> Dict[str, np.ndarray]:
        self._validate_prediction_covariates(discrete_covariates, continuous_covariates)

        discrete, continuous = self._prepare_covariates(
            discrete_covariates, continuous_covariates, self.covariate_info
        )
        X = _concat_array_empty([discrete, continuous], axis=1)

        N_MCMC = self._coefficients["alpha"].shape[0]
        N_NEW_LEVELS = (
            (discrete.shape[1] - self.coefficients["beta_disc"].shape[2])
            if discrete.size > 0
            else 0
        )

        rng = np.random.RandomState(seed=self.seed)

        for i in range(N_MCMC):
            # generate new discrete covariate level coefficients from the
            # group-level distribuion if needed, concat between discrete
            # and continuous covariate coefficients for use below
            Beta = _concat_array_empty(
                [
                    (
                        self._coefficients["beta_disc"][i]
                        if discrete.size > 0
                        else np.array([])
                    ),
                    *(
                        self._generate_new_level_coefficient(i, rng)
                        for _ in range(N_NEW_LEVELS)
                    ),
                    (
                        self._coefficients["beta_cont"][i]
                        if continuous.size > 0
                        else np.array([])
                    ),
                ],
                axis=1,
            )
            # each element of alpha and Beta in the generator expression below contains
            # model coefficients for a given model per the stacking routine. There will
            # be N_MODELS - 1 elements, and then 0's are concatenated at the end to
            # compute relative model stacking weights for all N_MODELS
            unconstrained_weights = np.concatenate(
                [
                    np.atleast_2d(
                        [
                            a + np.dot(X, b)
                            for a, b in zip(self._coefficients["alpha"][i], Beta)
                        ]
                    ),
                    np.atleast_2d(np.zeros(X.shape[0])),
                ],
                axis=0,
            )
            new_weights = np.apply_along_axis(
                _compute_weights, 0, unconstrained_weights
            )

            # compute running mean weights taken across posterior samples, which
            # helps to ensure that this step does not cause RAM issues
            if i == 0:
                weights = new_weights
            weights = _running_weight_mean(weights, new_weights, i + 1)

        return {
            model: np.atleast_2d(weight)
            for model, weight in zip(
                self.pointwise_diagnostics.keys(),
                weights,
            )
        }

    @property
    def priors(self) -> Priors:
        return self._priors


class PseudoBma(BayesBlendModel):
    """Subclass to compute model weights by pseudo Bayesian model averaging (pseudo-BMA).

    Information criteria weights are derived by a simple rescaling procedure
    (computing the differences between each IC and the maximum IC) and running the
    rescaled values through a softmax function. This procedure is referred to as
    pseudo Bayesian model averaging (BMA), whereas traditional BMA weights models
    by their marginal likelihoods (the denominator in Bayes' rule). However, the
    marginal likelihood is non-trivial to calculate from most models.

    The `bootstrap` option allows computing pseudo-BMA+ weights, which account for the
    uncertainty in information criteria by using a Bayesian bootstrap procedure to
    compute the distribution of log scores from the approximate leave-one-out
    predictive densities. The average weight across bootstrap replicates is used for
    each model. This is the default procedure because it performs better in so-called
    M-complete and M-open contexts.

    For further information, see Yao et al. (2018):
    http://www.stat.columbia.edu/~gelman/research/published/stacking_paper_discussion_rejoinder.pdf

    Attributes:
        pointwise_diagnostics: As in the base `BayesBlendModel` class.
        bootstrap: bool indicating whether the Bayesian bootsrap should be
            used to obtain regularized weight estimates (pseudo-BMA+). Defaults to `True`.
        n_boots: Number of bootstrap samples for the Bayesian bootstrap procedure.
            Defaults to `10_000`.
        seed: Random seed to use for the bootstrapping procedure.
    """

    def __init__(
        self,
        pointwise_diagnostics: PointwiseDiagnostics,
        bootstrap: bool = True,
        n_boots: int = 10_000,
        seed: int | None = None,
    ) -> None:
        self.bootstrap = bootstrap
        self.n_boots = n_boots
        self.seed = seed
        super().__init__(pointwise_diagnostics)

    def _bb_weights(
        self, x: np.ndarray, alpha: Union[float, np.ndarray] = 1.0
    ) -> np.ndarray:
        """A Bayesian bootstrap implementation to sample model weights"""

        N = np.shape(x)[0]
        _alpha = [alpha] * N if isinstance(alpha, float) else alpha
        state = {"random_state": np.random.default_rng(self.seed)}
        sample_weights = dirichlet(_alpha).rvs(size=self.n_boots, **state)
        return np.matmul(sample_weights, x * N)

    def fit(self) -> PseudoBma:
        if not self.bootstrap:
            elpds = {
                model: np.sum(lpd) for model, lpd in self.pointwise_diagnostics.items()
            }
            weights = _compute_weights(np.asarray(list(elpds.values())))

        else:
            lpd_points = np.asarray(list(self.pointwise_diagnostics.values())).T
            raw_bb_weights = np.asarray(
                [_compute_weights(bb) for bb in self._bb_weights(lpd_points)]
            )
            weights = raw_bb_weights.mean(axis=0)

        self._weights = {
            model: np.atleast_2d(weight)
            for model, weight in zip(self.pointwise_diagnostics.keys(), weights)
        }

        return self

    def predict(self) -> Weights:
        return self.weights


def _compute_weights(x: np.ndarray, rescaler: float = 1) -> List[float]:
    """Compute normalized weights.

    An optional rescaler value can be supplied that converts
    the raw weights (e.g. information criteria) to the log
    density scale. Traditional AIC-type weighting requires
    multiplying the criteria by -0.5, as the criteria are proportional
    to negative 2 times the log-likelihood.
    elpd_loo or elpd_waic, however, are already
    on the raw log density scale and so we don't need to re-scale.
    """

    # Rescale to difference from maximum
    z = x - max(x)

    # Apply optional rescaling value
    z *= rescaler

    # Compute the weights
    W = sum(np.exp(z))
    w = [np.exp(zz) / W for zz in z]

    return w


def _make_dummy_vars(
    discrete_covariates: Dict[str, Sequence] | None = None,
    discrete_covariate_info: Dict[str, Set[Any]] | None = None,
) -> Dict[Hashable, Sequence]:
    if discrete_covariates is None:
        return {}

    new_levels_df = pd.DataFrame()

    if discrete_covariate_info is not None:
        unique_levels_info = set().union(*list(discrete_covariate_info.values()))
        unique_levels_data = set().union(*list(discrete_covariates.values()))
        has_missing_levels = unique_levels_info - unique_levels_data
        has_new_levels = unique_levels_data - unique_levels_info

        # We create dummy coded columns here and append them to the end of the
        # dataframe so that we later know which columns did not originally
        # have the "new" covariates for prediction purposes
        if has_new_levels:
            new_levels_dict = {
                covariate: list(
                    set(levels).difference(discrete_covariate_info[covariate])
                )
                for covariate, levels in discrete_covariates.items()
                if set(levels).difference(discrete_covariate_info[covariate])
            }

            new_level_dummys = {}
            for covariate, new_levels in new_levels_dict.items():
                for level in new_levels:
                    new_covariate = covariate + "_" + level
                    dummy_codes = [
                        1 if v == level else 0 for v in discrete_covariates[covariate]
                    ]
                    new_level_dummys[new_covariate] = dummy_codes

            new_levels_df = pd.DataFrame(new_level_dummys)

        # if levels are missing, append dummy df to end of data df, assign dummy
        # variables, and then remove appended dummy df from result. This way,
        # we get the necessary discrete covariate columns, even if all 0's
        if has_missing_levels:
            missing_level_df = pd.DataFrame(
                {
                    k: pd.Series(list(v))
                    for k, v in (discrete_covariates | discrete_covariate_info).items()
                }
            ).ffill()

            dummy_coded_df = pd.get_dummies(
                pd.concat([pd.DataFrame(discrete_covariates), missing_level_df]),
                drop_first=True,
            ).iloc[: -len(missing_level_df)]

            return pd.concat(
                [dummy_coded_df.drop(new_levels_df.columns, axis=1), new_levels_df],
                axis=1,
            ).to_dict("list")

    return pd.concat(
        [
            pd.get_dummies(
                pd.DataFrame(discrete_covariates), drop_first=True, dtype=int
            ),
            new_levels_df,
        ],
        axis=1,
    ).to_dict("list")


def _concat_array_empty(arrays: List[np.ndarray], axis: int = 0) -> np.ndarray:
    return np.concatenate([array for array in arrays if len(array) > 0], axis=axis)


def _running_weight_mean(
    prior_weights: np.ndarray, new_weights: np.ndarray, idx: int
) -> np.ndarray:
    return prior_weights * (1 - (1 / idx)) + new_weights * (1 / idx)
